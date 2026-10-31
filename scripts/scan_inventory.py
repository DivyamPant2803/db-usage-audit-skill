#!/usr/bin/env python3
"""Deterministic discovery sweep for the db-usage-audit skill.

Walks a .NET repository and lists candidate DB-related entry points and
data-access sites. It is a regex net, not a parser: it seeds the coverage
ledger and will both miss unusual patterns and over-report.

Usage: scan_inventory.py <repo-root> [--out candidates.json]
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

SKIP_DIRS = {"bin", "obj", "node_modules", ".git", ".vs", ".idea", "packages",
             "dist", "build", ".db-audit-work", "TestResults"}

# (category, regex) applied to .cs files line by line.
CS_PATTERNS = [
    ("dbset", re.compile(r"\bDbSet<\s*([\w.]+)\s*>\s+(\w+)")),
    ("dbcontext_class", re.compile(r"class\s+(\w+)\s*:\s*[\w.<>, ]*\b(?:DbContext|IdentityDbContext\w*)\b")),
    ("entity_type_configuration", re.compile(r"class\s+(\w+)\s*:\s*IEntityTypeConfiguration<\s*([\w.]+)\s*>")),
    ("model_snapshot", re.compile(r"class\s+(\w+ModelSnapshot)\b")),
    ("migration_class", re.compile(r"class\s+(\w+)\s*:\s*Migration\b")),
    ("migration_raw_sql", re.compile(r"migrationBuilder\s*\.\s*Sql\s*\(")),
    ("global_query_filter", re.compile(r"\.HasQueryFilter\s*\(")),
    ("savechanges_override", re.compile(r"override\s+(?:async\s+)?(?:Task<int>|int)\s+SaveChanges(?:Async)?\s*\(")),
    ("savechanges_interceptor", re.compile(r"class\s+(\w+)\s*:\s*[\w.<>, ]*SaveChangesInterceptor\b")),
    ("http_action", re.compile(r"\[\s*(Http(?:Get|Post|Put|Delete|Patch))(?:\s*\(\s*\"([^\"]*)\"\s*\))?\s*\]")),
    ("controller_class", re.compile(r"class\s+(\w+Controller)\b")),
    ("minimal_api", re.compile(r"\.\s*Map(Get|Post|Put|Delete|Patch|Methods)\s*\(\s*\"([^\"]*)\"")),
    ("grpc_service", re.compile(r"class\s+(\w+)\s*:\s*\w+\.\w+Base\b")),
    ("signalr_hub", re.compile(r"class\s+(\w+)\s*:\s*Hub(?:<[^>]+>)?\b")),
    ("hosted_service", re.compile(r"class\s+(\w+)\s*:\s*[\w.<>, ]*\b(?:BackgroundService|IHostedService)\b")),
    ("hangfire_job", re.compile(r"\b(?:RecurringJob|BackgroundJob)\s*\.\s*(?:AddOrUpdate|Enqueue|Schedule)\b")),
    ("quartz_job", re.compile(r"class\s+(\w+)\s*:\s*[\w.<>, ]*\bIJob\b")),
    ("azure_function", re.compile(r"\[\s*Function(?:Name)?\s*\(\s*\"([^\"]+)\"")),
    ("azure_function_trigger", re.compile(r"\[\s*(\w*Trigger)\s*(?:\(|\])")),
    ("mediatr_handler", re.compile(r"class\s+(\w+)\s*:\s*[\w.<>, ]*\bIRequestHandler<")),
    ("mediatr_pipeline", re.compile(r"class\s+(\w+)\s*:\s*[\w.<>, ]*\bIPipelineBehavior<")),
    ("message_consumer", re.compile(r"class\s+(\w+)\s*:\s*[\w.<>, ]*\b(?:IConsumer<|IMessageHandler<|IHandleMessages<|ServiceBusProcessor)")),
    ("raw_sql_efcore", re.compile(r"\b(FromSqlRaw|FromSqlInterpolated|FromSql|ExecuteSqlRaw|ExecuteSqlInterpolated|ExecuteSql|SqlQueryRaw|SqlQuery)\b")),
    ("ado_net", re.compile(r"\b(SqlConnection|SqlCommand|SqlDataAdapter|SqlBulkCopy)\b")),
    ("dapper", re.compile(r"\.\s*(Query|QueryAsync|QueryFirst\w*|QuerySingle\w*|Execute|ExecuteAsync|ExecuteScalar\w*|QueryMultiple\w*)\s*(?:<[^>]*>)?\s*\(")),
    ("stored_proc", re.compile(r"CommandType\.StoredProcedure|\bEXEC(?:UTE)?\s+\[?\w+\]?\.?\[?\w+|StoredProcedure\s*\(", re.I)),
    ("savechanges_call", re.compile(r"\.\s*SaveChanges(?:Async)?\s*\(")),
    ("execute_update_delete", re.compile(r"\.\s*(ExecuteUpdate|ExecuteDelete)(?:Async)?\s*\(")),
    ("bulk_extension", re.compile(r"\.\s*(BulkInsert|BulkUpdate|BulkDelete|BulkMerge|BulkSaveChanges)(?:Async)?\s*\(")),
    ("transaction", re.compile(r"\b(BeginTransaction(?:Async)?|TransactionScope|CreateExecutionStrategy)\b")),
    ("dbcontext_injection", re.compile(r"\b(\w*DbContext\w*)\s+_?\w+\s*[,)]")),
    ("lazy_loading", re.compile(r"UseLazyLoadingProxies")),
    ("migrate_at_startup", re.compile(r"\.\s*Database\s*\.\s*(Migrate|EnsureCreated)(?:Async)?\s*\(")),
    ("identity_manager", re.compile(r"\b(UserManager|SignInManager|RoleManager)<")),
]

SQL_HINTS = re.compile(r"\b(CREATE|ALTER)\s+(PROCEDURE|PROC|VIEW|FUNCTION|TRIGGER|INDEX|TABLE)\b", re.I)


def iter_files(root, exts):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.lower().endswith(exts):
                yield os.path.join(dirpath, name)


def scan_cs(path, root, out):
    rel = os.path.relpath(path, root)
    is_test = bool(re.search(r"(^|[/\\])[\w.]*Tests?[\w.]*[/\\]", rel)) or rel.endswith("Tests.cs")
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                stripped = line.strip()
                if stripped.startswith("//"):
                    continue
                for category, rx in CS_PATTERNS:
                    m = rx.search(line)
                    if m:
                        out.append({
                            "category": category,
                            "name": " ".join(g for g in m.groups() if g) or m.group(0),
                            "file": rel,
                            "line": lineno,
                            "snippet": stripped[:200],
                            "in_tests": is_test,
                        })
    except OSError as e:
        print(f"warn: cannot read {rel}: {e}", file=sys.stderr)


def scan_sql(path, root, out):
    rel = os.path.relpath(path, root)
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError as e:
        print(f"warn: cannot read {rel}: {e}", file=sys.stderr)
        return
    objects = [f"{m.group(1).upper()} {m.group(2).upper()}" for m in SQL_HINTS.finditer(text)]
    out.append({
        "category": "sql_file",
        "name": os.path.basename(path),
        "file": rel,
        "line": 1,
        "snippet": ", ".join(sorted(set(objects)))[:200],
        "in_tests": False,
    })


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root")
    ap.add_argument("--out", default=".db-audit-work/candidates.json")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.exit(f"error: {root} is not a directory")

    results = []
    for p in iter_files(root, (".cs",)):
        scan_cs(p, root, results)
    for p in iter_files(root, (".sql",)):
        scan_sql(p, root, results)

    results.sort(key=lambda r: (r["category"], r["file"], r["line"]))
    out_path = args.out if os.path.isabs(args.out) else os.path.join(root, args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"root": root, "count": len(results), "candidates": results}, f, indent=2)

    counts = Counter(r["category"] for r in results)
    print(f"Wrote {len(results)} candidates to {out_path}")
    for cat, n in sorted(counts.items()):
        print(f"  {cat:28s} {n}")
    print("\nThis is a regex net: add anything it missed, then reconcile in ledger.md.")


if __name__ == "__main__":
    main()
