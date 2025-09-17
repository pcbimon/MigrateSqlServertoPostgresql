#!/usr/bin/env python3
"""
End-to-end migration helper: extract tables from SQL Server and load into PostgreSQL using Python.

Features:
- Reads connection info from `.env` in repo root.
- Lists tables from INFORMATION_SCHEMA (or accepts a comma-separated list).
- For each table: reads column metadata, creates a matching Postgres table (basic type mapping), exports data to a CSV file in EXPORT_DIR, and imports via psycopg2 COPY.
- Supports --dry-run to show planned DDL and actions without modifying Postgres.

Limitations:
- Column type mapping covers common SQL Server types; uncommon types fallback to text.
- This implementation stages data to disk as CSV files. For very large tables consider streaming or chunked COPY.

Venv auto-activation:
This script will attempt to detect a project virtual environment (common folders: `.venv`, `venv`, `env`) at the repository root
and re-exec itself using that venv's python interpreter if the current interpreter is not already inside a virtualenv.
Set environment variable `DISABLE_VENV_AUTO_ACTIVATE=true` to opt out of this behavior.
"""

import os
import sys
import argparse
import csv
import glob
from pathlib import Path
import tempfile

from decimal import Decimal


def _maybe_activate_venv():
    """If not running inside a venv, look for common venv folders in the repo root and re-exec using that python.

    This is a best-effort convenience for developers and automation agents. It does not modify the environment
    if DISABLE_VENV_AUTO_ACTIVATE is set to 'true'.
    """
    try:
        # If sys.base_prefix != sys.prefix then we're already in a venv (CPython)
        if getattr(sys, 'base_prefix', None) != getattr(sys, 'prefix', None):
            return
        if os.environ.get('DISABLE_VENV_AUTO_ACTIVATE', '').lower() == 'true':
            return

        # Determine repo root (two parents up from this script: scripts/ -> repo)
        script_path = Path(__file__).resolve()
        repo_root = script_path.parents[1]

        candidates = ['.venv', 'venv', 'env']
        for c in candidates:
            vpath = repo_root / c
            if not vpath.exists():
                continue
            # Determine python executable path in platform-aware way
            if os.name == 'nt':
                py = vpath / 'Scripts' / 'python.exe'
            else:
                py = vpath / 'bin' / 'python'
            if py.exists():
                # Re-exec using the venv python with same argv
                os.execv(str(py), [str(py)] + sys.argv)
    except Exception:
        # Do not fail startup due to venv activation heuristics
        return


# Attempt to auto-activate venv early before other imports that may depend on it
_maybe_activate_venv()

def load_dotenv(path):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, 'r', encoding='utf8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env

def mssql_type_to_pg(data_type, char_max_length, numeric_precision, numeric_scale):
    t = (data_type or '').lower()
    if t in ('int',):
        return 'integer'
    if t in ('bigint',):
        return 'bigint'
    if t in ('smallint', 'tinyint'):
        return 'smallint'
    if t in ('bit',):
        return 'boolean'
    if t in ('float',):
        return 'double precision'
    if t in ('real',):
        return 'real'
    if t in ('decimal', 'numeric'):
        if numeric_precision and numeric_scale is not None:
            return f'numeric({numeric_precision},{numeric_scale})'
        return 'numeric'
    if t in ('money',):
        return 'numeric(19,4)'
    if t in ('uniqueidentifier',):
        return 'uuid'
    if t.startswith('varchar') or t.startswith('nvarchar') or t in ('text', 'ntext'):
        return 'text'
    if t in ('datetime','datetime2','smalldatetime','datetimeoffset'):
        return 'timestamp'
    if t == 'date':
        return 'date'
    if t == 'time':
        return 'time'
    if t in ('binary','varbinary','image'):
        return 'bytea'
    return 'text'

def get_tables_list(mssql_conn, db, include_list=None, schema_list=None):
    cur = mssql_conn.cursor()
    # If include_list provided, it contains fully qualified names and takes precedence
    if include_list:
        # include_list expected as list of 'schema.table' or '[schema].[table]'
        placeholders = ','.join('?' for _ in include_list)
        sql = f"SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE' AND QUOTENAME(TABLE_SCHEMA)+'.'+QUOTENAME(TABLE_NAME) IN ({placeholders}) ORDER BY TABLE_SCHEMA, TABLE_NAME"
        params = include_list
    else:
        if schema_list:
            # filter by provided schemas
            placeholders = ','.join('?' for _ in schema_list)
            sql = (f"SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                   f"WHERE TABLE_TYPE='BASE TABLE' AND TABLE_SCHEMA IN ({placeholders}) "
                   f"ORDER BY TABLE_SCHEMA, TABLE_NAME")
            params = schema_list
        else:
            sql = "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE' ORDER BY TABLE_SCHEMA, TABLE_NAME"
            params = []
    cur.execute(sql, params)
    return [(r[0], r[1]) for r in cur.fetchall()]

def get_mssql_columns(mssql_conn, db, schema, table):
    cur = mssql_conn.cursor()
    sql = ("SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE "
           "FROM INFORMATION_SCHEMA.COLUMNS "
           "WHERE TABLE_CATALOG = ? AND TABLE_SCHEMA = ? AND TABLE_NAME = ? "
           "ORDER BY ORDINAL_POSITION;")
    cur.execute(sql, (db, schema, table))
    rows = cur.fetchall()
    cols = []
    for r in rows:
        cols.append({
            'name': r[0],
            'data_type': r[1],
            'char_max_length': r[2],
            'numeric_precision': r[3],
            'numeric_scale': r[4],
        })
    return cols

def create_table_sql(pg_schema, pg_table, cols):
    cols_sql = []
    for c in cols:
        pgtype = mssql_type_to_pg(c['data_type'], c['char_max_length'], c['numeric_precision'], c['numeric_scale'])
        name = c['name']
        cols_sql.append(f'"{name}" {pgtype}')
    cols_def = ', '.join(cols_sql) if cols_sql else 'id serial primary key'
    sql = f'CREATE SCHEMA IF NOT EXISTS "{pg_schema}"; CREATE TABLE IF NOT EXISTS "{pg_schema}"."{pg_table}" ({cols_def});'
    return sql

def export_table_to_csv(mssql_conn, db, schema, table, csv_path, env, use_unicode=True):
    """
    Export table to CSV using pyodbc and Python's csv.writer to produce stable UTF-8 CSVs compatible with Postgres COPY.
    Returns column names in ordinal order.

    If the environment variable USE_BCP_FALLBACK=true is set, and pyodbc export fails, fall back to bcp subprocess.
    """
    import pyodbc as _pyodbc
    import csv as _csv
    import subprocess as _subprocess

    os.makedirs(os.path.dirname(csv_path), exist_ok=True)

    # Get column metadata and build SELECT with explicit column list to ensure deterministic order
    cols = get_mssql_columns(mssql_conn, db, schema, table)
    if not cols:
        # no columns? fall back to empty list
        col_names = []
        # create empty file
        open(csv_path, 'w', encoding='utf8', newline='').close()
        return col_names

    col_names = [c['name'] for c in cols]
    col_list_sql = ', '.join(f'[{c}]' for c in col_names)

    # Build SELECT qualified name
    qualified = f'[{db}].[{schema}].[{table}]'
    select_sql = f'SELECT {col_list_sql} FROM {qualified}'

    try:
        cur = mssql_conn.cursor()
        cur.execute(select_sql)
        with open(csv_path, 'w', encoding='utf8', newline='') as fout:
            writer = _csv.writer(fout, quoting=_csv.QUOTE_MINIMAL)
            # Write rows with robust type/encoding conversion
            import datetime as _dt
            import uuid as _uuid
            for row in cur:
                out_row = []
                for v in row:
                    # None -> empty
                    if v is None:
                        out_row.append('')
                        continue
                    # Decimal -> str
                    if isinstance(v, Decimal):
                        out_row.append(str(v))
                        continue
                    # bytes-like -> try decodings
                    if isinstance(v, (bytes, bytearray, memoryview)):
                        try:
                            b = bytes(v)
                            out_row.append(b.decode('utf8'))
                            continue
                        except Exception:
                            try:
                                out_row.append(b.decode('utf-16-le'))
                                continue
                            except Exception:
                                out_row.append(b.decode('latin-1', errors='replace'))
                                continue
                    # datetimes -> iso
                    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
                        out_row.append(v.isoformat())
                        continue
                    # uuid -> str
                    if isinstance(v, _uuid.UUID):
                        out_row.append(str(v))
                        continue
                    # fallback to str
                    out_row.append(str(v))
                writer.writerow(out_row)
        return col_names
    except Exception as ex:
        # If this is an encoding/codec issue, automatically fall back to bcp.
        use_bcp = (env.get('USE_BCP_FALLBACK','').lower() == 'true')
        is_codec = isinstance(ex, UnicodeDecodeError) or 'utf-16' in str(ex).lower() or 'codec' in str(ex).lower()
        if not (use_bcp or is_codec):
            raise
        print('pyodbc exporter failed with encoding/codec error, falling back to bcp:', ex)

    # bcp fallback path (preserve original behavior)
    format_flag = '-w' if use_unicode else '-c'
    server = env.get('MSSQL_HOST') or ''
    port = env.get('MSSQL_PORT') or '1433'
    user = env.get('MSSQL_USER') or ''
    pwd = env.get('MSSQL_PASS') or ''
    server_arg = f"{server},{port}"
    bcp_cmd = [
        'bcp',
        select_sql,
        'queryout',
        csv_path,
        format_flag,
        '-t","',
        '-S', server_arg,
        '-U', user,
        '-P', pwd
    ]
    print('Falling back to bcp:', ' '.join(bcp_cmd))
    proc = _subprocess.run(bcp_cmd, stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, text=True)
    if proc.returncode != 0:
        print('bcp failed:', proc.stderr)
        raise SystemExit(proc.stderr)

    return col_names

def import_csv_to_pg(pg_conn, csv_path, pg_schema, pg_table, col_names):
    # Try reading as UTF-8; if that fails (bcp -w produced UTF-16LE), convert to UTF-8 first
    try:
        with open(csv_path, 'r', encoding='utf8') as f_check:
            _ = f_check.read(1024)
        source_path = csv_path
        temp_converted = None
    except UnicodeDecodeError:
        import tempfile
        # Convert from UTF-16-LE (common bcp -w output) to UTF-8
        temp_fd, temp_name = tempfile.mkstemp(suffix='.csv', prefix='pg_convert_')
        os.close(temp_fd)
        with open(csv_path, 'r', encoding='utf-16-le', errors='replace') as fin, open(temp_name, 'w', encoding='utf8', newline='') as fout:
            for line in fin:
                fout.write(line)
        source_path = temp_name
        temp_converted = temp_name

    # Normalize CSV dialect (delimiter and quoting) into a UTF-8, comma-delimited, properly quoted temp file
    def _normalize_csv(path, expected_cols):
        import tempfile as _temp
        import csv as _csv

        # Quick heuristic: try delimiters and pick the one that yields correct column count
        candidates = [',', '\t', '|', ';']
        best = None
        best_count = 0
        for d in candidates:
            try:
                with open(path, 'r', encoding='utf8', newline='') as ff:
                    reader = _csv.reader(ff, delimiter=d)
                    first = next(reader, None)
                    if first is None:
                        continue
                    cnt = len(first)
                    if cnt == len(expected_cols):
                        best = d
                        best_count = cnt
                        break
                    if cnt > best_count:
                        best = d
                        best_count = cnt
            except Exception:
                continue

        # If no delimiter found, default to comma
        if not best:
            best = ','

        # If the file already uses comma and correct count, we can return original path
        if best == ',' and best_count == len(expected_cols):
            return path

        # Otherwise rewrite into a proper CSV with comma delimiter and quoting
        temp_fd, temp_name = _temp.mkstemp(suffix='.csv', prefix='pg_normalized_')
        os.close(temp_fd)
        with open(path, 'r', encoding='utf8', newline='') as fin, open(temp_name, 'w', encoding='utf8', newline='') as fout:
            reader = _csv.reader(fin, delimiter=best)
            writer = _csv.writer(fout, delimiter=',', quoting=_csv.QUOTE_MINIMAL)
            for row in reader:
                # If row has fewer cols than expected, pad with empty strings
                if len(row) < len(expected_cols):
                    row = row + [''] * (len(expected_cols) - len(row))
                writer.writerow(row)
        return temp_name

    norm_path = _normalize_csv(source_path, col_names if col_names else [])

    with open(norm_path, 'r', encoding='utf8', newline='') as f:
        cols_list = ','.join([f'"{c}"' for c in col_names]) if col_names else ''
        if cols_list:
            copy_sql = f"COPY \"{pg_schema}\".\"{pg_table}\" ({cols_list}) FROM STDIN WITH (FORMAT csv, DELIMITER ',' , NULL '' , HEADER false)"
        else:
            copy_sql = f"COPY \"{pg_schema}\".\"{pg_table}\" FROM STDIN WITH (FORMAT csv, DELIMITER ',' , NULL '' , HEADER false)"
        cur = pg_conn.cursor()
        cur.copy_expert(copy_sql, f)
        pg_conn.commit()

    # cleanup temp files
    if temp_converted:
        try:
            os.remove(temp_converted)
        except Exception:
            pass
    if norm_path != source_path:
        try:
            os.remove(norm_path)
        except Exception:
            pass

    if temp_converted:
        try:
            os.remove(temp_converted)
        except Exception:
            pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--export-dir', default=None)
    parser.add_argument('--tables', default=None, help='Comma-separated list of tables as [schema].[table] or schema.table')
    parser.add_argument('--schema', default=None, help='(legacy) Comma-separated list of source schemas to include when --tables is not provided')
    parser.add_argument('--source-schema', default=None, help='Comma-separated list of source schemas to include when --tables is not provided')
    parser.add_argument('--target-schema', default=None, help='Postgres target schema to create tables in (default: public)')
    parser.add_argument('--create-only', default=None, help='Comma-separated list of tables to only create schema for (no data). Use schema.table or table names')
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    env = load_dotenv(str('.env'))

    export_dir = args.export_dir or env.get('EXPORT_DIR') or str(repo_root / 'exports')
    os.makedirs(export_dir, exist_ok=True)

    # Connect to MSSQL
    # Import pyodbc only when we actually need to connect to MSSQL
    try:
        import pyodbc
    except ImportError:
        print('Missing dependency: pyodbc is required to connect to SQL Server. Install with: pip install pyodbc')
        raise

    driver = env.get('MSSQL_DRIVER','{ODBC Driver 17 for SQL Server}')
    server = env.get('MSSQL_HOST')
    port = env.get('MSSQL_PORT','1433')
    user = env.get('MSSQL_USER')
    pwd = env.get('MSSQL_PASS')
    database = env.get('MSSQL_DATABASE')
    mssql_conn_str = f'DRIVER={driver};SERVER={server},{port};UID={user};PWD={pwd};DATABASE={database}'
    print('Connecting to MSSQL', server, database)
    mssql_conn = pyodbc.connect(mssql_conn_str)

    # Connect to Postgres
    pg_host = env.get('PG_HOST','localhost')
    pg_port = env.get('PG_PORT','5432')
    pg_db = env.get('PG_DATABASE')
    pg_user = env.get('PG_USER')
    pg_pass = env.get('PG_PASS')
    print('Postgres target', pg_host, pg_db)
    if args.dry_run:
        pg_conn = None
    else:
        try:
            import psycopg2
        except ImportError:
            print('Missing dependency: psycopg2 is required to connect to Postgres. Install with: pip install psycopg2-binary')
            raise
        pg_conn = psycopg2.connect(host=pg_host, port=pg_port, dbname=pg_db, user=pg_user, password=pg_pass)

    include_list = None
    if args.tables:
        include_list = [t.strip() for t in args.tables.split(',') if t.strip()]
    # Determine source schema(s): prefer --source-schema, fall back to legacy --schema, else None
    schema_list = None
    src_schema_arg = args.source_schema or args.schema
    if src_schema_arg and not include_list:
        schema_list = [s.strip() for s in src_schema_arg.split(',') if s.strip()]

    # Determine target postgres schema: CLI -> .env -> default 'public'
    target_schema = args.target_schema or env.get('PG_SCHEMA') or 'public'

    # Parse create-only list into a normalized set of lowercase identifiers
    create_only_set = set()
    if args.create_only:
        for t in args.create_only.split(','):
            tt = t.strip()
            if not tt:
                continue
            # Normalize to either 'schema.table' or just 'table' in lowercase
            create_only_set.add(tt.strip().lower())

    tables = get_tables_list(mssql_conn, database, include_list, schema_list)
    if not tables:
        print('No tables found to migrate')
        return

    summary = []
    for schema, table in tables:
        print('\nMigrating', f'{schema}.{table}', '->', f'{target_schema}.{table}')
        cols = get_mssql_columns(mssql_conn, database, schema, table)
        create_sql = create_table_sql(target_schema, table, cols)
        print('DDL:', create_sql)
        if not args.dry_run:
            cur = pg_conn.cursor()
            cur.execute(create_sql)
            pg_conn.commit()

        # Special-case: for `MasUser` we only want to create the table and NOT export/import data.
        # This allows creating schema for sensitive tables without transferring rows.
        # Determine if this table is in the create-only list (skip data export/import)
        try:
            tbl_l = (table or '').strip().lower()
            schema_tbl = f"{schema}.{table}".strip().lower()
            is_create_only = (tbl_l in create_only_set) or (schema_tbl in create_only_set)
        except Exception:
            is_create_only = False
        if is_create_only:
            print('Create-only table requested, skipping data export/import for', f'{schema}.{table}')
            summary.append((schema, table, '(create-only)'))
            continue

        # Export to CSV
        safe_name = f'{schema}_{table}'.replace('.', '_')
        csv_path = os.path.join(export_dir, safe_name + '.csv')
        print('Exporting source to', csv_path)
        col_names = export_table_to_csv(mssql_conn, database, schema, table, csv_path, env)

        print('Importing CSV into Postgres', f'{target_schema}.{table}')
        if not args.dry_run:
            import_csv_to_pg(pg_conn, csv_path, target_schema, table, col_names)

        summary.append((schema, table, os.path.abspath(csv_path)))

    print('\nMigration summary:')
    for s in summary:
        print('-', s[0] + '.' + s[1], '->', s[2])

    if not args.dry_run:
        pg_conn.close()
    mssql_conn.close()

if __name__ == '__main__':
    main()
