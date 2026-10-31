# DB_USAGE_REPORT.md template

Use this structure and these column names exactly, so the downstream planning agent can parse the report without guessing. Keep IDs in backticks. Omit nothing: an empty section says "none found" and why.

```markdown
# Database Usage Report

- Generated: <date> from commit <sha>
- Stack: .NET <ver>, EF Core <ver>, SQL Server (Azure), code-first
- DbContexts: <list with connection names>
- Method: static analysis of source, migrations and .sql files. No live DB access.
  Traffic, row counts, and runtime cost are NOT in this report.

## 0. Summary and completeness gate
| Metric | Value |
|---|---|
| Tables / views | n |
| Workflows traced | n (HTTP n, background n, consumers n, startup n, other n) |
| Candidates excluded (with reason) | n |
| Unresolved items | n (see section 8) |
| Model vs snapshot drift items | n |

Gate checks: [x] every table referenced or marked unreferenced  [x] ledger has no silent drops
[x] independent grep sweep covered  [x] spot-check of 3 workflows

## 1. Schema inventory
### `T-Orders` (dbo.Orders, entity `Order`, DbContext `AppDbContext`)
Source: Migrations/20240101_Init.cs:42, OrderConfiguration.cs:12
| Column | SQL type | Null | Default/computed | Notes (PK, FK->, concurrency, discriminator) |
|---|---|---|---|---|
Indexes:
| Index ID | Kind (clustered/nonclustered/unique/filtered/convention) | Key columns (ordered) | Included columns | Filter | Source |
|---|---|---|---|---|---|
Foreign keys:
| Column | References | On delete |
|---|---|---|
Other: check constraints, triggers, temporal, seed data, owned types

## 2. Index inventory (all tables, one row per index)
| Index ID | Table | Kind | Key columns | Included | Workflows using it (W-ids) |

## 3. Workflow catalog
| ID | Trigger | Type | Entry point (file:line) | Tables touched | Writes? | Confidence | UI callers (optional) |

## 4. Workflow detail
### `W-012` POST /api/orders
Entry: OrdersController.cs:88 -> OrderService.Create -> OrderRepository.Add
Tables: `T-Orders` (INSERT), `T-OrderLines` (INSERT), `T-Customers` (READ)
| Table | Op | Columns read | Filter columns | Join columns | Sort/group | Columns written | Paging | Tracking | Index coverage | Evidence |
|---|---|---|---|---|---|---|---|---|---|---|
Coverage cell example: `CustomerId: COVERED-SEEK (IX-Orders_CustomerId)`; `Status: NOT-COVERED`
Transaction: single SaveChanges writes T-Orders, T-OrderLines
Implicit effects: global filter `IsDeleted=0` on T-Orders; audit columns CreatedAt/CreatedBy set in AppDbContext.cs:140
Notes: facts only (unbounded read, non-sargable predicate, N+1 pattern as observed)

## 5. Table -> workflows
| Table | Reads (W-ids) | Writes (W-ids) | Columns used by any workflow | Columns with NO references found |

## 6. Column -> workflows (every column of every table)
| Table.Column | Read by | Filtered by | Joined by | Sorted/grouped by | Written by | Indexed (IX-ids) |

## 7. Co-access and write-pattern facts
- Column groups always accessed together / used by disjoint workflow sets, per table
- Tables written by >= 3 workflows; workflows writing >= 3 tables in one transaction
- Predicates with NOT-COVERED or COVERED-PARTIAL coverage (list: workflow, table, columns, reason)
- Indexes no traced workflow can use (stated as "no traced predicate matches")
- Hot-path hints from structure only (loops, unbounded reads, bulk jobs), labelled "inferred, not measured"

## 8. Unresolved and needs-review
| Item | Location | What is known | Why unresolved |

## 9. Appendices
A. Raw SQL / stored procedures / views / triggers (full text or file refs, tables touched)
B. Coverage ledger (every candidate -> traced / merged / excluded / unresolved)
C. Model vs migration drift details
D. Suggested runtime data to collect in Azure (Query Store, Query Performance Insight, sys.dm_db_index_usage_stats, missing-index DMVs), as a list of what to pull, not conclusions
```

## Rules that keep the Excel export working

`scripts/build_workbook.py` recognises each table by its header text, so keep the headers above verbatim and follow these conventions:

- Every table is a real markdown pipe table (header row, `|---|` separator, one row per fact). No prose-style tables.
- Heading format: schema tables use `### \`T-<Table>\``; workflow detail uses `### \`W-012\` <trigger>`. The script copies the table or workflow ID from the nearest heading into each row.
- In the workflow-access table, separate multiple columns with commas (`Id, Total, Status`). Use `*` for "all columns".
- The `Index coverage` cell uses `Column: LABEL (IX-id)` entries separated by `;`, e.g. `CustomerId: COVERED-SEEK (IX-Orders_CustomerId); Status: NOT-COVERED`. Each filter, join or sort column should have an entry so it lands in the Excel coverage column.
- Put one workflow-access row per table per distinct query, and repeat the `Workflow` heading context rather than merging workflows into one table.
- Escape a literal pipe inside a cell as `\|`.

## Splitting a large report

If the report would exceed ~3,000 lines, write `docs/db-audit/00-index.md` (sections 0, 2, 3, 5, 8) and put sections 1, 4, 6 and the appendices in separate files grouped by table or feature area. Keep every ID unchanged so cross-references still resolve.
