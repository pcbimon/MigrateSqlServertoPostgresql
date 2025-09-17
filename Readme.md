
# MigrateSqlServertoPostgresql

This repository contains guidance, commands, and example scripts to migrate a SQL Server database (schema and data) to PostgreSQL.

## Objective

Migrate one or more databases from Microsoft SQL Server to PostgreSQL including:
- Schema (tables, columns, indexes, constraints, sequences where applicable)
- Data (rows, with attention to types, nulls, and defaults)
- Minimal downtime where possible (options for dump & load and logical replication)

This README documents prerequisites, an actionable step-by-step migration path, validation checks, and rollback guidance.

## Assumptions

- You have access to the SQL Server instance (connection/credentials) and the PostgreSQL server.
- Target PostgreSQL version is 12+ (adjust type mapping if using older/newer versions).
- Basic familiarity with SQL, pg_dump/psql, and SQL Server tools (sqlcmd, bcp) or third-party tools.
- Migration is performed in a secure environment and backups are created before destructive operations.

## High-level approach

1. Inventory source schema and data, identify incompatible types and objects.
2. Convert schema (DDL) from T-SQL/SQL Server syntax to PostgreSQL-compatible SQL.
3. Create the target schema in PostgreSQL and prepare for data import.
4. Export and transform data from SQL Server to a format PostgreSQL can import.
5. Load data into PostgreSQL, fix any issues, and reapply constraints/indexes if postponed.
6. Validate row counts, checksums, and application-level tests.
7. Cutover and monitor; have a rollback plan ready.

## Tools and options

- Native/CLI: sqlcmd, bcp, BULK EXPORT, bcp import, psql, pg_restore
- Conversion helpers: pgloader, ora2pg (works for SQL Server in some configs), SQL Server Migration Assistant (SSMA), AWS DMS (for continuous replication), Debezium (CDC)
- Scripting: Python (pyodbc/pymssql + psycopg2), nodejs (mssql + pg), or custom ETL
- Choose based on complexity: for quick migrations use pgloader; for zero-downtime use logical replication or CDC-based tools.

pgloader is recommended for many SQL Server → PostgreSQL migrations because it can handle schema conversion and data transfer with transformations.

## Example: pgloader-based migration (quick path)

Prerequisites:
- Install pgloader (https://pgloader.io/)
- Ensure PostgreSQL and SQL Server are reachable from the machine running pgloader

Sample pgloader command (run from a shell where pgloader is installed):

pgloader "mssql://USER:PASS@MSSQL_HOST:1433/SourceDB" "postgresql://PGUSER:PGPASS@PG_HOST:5432/TargetDB"

Key notes:
- Review pgloader output for type mapping warnings and errors.
- pgloader can migrate schema and data in one operation. For large datasets you may prefer to migrate schema first and then use bulk data loads.

## Example: manual export/import (more control)

1) Export schema from SQL Server (generate scripts via SSMS or use SQL Server Management Objects).
2) Translate DDL differences (example: replace T-SQL IDENTITY with PostgreSQL SERIAL/SEQUENCE or use GENERATED AS IDENTITY in PG12+; convert DATETIME2 to TIMESTAMP, MONEY to NUMERIC, UNIQUEIDENTIFIER to UUID).
3) Create schema in PostgreSQL (apply translated DDL with psql).
4) Export data from SQL Server as CSV using bcp or SQL Server export:

bcp "SELECT col1, col2, ... FROM dbo.MyTable" queryout C:\tmp\MyTable.csv -c -t"," -S MSSQL_HOST -U USER -P PASS

5) Prepare CSV for PostgreSQL: ensure proper null markers, quoting, date formats and encoding (UTF-8).
6) Use COPY to load data into PostgreSQL (run as a superuser or role with COPY privileges):

\copy public.mytable(col1, col2, ...) FROM 'C:/tmp/MyTable.csv' WITH (FORMAT csv, DELIMITER ',', NULL '', HEADER false, ENCODING 'utf8');

7) Recreate indexes, constraints, and foreign keys after bulk loads if you deferred them for performance.

## Type mapping cheatsheet (common cases)

- SQL Server INT -> integer
- BIGINT -> bigint
- SMALLINT -> smallint
- BIT -> boolean
- VARCHAR / NVARCHAR -> varchar / text (consider removing length or using appropriate length)
- TEXT -> text
- DATETIME / DATETIME2 -> timestamp without time zone (or with time zone if you need tz handling)
- DATE -> date
- TIME -> time
- DECIMAL/NUMERIC -> numeric
- MONEY -> numeric(19,4) or money (Postgres has money type but numeric is recommended)
- UNIQUEIDENTIFIER -> uuid (use gen_random_uuid() or uuid-ossp extension for generation)

## Validation and verification

- Row counts per table: compare SELECT COUNT(*) on source vs target.
- Checksum sampling: use hashing of concatenated sorted columns (mind NULL handling) on a sample or full data set for small tables.
- Schema checks: compare columns, types, nullability, default values.
- Application-level smoke tests: run read/write tests against the target.
- Performance checks: compare explain plans for critical queries; add indexes if needed.

## Downtime and cutover strategies

- Bulk load approach: take a maintenance window, stop writes on source, run final delta export, import, then switch app connections to PostgreSQL.
- Minimal downtime: use CDC (Change Data Capture) tools like Debezium or AWS DMS to replicate changes while migrating historical data; cutover when lag is small.

## Rollback plan

- Keep source system running until cutover is validated.
- Preserve backups (full database backups and exported files).
- If cutover fails, re-point application to SQL Server and investigate discrepancies. Document failed steps and re-run with fixes.

## Security and permissions

- Use least-privilege accounts for migration with only the permissions needed for read/export on SQL Server and create/load on PostgreSQL.
- Protect credentials (use secrets manager where possible).
- Ensure network access is restricted and encrypted (VPN, SSL/TLS).

## Example scripts and automation ideas

- Small PowerShell script to run bcp exports and then call pgloader or psql for loading.
- Python script to transform data on the fly (fix dates, UUIDs, encoding) and stream into postgres via COPY.

## Next steps and checklist

- Inventory all databases/tables to migrate and estimate size and complexity.
- Choose tool (pgloader for simple; DMS/Debezium for continuous replication)
- Run a test migration on a staging environment.
- Validate data and performance.
- Plan cutover and rollback window.

## Where to put scripts

Add any export/import scripts, sample pgloader load files, or transformation utilities to this repository under a `scripts/` directory. Keep credentials out of source control.

## Contact / Notes

Author: (add your name and contact info)

License: (choose appropriate license for your repo)
