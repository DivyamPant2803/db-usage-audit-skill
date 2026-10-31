# Tracing EF Core code to tables and columns

Read this before Phase 4. It lists how EF Core constructs map to SQL, and the implicit behaviors that touch columns the code never mentions. Contents: 1 Resolving the call chain, 2 LINQ to columns, 3 Writes and change tracking, 4 Implicit column and table access, 5 Non-EF data access, 6 Dynamic and generic patterns.

## 1. Resolving the call chain

- Start at the entry point and follow every call that can reach a `DbContext`, `DbSet<T>`, `IQueryable<T>`, `IRepository<T>`, `SqlConnection` or Dapper `IDbConnection`.
- Interfaces: find the DI registration (`AddScoped<IFoo, Foo>`, assembly scanning like `Scrutor`, `AddMediatR`) to pick the implementation. If there are several, trace all.
- MediatR: `IRequestHandler<TReq,TRes>` for the request type; also pipeline behaviors (validation, transaction, caching, audit) which may query or write.
- Repository/specification patterns: the query is composed in several places. Merge the base spec, the caller's predicate, and any `ApplyQuery` extension to see the final shape.
- Extension methods on `IQueryable` (e.g. `.WhereActive()`, `.ForTenant()`) hide predicates. Open them.
- Async/event hops: domain events, `SaveChanges` interceptors, outbox publishers, `IHostedService` consumers triggered later are separate workflows; link them ("triggers W-0xx").
- `Task.Run`, `Parallel.ForEach`, and `IAsyncEnumerable` loops are still the same workflow but note per-iteration DB calls as "inside loop".

## 2. LINQ to columns

| Code | Reads / uses |
|---|---|
| `db.Orders.ToList()` (no `Select`) | all mapped columns of Orders (`*`) |
| `.Select(o => new { o.Id, o.Total })` | only Id, Total (plus columns used in predicates). Mark the rest unread |
| `.Where(o => o.Status == s && o.CustomerId == c)` | filter columns Status, CustomerId |
| `.Where(o => o.Customer.Name == n)` | join Orders->Customers on FK; filter Customers.Name |
| `.Include(o => o.Lines)` | JOIN (or split query) to OrderLines on its FK; all OrderLines columns unless filtered include/projection |
| `.Include(...).ThenInclude(...)` | each hop is another table; list all |
| `.OrderBy(o => o.CreatedAt)` | sort column |
| `.GroupBy(o => o.CustomerId).Select(g => new { g.Key, Sum = g.Sum(x => x.Total) })` | group col CustomerId, aggregate col Total |
| `.Any()/Count()/First()/Single()` | still a read with its predicate; `Any()` reads no data columns |
| `.Contains(list)` | `IN (...)` on that column; a large list may be parameterized as OPENJSON |
| `.Skip().Take()` | paging; check an `OrderBy` exists and which column |
| `.AsNoTracking()` / `.AsNoTrackingWithIdentityResolution()` | tracking = off |
| `.AsSplitQuery()` | multiple queries, same tables |
| `.FromSqlRaw/FromSqlInterpolated` | parse the SQL text (see section 5); EF may compose further LINQ on top |
| `.ToLower()`, `.Date`, `.Year`, `string.Contains` (-> `LIKE '%x%'`), `EF.Functions.Like` | non-sargable or function-on-column predicate: record |
| `.Distinct()`, `.Union()`, `.Concat()` | additional sort/hash work; include all columns involved |

Count each distinct query in a workflow separately, since one workflow may hit the same table with different shapes.

## 3. Writes and change tracking

- `Add`/`AddRange`/`AddAsync`: INSERT; columns = all non-database-generated columns of the entity (plus FK shadow columns). List defaults and identity columns separately.
- Tracked entity modified, then `SaveChanges`: UPDATE sets **only changed properties** (compare via snapshot). Find which property assignments occur in the workflow.
- `Update(entity)` / `Attach` + `State = Modified` on a detached entity: UPDATE sets **all** non-key columns. Record as "all columns written" because this is a classic wide-write source.
- `Remove`: DELETE; check cascade behavior in the schema (child tables deleted too, or SET NULL), and soft-delete conversions (`IsDeleted = true` becomes an UPDATE via interceptor).
- `ExecuteUpdate` / `ExecuteDelete` (EF 7+): set-based; columns from `SetProperty` calls and predicate.
- `AddRange` + `SaveChanges` and `EFCore.BulkExtensions`/`SqlBulkCopy`: BULK; note batch size if visible.
- Upsert patterns (`Any` then `Add` or `Update`; `MERGE` raw SQL): record both the read and the write.
- Concurrency tokens (`[Timestamp]`, `rowversion`, `[ConcurrencyCheck]`): the UPDATE/DELETE `WHERE` includes the token column; list it as a predicate.
- Transactions: `BeginTransaction`, `TransactionScope`, `CreateExecutionStrategy`, or a single `SaveChanges` (which is atomic). List the tables written inside one transaction. Isolation level if set.
- `SaveChanges` count per request: more than one in a workflow means separate transactions unless wrapped.

## 4. Implicit column and table access

Check each of these in the model/context and attribute them to every workflow that triggers them:

- **Global query filters** (`HasQueryFilter`): soft delete (`IsDeleted`), tenant id (`TenantId`), `IsActive`. Every read of that entity includes the predicate; add the column to filter columns. `IgnoreQueryFilters()` removes it.
- **Audit columns**: overridden `SaveChanges`/`SaveChangesAsync`, `SaveChangesInterceptor`, or base entity setters populating `CreatedAt/By`, `UpdatedAt/By`. Add to columns written on INSERT/UPDATE. Audit-log tables written by an interceptor are extra table writes.
- **Shadow properties** (`Property<DateTime>("LastUpdated")`) and FK shadow columns exist in the snapshot but not on the C# class.
- **TPH inheritance**: all subtypes share one table with a discriminator column; queries on a subtype filter on the discriminator. TPT/TPC: joins or unions across tables.
- **Owned types / table splitting / complex types / JSON columns**: columns live in the owner's table (or JSON column); mapping `HasColumnName` may differ.
- **Value converters**: the CLR property maps to a column of a different type; conversion can make predicates non-sargable.
- **Many-to-many skip navigations**: an implicit join table exists in the snapshot with no C# class.
- **Temporal tables**: history table is written implicitly on every UPDATE/DELETE of the main table.
- **Computed/stored columns, triggers, DB defaults**: columns the DB writes or side effects of a write (find triggers in migrations' raw SQL).
- **Identity/ASP.NET Identity**: `UserManager`/`SignInManager`/`RoleManager` calls hit AspNetUsers, AspNetUserRoles, AspNetUserClaims, AspNetUserTokens, etc. Trace login, token refresh, password change, role checks as workflows.
- **Per-request middleware**: authorization handlers, permission lookups, tenant resolvers, request-logging that persist rows. These are attributed to every endpoint behind them; record once as a shared "cross-cutting" workflow and reference it from endpoints.
- **Lazy loading proxies** (`UseLazyLoadingProxies`): any navigation property access can issue a query. Treat navigation reads in loops as reads of the related table.
- **Migrations at startup** (`Database.Migrate()`) read `__EFMigrationsHistory`.

## 5. Non-EF data access

- Dapper (`Query<T>`, `QueryAsync`, `Execute`), `SqlCommand`, `SqlDataAdapter`, `FromSqlRaw`, `ExecuteSqlRaw`, `Database.SqlQuery<T>`: read the SQL string. Parse tables, selected columns (`*` counts as all), `WHERE`, `JOIN ... ON`, `ORDER BY`, `GROUP BY`, `INSERT` column lists, `UPDATE SET` lists.
- Interpolated or concatenated SQL: capture the template and list possible variants; if the table or column is variable, mark unresolved.
- Stored procedures (`EXEC`, `CommandType.StoredProcedure`, `FromSqlRaw("EXEC ...")`): find the proc definition in migrations/.sql and trace *inside* it. If the definition is not in the repo, record the proc name as unresolved ("body not in repo").
- Views: a read of a view reads its underlying tables; find the view definition and list base tables/columns.
- Functions (TVFs, scalar UDFs), `HasDbFunction`: same, trace the body.
- `SqlBulkCopy` destinations and `OPENJSON` / table-valued parameters count as writes/reads of their target tables.

## 6. Dynamic and generic patterns

- Generic repository `GetAll(Expression<Func<T,bool>> filter, string includes)`: trace every **caller** and record the predicate and includes each caller passes. The repository method itself is not the workflow; its callers are.
- Dynamic sorting/filtering from query-string (`OrderBy(sortField)`, `System.Linq.Dynamic.Core`, OData, `Specification` built from request DTOs): list the allowed fields (look for whitelists or DTO properties) as possible filter/sort columns, marked `UNKNOWN` coverage with the candidate set.
- OData / GraphQL / `$filter` endpoints expose many columns at once; record the entity's full queryable surface and mark coverage per column.
- Reflection or convention-based registration: grep for the registration mechanism and enumerate manually; never assume the scan found them all.

When in doubt, put it in the Unresolved table with what you know. Completeness means nothing is silently skipped, not that everything is resolved.
