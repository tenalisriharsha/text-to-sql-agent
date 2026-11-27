# Verification report

All steps below were run in a fresh clone outside the repository
(`git clone` of the working tree at commit `666673f`, branch
`night/2026-11-27`), in a new Python 3.15.2 virtual environment, with no
pre-existing `.venv` or `data/` carried over.

## First-use check

Commands run exactly as given in the README quick start, in order, in the
fresh clone:

```bash
git clone <repo> text-to-sql-agent   # done via local clone, see note above
cd text-to-sql-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

text-to-sql-agent init-warehouse
text-to-sql-agent ask "how many customers" --yes
```

- `python3 -m venv .venv` — expected: creates a venv. Observed: exit 0,
  `.venv/` created.
- `pip install -e .` — expected: installs `duckdb`, `sqlglot`, `pyarrow`,
  `click`, `faker` and the `text-to-sql-agent` console script. Observed:
  all five dependencies resolved and installed, editable build succeeded,
  `Successfully installed ... text-to-sql-agent-0.1.0`.
- `text-to-sql-agent init-warehouse` — expected (per README): generates
  `customers`, `orders`, and a partitioned `events` click-stream under
  `data/warehouse/`. Observed: printed paths for `customers.parquet`,
  `orders.parquet`, and `events/event_date=.../part-0.parquet`; the
  directory was actually created with a `customers.parquet`, an
  `orders.parquet`, and an `events/` tree partitioned into 30
  `event_date=YYYY-MM-DD` directories. Matches.
- `text-to-sql-agent ask "how many customers" --yes` — expected: answers
  the question without a confirmation prompt (per `--yes`). Observed:
  printed an estimated-scan-cost line, the drafted SQL
  (`SELECT COUNT(*) AS customer_count FROM customers LIMIT 1000`), and
  the result `customer_count | 200`. Matches.
- Extra check (not a literal quick-start command, but a claim made in the
  prose immediately below it): running the same question without `--yes`
  shows a `Proceed with execution? [y/N]:` prompt; answering `n` aborts
  cleanly with exit 0. Matches the README's description of the
  human-approval gate.

No step failed, printed anything unexpected, or needed an undocumented
fix.

Result: PASS

## Headline feature run

This project's own description of its main feature: given a plain-English
question, retrieve relevant schema, draft SQL, validate it against the
safety contract, run `EXPLAIN`, estimate scan cost, get human approval,
execute, and record every step to an audit trace. The README states the
bundled `LLMProvider` (`ScriptedProvider`) is a deterministic pattern
matcher with "no model or network access" by design — swapping in a real
LLM is called out as "the natural next step for anyone extending this",
not something this repository implements. There is accordingly no LLM
call path in the shipped code to exercise with `claude -p`; the pipeline
below is the real, unmocked feature as the project ships it.

Commands run for real against the local DuckDB sample warehouse (no part
of the project's own code was mocked or stubbed):

```bash
text-to-sql-agent retrieve "total revenue by status"
text-to-sql-agent ask "show me orders with customer details" --yes --audit-log trace.jsonl
text-to-sql-agent ask "who are our top performers" --yes
text-to-sql-agent ask "list all employees" --yes
text-to-sql-agent validate "SELECT * FROM orders"
text-to-sql-agent validate "DROP TABLE orders"
text-to-sql-agent estimate-cost "SELECT * FROM events WHERE event_date = '2026-11-20'" --pricing-model bigquery
text-to-sql-agent ask "how many customers" --target-dialect bigquery
text-to-sql-agent benchmark --output report.json
```

- `retrieve` — expected: ranks tables by relevance to the question and
  prints the narrowed schema context. Observed: ranked `orders` highest
  (0.1857), then `events` (0.0187), then `customers` (0.0000), with full
  column descriptions for the retrieved tables. Matches.
- `ask ... --audit-log trace.jsonl` (join question) — expected: a joined
  `orders`/`customers` query, executed, with every step recorded to the
  JSONL trace. Observed: drafted
  `SELECT o.order_id, c.name, o.order_date, o.total_amount FROM orders AS o JOIN customers AS c ON o.customer_id = c.customer_id ORDER BY o.order_date DESC LIMIT 1000`,
  1000 rows returned; `trace.jsonl` contained ordered
  `question_received` → `schema_retrieved` → `draft` → `validation` →
  `cost_estimate` → `approval` → `explain` → `execution_result` → `final`
  entries, each with a timestamp. Matches.
- `ask "who are our top performers" --yes` — expected: declined as
  ambiguous rather than guessed at (per README's ambiguity-detection
  description). Observed: exit 1,
  `question is ambiguous: 'performer' isn't a metric -- it could mean highest revenue, most orders, or most events, and over customers or orders.`
  Matches.
- `ask "list all employees" --yes` — expected: declined as unanswerable
  (no employees table in this warehouse). Observed: exit 1,
  `could not answer the question: no scripted rule matches question: 'list all employees'`.
  Matches the described "unrecognized question --> declined as
  unanswerable" behavior.
- `validate "SELECT * FROM orders"` — expected: valid, with an enforced
  `LIMIT`. Observed: `VALID`, `SELECT * FROM orders LIMIT 1000`. Matches.
- `validate "DROP TABLE orders"` — expected: rejected (DDL not allowed).
  Observed: `REJECTED`, `only SELECT queries (including
  UNION/EXCEPT/INTERSECT) are allowed, got Drop`, exit 1. Matches.
- `estimate-cost ... --pricing-model bigquery` — expected: partition
  pruning restricts the scan to the one matching `event_date` partition.
  Observed: `total: 4078 bytes (~$0.000060, bigquery pricing)`,
  `partitions scanned: 1 (event_date=2026-11-20)`. Matches.
- `ask ... --target-dialect bigquery` — expected: prints the translated
  query instead of executing it. Observed: printed
  `SELECT COUNT(*) AS customer_count FROM customers LIMIT 1000` with no
  execution output. Matches.
- `benchmark --output report.json` — expected: runs all 44 benchmark
  questions through the same agent loop `ask` uses and reports pass/fail
  by category and difficulty. Observed:
  `44/44 passed (100%), mean attempts 1.00, est. cost $0.0015 (athena pricing)`,
  with every category (count, aggregate, groupby, join, top_n,
  time_filter, window, ambiguous, unanswerable) and every difficulty
  level at full pass, and `report.json` written with the per-question
  breakdown. Matches.

Athena and BigQuery *execution* (as opposed to the local `--target-dialect`
query-translation path exercised above) were not run: they require a real
AWS or GCP account, and only dummy credentials are available in this
environment, so hitting a real Athena/BigQuery endpoint is
NOT_POSSIBLE here. This is incidental to the headline feature, which is
the local draft/validate/estimate/execute/audit pipeline — that ran
end-to-end for real above.

Result: PASS

## Test suite with development tools

The project documents exactly one set of development tools: the `dev`
extra (`pytest>=8.0.0`, `pytest-cov>=5.0.0`), which is also all that the
project's own CI workflow (`.github/workflows/ci.yml`) installs and runs
(`pip install -e ".[dev]"` then `pytest`). No linter or type checker
(ruff, mypy, flake8, black, etc.) is referenced anywhere in the
repository's config files, CI workflow, or docs, so none was installed.

```bash
pip install -e ".[dev]"
pytest -q
pytest -q --cov=text_to_sql_agent --cov-report=term-missing
```

- `pip install -e ".[dev]"` — expected: installs `pytest` and
  `pytest-cov` on top of the base install. Observed: exit 0,
  `Successfully installed coverage-7.17.5 ... pytest-9.2.0 pytest-cov-7.1.0 text-to-sql-agent-0.1.0`.
- `pytest -q` — expected: the full suite passes (same command CI runs).
  Observed: `287 passed in 11.80s`, exit 0.
- `pytest -q --cov=text_to_sql_agent --cov-report=term-missing` —
  expected: coverage report runs alongside the suite. Observed:
  `287 passed`, overall coverage `96%` (`TOTAL 1163 43 96%`), exit 0.

Result: PASS
