# db-usage-audit

A tool-agnostic AI coding agent skill that works in **Claude Code, GitHub Copilot, Cursor, Windsurf, Cline and Codex-style CLIs**. It scans a **.NET / Entity Framework Core (code-first) + SQL Server** backend and writes a complete, evidence-backed report of how the application uses its database:

- every **table, column, index, foreign key** and constraint
- every **workflow** (API endpoint, background job, queue or Azure Function trigger, startup task, migration, raw SQL)
- for each workflow, exactly which **tables and columns** it reads, filters, joins, sorts and writes
- whether each of those columns is **covered by an index**

The report records facts only, with `file:line` evidence and no recommendations. It is built to be the input for a second agent (or a person) that plans DB optimization: splitting tables, normalizing, adding or removing indexes.

Outputs:

| File | Purpose |
|---|---|
| `DB_USAGE_REPORT.md` | The full report, for agents and detailed review |
| `DB_USAGE_REPORT.xlsx` | The same data as sheets you can filter and pivot |

## Install

The skill itself (`SKILL.md`, `references/`, `scripts/`) is plain markdown and Python, so it is the same for every tool. `install.py` copies it into your project and writes the small pointer file each tool uses to find it.

```bash
git clone <this-repo-url> db-usage-audit
cd db-usage-audit

# Pick your tool(s); repeat --tool for several, or use --tool all
python3 install.py --target /path/to/your-backend-repo --tool cursor
python3 install.py --target /path/to/your-backend-repo --tool copilot
python3 install.py --target /path/to/your-backend-repo --tool claude
```

| `--tool` | What it creates in your project | How you run it |
|---|---|---|
| `claude` | `.claude/skills/db-usage-audit/` | Ask in Claude Code (new session), or `/db-usage-audit` |
| `cursor` | `.cursor/rules/db-usage-audit.mdc` + `.db-usage-audit/` | Cursor **Agent** mode; the rule is picked up by its description, or `@db-usage-audit` |
| `copilot` | `.github/prompts/db-usage-audit.prompt.md` + `.db-usage-audit/` | Copilot Chat in **Agent** mode, type `/db-usage-audit` |
| `windsurf` | `.windsurf/rules/db-usage-audit.md` + `.db-usage-audit/` | Cascade (agent) picks it up from the rule description |
| `cline` | `.clinerules/db-usage-audit.md` + `.db-usage-audit/` | Ask Cline |
| `agents` | a section in `AGENTS.md` + `.db-usage-audit/` | Codex, Gemini CLI, Copilot coding agent and any tool that reads `AGENTS.md` |
| `all` | all of the above | |

The installer is safe to re-run: files it generated are refreshed, your hand-written files are never overwritten unless you pass `--force`, and an existing `AGENTS.md` keeps its content (the skill goes in a marked section).

**Any other tool:** copy this repo's `SKILL.md`, `references/` and `scripts/` into your project (for example `.db-usage-audit/`) and tell the agent: *"Follow the instructions in `.db-usage-audit/SKILL.md` exactly to produce the DB usage report."*

The adapter file formats follow each tool's documented conventions but I have only tested the installer's output files and idempotence, not each IDE. If a tool does not discover the rule, use the "any other tool" prompt above; the skill content works the same way.

### Requirements

- An AI coding tool **in agent mode**: it must be able to read and search the repo, create files, and run shell commands. Plain chat or autocomplete modes are not enough.
- Python 3.8+ and the `openpyxl` package, used by two helper scripts. **You do not need to set these up:** the skill checks for them at the start of each run and installs `openpyxl` itself (user-level `pip install --user`). If Python is missing it asks before installing, and if you decline it falls back to grep-based discovery and CSV output instead of `.xlsx`.

## How to use

Open your AI coding tool in **agent mode** in the backend repo and ask for it in plain language (or use the slash command from the install table). The prompts below work the same in every tool. If your tool does not auto-select the rule, start with: "Follow the instructions in `.db-usage-audit/SKILL.md`".

**Full audit (the usual one):**

```text
Run the db-usage-audit skill on this repo. Scan the whole backend, trace every
workflow (API endpoints, background jobs, queue/function triggers, startup tasks)
to the tables and columns it touches, check index coverage, and write
DB_USAGE_REPORT.md plus DB_USAGE_REPORT.xlsx at the repo root.
```

**Large repo or several apps:**

```text
Use db-usage-audit on the /src/Api and /src/Worker projects only. Our DbContext
is AppDbContext. Split the report into docs/db-audit/ if it gets big, and use
subagents to trace workflows in parallel.
```

**Rebuild only the Excel file from an existing report:**

```text
Rebuild DB_USAGE_REPORT.xlsx from DB_USAGE_REPORT.md using the db-usage-audit
build_workbook.py script.
```

**Targeted run:**

```text
Use db-usage-audit, but only for workflows touching the Orders and OrderLines tables.
```

A run on a real backend can take a long time. Intermediate files go in `.db-audit-work/` (add it to `.gitignore`).

## Reading the results

Check the **completeness gate** at the top of `DB_USAGE_REPORT.md` first: counts of tables and workflows, unresolved items, and which checks passed. Review the **Unresolved** section (dynamic SQL, generic repositories, stored procedures whose bodies are not in the repo) before relying on the report.

Excel sheets:

| Sheet | One row per |
|---|---|
| `Workflows` | workflow |
| `Workflow_Access` | workflow × table query: operation, columns read, filter, join, sort and written columns, paging, index coverage, evidence |
| `Access_Flat` | workflow × table × role × column, with index-coverage label (best sheet for pivot tables) |
| `Columns` | column of every table |
| `Table_Indexes`, `Index_Inventory` | index, per table and overall, with the workflows that use it |
| `Foreign_Keys` | foreign key |
| `Table_to_Workflows`, `Column_to_Workflows` | table or column and the workflows that touch it |
| `Unresolved` | item that could not be traced |

Index coverage labels: `COVERED-SEEK`, `COVERED-PARTIAL`, `COVERED-INCLUDE`, `NOT-COVERED`, `UNKNOWN`. Example: filter `Role = FILTER` and `Index coverage = NOT-COVERED` in `Access_Flat` to list every unindexed filter column and the workflow behind it.

Hand `DB_USAGE_REPORT.md` to your optimization-planning agent. The `.xlsx` is for people.

## How it works

1. **Preflight:** finds Python, installs `openpyxl` if needed.
2. **Orient and discover:** finds the `DbContext`s and runs `scripts/scan_inventory.py`, a regex sweep that lists candidate entry points and data-access sites.
3. **Schema:** builds the inventory from the EF model snapshot, migrations (including raw SQL inside them), Fluent config and standalone `.sql` files, and flags drift between model and migrations.
4. **Workflows:** enumerates endpoints, jobs, consumers, startup tasks, Identity flows and per-request middleware.
5. **Tracing:** follows each workflow through services and repositories to the queries, including implicit effects (global query filters, audit columns, cascades, `Update()` writing all columns). See `references/ef-core-tracing-guide.md`.
6. **Index coverage:** matches every filter, join and sort column against the real indexes.
7. **Cross-references and a completeness gate:** table → workflows, column → workflows, index → workflows, and a coverage ledger so nothing is silently dropped.
8. **Excel export:** `scripts/build_workbook.py` converts the report's tables into the workbook.

## Limits

- **Static analysis only.** It reads source, migrations and `.sql` files and never connects to a database. Traffic, row counts and runtime cost are not in the report. The report ends with a list of what to pull from Azure (Query Store, Query Performance Insight, index usage stats) to add that.
- Dynamic or generic data access can leave items unresolved. They are listed explicitly, not guessed.
- Built for .NET, EF Core and SQL Server. Other stacks need changes to the discovery patterns and tracing guide.

## Layout

```text
install.py                            Installs the skill into a project for Claude, Cursor, Copilot, Windsurf, Cline, AGENTS.md
SKILL.md                              The skill: phases and rules the agent follows
references/report-template.md         Exact report structure (the Excel export depends on it)
references/ef-core-tracing-guide.md   How EF Core code maps to tables and columns
scripts/scan_inventory.py             Discovery sweep for candidate entry points
scripts/build_workbook.py             Markdown report -> Excel workbook
```
