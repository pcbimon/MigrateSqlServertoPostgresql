## Quick context

This repo contains guidance and example commands to migrate SQL Server databases to PostgreSQL. There are no application sources yet — it's a collection of migration notes and example scripts. Key files:

- `Readme.md` — canonical walkthrough (schema conversion, pgloader usage, bcp/CSV export, validation and cutover).
- `.env.example` — environment variable names and flags used by scripts (copy to `.env` and fill in secrets).

Do not commit secrets or a filled `.env` file.

## What an AI agent should know up-front

- Primary tools referenced: pgloader, sqlcmd, bcp, psql/pg_restore, and optional scripting via PowerShell, Python (pyodbc/psycopg2) or Node.js (mssql + pg).
- The repository uses Windows-friendly paths and examples (PowerShell and `C:/tmp/...`) in `Readme.md`; prefer cross-platform paths when adding tooling but keep PowerShell examples for local dev.
- Migration mode switch: `.env` flag `USE_PGLOADER=true` (pgloader direct migration) vs `false` (CSV export/import flow). Respect this flag in any scripts you add.
- Temporary export directory: `.env` variable `EXPORT_DIR` (example `C:/tmp/migration_exports`) — scripts must honor this and create it if missing.

## Patterns & conventions to follow

- Put automation scripts under `scripts/` (PowerShell for Windows-centric tasks, or `scripts/python/` for cross-platform helpers). Name scripts with a verb prefix: `scripts/export-bcp.ps1`, `scripts/load-copy.ps1`, `scripts/pgloader-run.sh`.
- Configuration via `.env` (copy `.env.example`). Read environment with standard dotfile loaders or PowerShell `Get-Content`/`ConvertFrom-StringData` in scripts. Never parse secrets into VCS.
- For schema transformations, follow examples in `Readme.md`: translate IDENTITY => `GENERATED AS IDENTITY`, DATETIME2 => `timestamp`, UNIQUEIDENTIFIER => `uuid` (enable `pgcrypto`/`uuid-ossp` if generation is required).

## Useful repo-specific examples to reference or reuse

- pgloader direct migration (from `Readme.md`):

  pgloader "mssql://USER:PASS@MSSQL_HOST:1433/SourceDB" "postgresql://PGUSER:PGPASS@PG_HOST:5432/TargetDB"

- bcp export example (CSV) and PostgreSQL COPY import (from `Readme.md`):

  bcp "SELECT col1, col2 FROM dbo.MyTable" queryout C:/tmp/MyTable.csv -c -t"," -S $env:MSSQL_HOST -U $env:MSSQL_USER -P $env:MSSQL_PASS

  \copy public.mytable(col1, col2) FROM 'C:/tmp/MyTable.csv' WITH (FORMAT csv, DELIMITER ',', NULL '', HEADER false, ENCODING 'utf8');

Use these exact patterns when constructing migration steps or sample scripts so outputs match the README expectations.

## Virtual environment (venv) guidance

When running automation scripts in this repository prefer to use a Python virtual environment from the repository root.
This keeps dependencies isolated and ensures consistent behavior for tools like `pyodbc` and `psycopg2`.

Conventions:
- Look for a venv folder named `.venv`, `venv`, or `env` at the repository root.
- The `scripts/migrate_all.py` script will attempt to auto-activate (re-exec) itself using a found venv Python unless
  the environment variable `DISABLE_VENV_AUTO_ACTIVATE` is set to `true` or the current interpreter is already inside a venv.

Creating and activating a venv (Windows PowerShell):

1. Create the venv (run from repository root):

  python -m venv .venv

2. Activate it in PowerShell:

  # PowerShell
  .\.venv\Scripts\Activate.ps1

3. Install dependencies from `requirements.txt`:

  pip install -r requirements.txt

Notes for automation agents and CI:
- Agents should prefer activating the repository venv before executing scripts. If that's not possible, `migrate_all.py` will try
  to re-run under the venv python automatically.
- To opt-out of the automatic re-exec behavior set `DISABLE_VENV_AUTO_ACTIVATE=true` in the environment (or in `.env` for local convenience).

## Language/runtime

This repository currently uses Python only for the migration helpers and scripts. Do not add implementations in other languages without updating this guidance.

CLI for `scripts/migrate_all.py` (exact output of `python scripts/migrate_all.py -h`):

usage: -c [-h] [--dry-run] [--export-dir EXPORT_DIR] [--tables TABLES]
          [--schema SCHEMA] [--source-schema SOURCE_SCHEMA]
          [--target-schema TARGET_SCHEMA]

options:
  -h, --help            show this help message and exit
  --dry-run
  --export-dir EXPORT_DIR
  --tables TABLES       Comma-separated list of tables as [schema].[table] or  
                        schema.table
  --schema SCHEMA       (legacy) Comma-separated list of source schemas to     
                        include when --tables is not provided
  --source-schema SOURCE_SCHEMA
                        Comma-separated list of source schemas to include      
                        when --tables is not provided
  --target-schema TARGET_SCHEMA
                        Postgres target schema to create tables in (default:   
                        public)

  Additional CLI option added by contributors:

    --create-only CREATE_ONLY
                          Comma-separated list of tables to only create schema for
                          (no data). Use schema.table or table names. Matching is
                          case-insensitive; unqualified names match across schemas.

  Examples:

    # Create only schema for MasUser (no data import)
    python scripts/migrate_all.py --create-only MasUser --tables "[dbo].[MasUser]"

    # Create schema for multiple tables (mix of schema-qualified and unqualified)
    python scripts/migrate_all.py --create-only dbo.MasUser,MasDepartment --tables "[dbo].[MasUser],[dbo].[MasDepartment]"



## Integration points & external dependencies

- Network connectivity to SQL Server and PostgreSQL from the machine running migration tools (pgloader, bcp). Validate host/port from `.env` before running.
- Tools to install: pgloader (native binary), SQL Server tools that include `bcp` and `sqlcmd`, and `psql` for PostgreSQL. If adding Python helpers, document `requirements.txt` with `pyodbc`, `psycopg2-binary`.

## Developer workflows (explicit)

- Local dry run with pgloader: set `.env` values, `USE_PGLOADER=true`, then run the pgloader command from README (replace credentials). Check pgloader logs for type-mapping warnings.
- Manual export/import flow: set `USE_PGLOADER=false`, ensure `EXPORT_DIR` exists, run `bcp` exports to `EXPORT_DIR`, then use `psql`/`\copy` to import into Postgres.
- Validation: always run row-count checks (SELECT COUNT(*)) on source and target and sample checksums for sensitive tables.

## What to avoid / repo-specific gotchas

- Do not modify `Readme.md` to store executable credentials. Use `.env` and keep it out of Git.
- The README assumes PostgreSQL 12+; if adding DDL generation, prefer `GENERATED AS IDENTITY` (PG12+) rather than `SERIAL`.
- Windows paths appear in examples; ensure any new scripts either normalize paths or document platform differences.

## Files to update or check when making changes

- `Readme.md` — update walkthroughs and commands when changing default approaches.
- `.env.example` — keep in sync with any new script environment variables.
- Add scripts under `scripts/` and reference them from `Readme.md`.

## Final checklist for AI contributors

- Use values from `.env.example` to infer variable names and default behaviors.
- Reference the exact `pgloader` and `bcp` command patterns from `Readme.md` when producing scripts or examples.
- Document any new external dependencies in a top-level `requirements.txt` or `README.md` and add install notes.
 - When adding CLI flags (like `--create-only`), document them here and provide a small example showing typical usage.

If anything in these instructions is unclear or missing (for example: preferred scripting language, CI steps, or a canonical test dataset), tell me what you want added and I will update this file.
