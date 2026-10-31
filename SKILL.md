---
name: db-usage-audit
description: Exhaustively audits a .NET / Entity Framework Core + SQL Server backend and writes a markdown fact report (DB_USAGE_REPORT.md) of every table, column, index and constraint, plus every workflow (API endpoint, background job, queue consumer, timer, migration, raw SQL) and exactly which tables and columns each one reads or writes and whether those columns are indexed. The report is the input for a later DB-optimization planning agent, so it records facts and evidence only, not recommendations. Use this whenever the user wants to map database usage, document which workflows touch which tables or columns, prepare for table splitting, normalization or index tuning, find index coverage gaps, or says things like "scan the backend for DB usage", "which endpoints hit this table", "generate a DB usage report", or "inventory our schema and queries", even if they never say "audit" or "skill".
---

# DB Usage Audit (.NET 8 / EF Core code-first / SQL Server)

## Purpose

Produce one authoritative, evidence-backed map of **how the application uses its database**, so a second agent (or a human) can later decide what to split, normalize or index. That later agent can only be as good as this report, and a missing workflow silently becomes a bad optimization decision (e.g. dropping an "unused" index that a nightly job needs). So the bar here is **completeness and traceability**, not insight.

This skill reports facts. It does **not** recommend changes, because mixing opinions into the inventory makes it harder for the planning agent to trust and re-use. The one exception is a neutral coverage flag (e.g. "filter column has no index"), which is still a statement of fact.

Static analysis only: read source, migrations and `.sql` files. Never connect to a database. Say so in the report, and mark traffic/volume as unknown (the planning agent should get that from Azure SQL Query Store / Query Performance Insight).

## Output

Write `DB_USAGE_REPORT.md` at the repo root (or where the user says). Follow the structure in `references/report-template.md`. If the report would exceed roughly 3,000 lines, split it into `docs/db-audit/` with a `00-index.md` that links the parts, keeping stable IDs (below) so cross-references keep working.

Use stable IDs everywhere so the planning agent can grep and join: `T-<TableName>` for tables, `W-001`… for workflows, `IX-<IndexName>` for indexes. Every claim carries evidence as `path/to/File.cs:line`.

## Working method: externalize state

A real backend will not fit in context, and a report written from memory will drop things. So work in phases and persist each phase to disk in `.db-audit-work/` (add it to `.gitignore` or delete it at the end, and tell the user which you did):

- `schema.json`: tables, columns, indexes, FKs (Phase 2)
- `candidates.json`: every discovered entry point and data-access site (Phase 1/3)
- `workflows/W-xxx.md`: one trace file per workflow (Phase 4)
- `ledger.md`: coverage ledger; every candidate → traced / merged into W-xxx / excluded (with reason) / unresolved

Only assemble the final report from these files. If you have subagents available, fan Phase 4 out per bounded context (e.g. per controller group or feature folder), giving each the schema file and the tracing guide. Each subagent writes its own `workflows/*.md`; you reconcile.

## Phase -1: Preflight (set up tooling automatically)

Two helper scripts (`scan_inventory.py`, `build_workbook.py`) need Python 3.8+, and the Excel export also needs `openpyxl`. Set these up yourself so the user does not have to; do not ask them to run commands manually.

1. Find a working interpreter: try `python3 --version`, then `python --version`, then `py -3 --version` (Windows). Use whichever works as `<py>` for all script calls below, and check it reports 3.8 or newer.
2. If found, check `<py> -c "import openpyxl"`. If that fails, run `<py> -m pip install --user openpyxl` (add `--break-system-packages` only if pip refuses with an "externally managed environment" error). This is a small, user-level install, so do it without asking, and mention it in the final message.
3. If no Python is found, do **not** silently install a system runtime. Tell the user in one line that Python is missing and offer to install it with the platform package manager (`winget install Python.Python.3.12` on Windows, `brew install python` on macOS, `apt-get install python3 python3-pip` on Debian/Ubuntu), and run it only if they say yes. If they decline or the install fails, use the fallbacks below and carry on; the audit itself does not depend on Python.

Fallbacks without Python:
- **Discovery (Phase 1):** run the equivalent `grep -rEn` / `rg` searches yourself for the categories listed in Phase 1 (`DbSet<`, `[HttpGet`, `MapGet(`, `BackgroundService`, `FromSqlRaw`, `SqlConnection`, `SaveChanges`, and so on) over `*.cs` and `*.sql`, excluding `bin`, `obj` and `node_modules`, and record the hits in `candidates.json`. Expect lower recall than the script, so be extra thorough in the Phase 6 completeness gate.
- **Excel (Phase 7):** write one UTF-8 CSV per sheet (same sheet names and columns, `Access_Flat` included) into `docs/db-audit-csv/`, which Excel opens directly, and tell the user the `.xlsx` was replaced by CSVs and why.

## Phase 0: Orient

1. Find solution/projects (`*.sln`, `*.csproj`); identify the API host, worker/function hosts, data project(s), and test projects (exclude tests from workflows but note them).
2. Find every `DbContext` subclass. Multiple contexts or multiple databases are common; treat each separately and record the connection name.
3. Note the ORM mix: EF Core, plus Dapper / `SqlConnection` / `FromSqlRaw` / stored procedures. Note EF version and provider (`Microsoft.EntityFrameworkCore.SqlServer`).
4. Note how data access is organized (repositories, specification pattern, MediatR handlers, services calling `DbContext` directly). This decides how you trace in Phase 4. See `references/ef-core-tracing-guide.md`.

## Phase 1: Run the discovery scan

Run `<py> <skill-dir>/scripts/scan_inventory.py <repo-root> --out .db-audit-work/candidates.json`. It is a deterministic regex sweep that lists: `DbSet<>` properties, `IEntityTypeConfiguration`, migrations, controller actions, minimal-API maps, `IHostedService`/`BackgroundService`, Hangfire/Quartz/Azure Functions triggers, MediatR handlers, message consumers (Service Bus, MassTransit, etc.), raw SQL and stored-procedure call sites, `.sql` files, and `DbContext` injection sites.

The script is a net, not an oracle. It will miss unusual patterns and over-report some. Use it to seed the ledger, then do your own targeted `grep` for anything the project does differently (custom base controllers, endpoint filters, convention-based registration). Add what you find to `candidates.json`.

## Phase 2: Schema inventory (ground truth = migrations + model snapshot)

Code-first means the deployed schema is defined by migrations, and the C# model may be ahead of or behind them. Build the schema in this priority order:

1. `*ModelSnapshot.cs`: the current model as EF understands it. Best single source for tables, columns, types, nullability, max lengths, keys, indexes (including `IsUnique`, filters, included columns), FKs and delete behavior, owned types, TPH discriminators, table splitting, temporal tables, sequences, and `HasData` seed data.
2. Migration `Up()`/`Down()` files: raw `migrationBuilder.Sql(...)` (views, triggers, procs, indexes created by hand, computed columns) which the snapshot does **not** show. Search all of these. Also catch renamed/dropped objects so you do not report stale ones.
3. Fluent config (`OnModelCreating`, `IEntityTypeConfiguration<T>`) and entity attributes (`[Index]`, `[Key]`, `[Column]`, `[Timestamp]`, `[ConcurrencyCheck]`, `[NotMapped]`), mainly to explain *why* and to catch pending (un-migrated) model changes. If model and snapshot disagree, record both and flag **"pending migration / drift"**.
4. Standalone `.sql` scripts, DB projects, or deployment scripts (look in `/scripts`, `/db`, pipeline YAML) for objects EF does not own.

Capture per table: schema, name, entity class, columns (name, SQL type, nullable, default, computed, max length, concurrency token), PK, **clustered** key, FKs (with delete behavior), all indexes (name, key columns in order, included columns, unique, filter, clustered/nonclustered), check constraints, and whether it is a view, keyless entity, or temporal. EF conventions create **implicit indexes on every FK column** and a clustered index on the PK unless overridden; record these as indexes (marked `convention`) because they count for coverage.

Save to `.db-audit-work/schema.json`.

## Phase 3: Enumerate workflows (the entry points)

A **workflow** is any externally triggered unit of work that can reach the database. Enumerate all of these categories and record each as a candidate, including those that touch no table (so the ledger can prove it was checked):

- HTTP endpoints: controller actions, minimal APIs, gRPC, SignalR hub methods, GraphQL resolvers
- Background work: `BackgroundService`/`IHostedService`, Hangfire, Quartz.NET, Azure Functions (timer/queue/blob/HTTP/Service Bus/Event Grid triggers), WebJobs
- Message/event consumers and outbox/inbox processors
- Startup tasks: migrations at startup, seeding, cache warmers, health checks that query the DB
- Auth/identity flows (ASP.NET Identity tables, token stores, `UserStore`) and middleware/filters that query the DB per request (tenant resolution, permissions, audit logging)
- Migrations that move data (`migrationBuilder.Sql` with `UPDATE`/`INSERT`)
- CLI / console tools in the repo that use the data project

The React frontend is not itself a workflow, but if cheap, note which UI screens call which endpoint (grep `fetch`/`axios`/API client modules) as an optional `ui_callers` hint; it tells the planning agent which endpoints are user-facing and likely hot. Skip it if it would take long.

Assign `W-001`… IDs. Name them by trigger, e.g. `POST /api/orders` or `Timer: NightlyInvoiceJob`.

## Phase 4: Trace each workflow to tables and columns

For each workflow, follow the call chain from the entry point down to every `DbContext`/SQL touchpoint, through services, repositories, handlers, extension methods, specifications and base classes. Resolve interfaces to their implementations via DI registrations. Then record **per table touched**:

| Field | What to capture |
|---|---|
| Operation | READ / INSERT / UPDATE / DELETE / UPSERT / BULK, plus `ExecuteUpdate/Delete`, raw SQL, proc |
| Columns read | explicit projection columns; if no `Select` projection, **all columns** (mark `*`, entity materialization) |
| Columns in predicates | `Where`, `Any`, `FirstOrDefault(x => …)`, join `On`, `Contains` → separate "filter" columns |
| Join columns | from `Include`/`ThenInclude` (FK + referenced key), explicit `Join`, navigation use in predicates |
| Sort / group columns | `OrderBy`, `GroupBy`, `Distinct`, aggregate columns |
| Columns written | on INSERT all non-default columns; on UPDATE only modified columns (see tracking notes in the guide) |
| Paging / limits | `Skip/Take`, `Take`, or **unbounded** |
| Tracking | tracked vs `AsNoTracking` |
| Transaction scope | which tables are written in the same `SaveChanges`/transaction |
| Evidence | `file:line` for each |

Things that touch columns **implicitly** and are the easiest to miss (full detail in `references/ef-core-tracing-guide.md`): global query filters (soft delete, tenant id), audit columns set by `SaveChanges` overrides/interceptors, concurrency tokens, TPH discriminators, owned types, shadow properties, value converters, cascade deletes, and `Include` of collections that pull child tables. Attribute these to the workflows that trigger them.

If a path is dynamic (query built from a string, expression passed in by callers, generic repository), do not guess silently: list every concrete call site you can find, and if some remain unresolvable, record them under **Unresolved** with what is known (likely tables, why unresolved). An honest "unresolved" is far more useful than a confident guess.

Write one `workflows/W-xxx.md` per workflow as it is traced, immediately. Do not hold traces in memory.

## Phase 5: Index coverage per access

For every (workflow, table) pair, evaluate each filter/join/sort column set against the table's indexes from Phase 2 and label it:

- `COVERED-SEEK`: a usable index whose **leading key column(s)** match the equality/range predicate columns
- `COVERED-PARTIAL`: an index exists but only a leading prefix matches, or the sort/extra columns are not in the key (note which)
- `COVERED-INCLUDE`: the index also includes the selected columns (no key lookup needed)
- `NOT-COVERED`: no index has the column as a usable leading key (PK/clustered counts if it matches)
- `UNKNOWN`: dynamic predicate

SQL Server specifics that change the answer: column order matters in composite indexes; filtered indexes only apply if the query's predicate implies the filter; functions on columns (`.ToLower()`, `.Date`, `x.Name.Contains(…)` → `LIKE '%…%'`) make predicates non-sargable even with an index; implicit conversions (string vs `varchar`/`nvarchar`) can defeat seeks. Record these as facts about the predicate ("non-sargable: leading wildcard"), not as advice.

Do not claim index coverage from memory. Cite the index ID you matched.

## Phase 6: Reverse views and completeness

Build the cross-references the planning agent needs:

- **Table → workflows** (with R/W counts, and the columns each workflow uses)
- **Column → workflows** (columns never referenced by any workflow are listed explicitly as "no references found", which is a meaningful fact for table-splitting)
- **Index → workflows that use it** (and indexes no traced workflow can use, stated carefully: "no traced workflow predicate matches", since raw SQL and ad-hoc queries may exist)
- **Co-access**: sets of columns that are always read/written together, and column groups used by disjoint sets of workflows (raw material for vertical splitting)
- **Write amplification**: tables written by many workflows, and workflows writing many tables in one transaction

Then run the **completeness gate** before writing the final report:

1. Every `DbSet`/table appears in the report with at least its referencing workflows or an explicit "no references found".
2. Every candidate in `candidates.json` is in `ledger.md` as traced, merged, excluded-with-reason, or unresolved. No silent drops.
3. Re-run a few independent greps (`SaveChanges`, `ExecuteSql`, `FromSql`, `SqlConnection`, `.Database.`, `DbContext` constructor injection) and confirm each hit is covered by a traced workflow. Anything not covered gets traced or listed as unresolved.
4. Spot-check three random traced workflows by re-reading the code against the trace.

Report the gate results at the top of the report (counts of workflows, tables, unresolved items, and which checks passed). If the gate fails, say so rather than papering over it.

## Phase 7: Excel export

Teams review this kind of data in a spreadsheet (filter, sort, pivot), so after the markdown report passes the gate, also produce `DB_USAGE_REPORT.xlsx` from it. Build the workbook from the report rather than writing it separately, so the two can never disagree:

```
<py> <skill-dir>/scripts/build_workbook.py DB_USAGE_REPORT.md --out DB_USAGE_REPORT.xlsx
```

(Pass the `docs/db-audit/` folder instead if the report was split. `<py>` and `openpyxl` were set up in the preflight; if the preflight fell back to CSVs, skip the script.)

Sheets produced: `Workflows`, `Workflow_Access` (one row per workflow × table query), `Access_Flat` (one row per workflow × table × role × column with its index-coverage label, the best sheet for pivots), `Columns`, `Table_Indexes`, `Foreign_Keys`, `Index_Inventory`, `Table_to_Workflows`, `Column_to_Workflows`, `Unresolved`.

The script identifies tables by their header text, so the report must follow `references/report-template.md` (see "Rules that keep the Excel export working"). After running it, read the script's output: it lists rows per sheet and warns about skipped tables or empty core sheets. If a sheet that should have data is empty or missing, fix the report's table headers and rerun, don't hand-edit the workbook. Spot-check that the `Access_Flat` row count is plausible against the workflow detail section, and mention the workbook path in your final message.

## Output quality rules

- Evidence over assertion: every table/column/index claim has a `file:line` or migration name.
- Distinguish **model**, **migration** and **hand-written SQL** sources when they disagree.
- Mark confidence (`high` / `medium` / `low`) on traces that depend on DI resolution, reflection, or dynamic queries.
- Keep recommendations out. Describe, do not prescribe.
- Keep tables machine-friendly: one row per fact, consistent column names, IDs in backticks, so another agent can parse them.
- Finish by telling the user where the report is, the gate result, and the size of the unresolved list, with a pointer to what a follow-up agent should pull from Azure (Query Store, Query Performance Insight, missing-index DMVs) to add runtime volume to this static picture.
