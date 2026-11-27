# Daily Report

Summary of what was built across every session on this project, from
initial scaffold to the completed evaluation harness.

## What was built

**Phase 1 — core foundation.** Project scaffold (`pyproject.toml`, MIT
license, package layout), `config.py` (pricing models, budget, agent
limits), `schema_catalog.py` (schema introspection over a live DuckDB
connection), a deterministic sample warehouse generator (`customers`,
`orders`, a hive-partitioned `events`), the pluggable `LLMProvider`
interface plus `ScriptedProvider` (a deterministic, no-API-key provider),
`sql/validator.py` (single-statement, `SELECT`-only, table-allowlist,
`LIMIT`-enforcing safety contract), `audit.py` (a JSON Lines trace
recorder), and a CLI (`init-warehouse`, `inspect-schema`, `validate`,
`ask`) wiring all of it into one draft/validate/approve/execute flow.

**Phase 2 — retrieval and real self-correction.** `retrieval.py`: a local
TF-IDF/cosine-similarity schema retriever (no network call, no embedding
API) that narrows the catalog to a question's most relevant tables and
columns, exposed as both a library and a `retrieve` CLI command. The
draft/validate/correct loop was extracted into `agent.run_agent_loop`,
independent of the CLI, and gained a real `EXPLAIN` check against the
target engine inside the retry loop, so an engine-level error (not just a
parser-level one) now triggers a correction instead of crashing.
`ScriptedProvider` grew to cover aggregates, a relative-date time filter,
a window function, and a second join pattern.

**Phase 3 — the scan-cost guard for real.** `cost_estimate.py` replaced a
placeholder ("sum every referenced table's file size") with a
partition-and-column-pruning-aware estimate: it parses the query's own
`WHERE` clause to restrict `events`' partition scan to the directories a
filter can't rule out, and parses the `SELECT`/`WHERE`/`GROUP BY`/`ORDER
BY` to charge only for the columns actually referenced, reading real
Parquet row-group metadata rather than guessing. Both prunings fail safe
(ambiguous references charge for more bytes, never fewer). `BudgetConfig`/
`PricingModel` selection was wired into the CLI (`--pricing-model`,
`--max-bytes-scanned`, `--max-cost-usd` on both `ask` and the new
`estimate-cost` command).

**Phase 4 — multi-dialect execution.** `engines/base.py` defines the
narrow `Connection`/`Cursor` protocol the agent loop needs and a
normalized `EngineExecutionError`; `engines/athena_engine.py` and
`engines/bigquery_engine.py` wrap `pyathena` and `google-cloud-bigquery`
(both optional extras) behind that protocol, including BigQuery's
dry-run-as-`EXPLAIN` special case since BigQuery has no `EXPLAIN`
statement of its own. `ask --dialect` can now target `duckdb` (the
bundled demo), `athena`, or `bigquery`; non-`duckdb` dialects always
require interactive approval, and the scan-cost guard (which sizes local
Parquet files) is skipped for them in favor of the `EXPLAIN`/dry-run plan
as the cost signal shown to the approver. Verified by hand against the
real `pyathena` driver (placeholder AWS credentials) in a scratch venv.

**Phase 5 — the evaluation harness.** Two additions:

- Ambiguity detection in `ScriptedProvider`: six recognizable-but-
  underspecified phrasings ("top performers", "recent activity", "how is
  the business doing", "last month's numbers", "customer activity
  summary", "our growth") now raise `ClarificationNeededError` — carrying
  the ambiguity and concrete rephrasings that would resolve it — instead
  of silently picking an interpretation. `run_agent_loop` and `ask` both
  handle this as a distinct outcome from "no rule matched at all."
- `evaluation/benchmark.py` + `evaluation/runner.py`: a fixed 44-question
  benchmark spanning every `ScriptedProvider` rule (several independent
  phrasings each), the six ambiguous questions above, and seven
  questions about data the warehouse genuinely doesn't have (no
  `products`/`employees` table, no churn/forecast metric). Each
  "success" question carries its own `expected_sql`, authored
  independently of `ScriptedProvider`, so scoring checks the agent's
  answer against a ground truth rather than against itself. The new
  `benchmark` CLI command runs every question through the real
  `run_agent_loop` against the local warehouse and reports pass/fail by
  category and difficulty, mean correction attempts, and total estimated
  cost; `--category` filters, `--output` writes the full per-question
  JSON report.

## Test results

211 tests pass (`pytest`), including a full re-run against a from-scratch
clean checkout (`python3 -m venv`, `pip install -e .`, every README
quick-start command run verbatim) to confirm the public install path
actually works, not just the development checkout.

## Known limitations

- `ScriptedProvider` is a fixed keyword-matched rule table, not a general
  text-to-SQL model — it only answers the specific phrasings the
  benchmark and tests exercise. The retrieved schema context and
  correction-driven retries are real, exercised machinery, but nothing
  downstream reads them semantically yet; that payoff lands once a real
  LLM provider is plugged in behind `LLMProvider`.
- The Athena/BigQuery adapters are exercised against the real drivers
  with placeholder credentials (confirming error paths and the request
  shape are correct) but not against a live AWS/GCP account, since no
  cloud credentials are available in this environment.
- The scan-cost guard only estimates bytes for the local DuckDB
  warehouse's Parquet files; against a real Athena/BigQuery account it
  is intentionally skipped in favor of that engine's own `EXPLAIN`/
  dry-run estimate.
- The benchmark's "ambiguous" and "unanswerable" categories are scored by
  outcome kind (did the agent correctly decline), not by exact wording of
  the clarification message.

## Future ideas

- A real local-model `LLMProvider` (e.g. wrapping a small Ollama model)
  so the benchmark can also measure genuine natural-language
  understanding, not just pipeline integrity against a fixed rule table.
- Exercising the Athena/BigQuery adapters against live accounts if cloud
  credentials ever become available.
- Expanding the benchmark with multi-table ambiguity cases (e.g. a
  question that's answerable but requires picking between two valid
  joins) once a non-scripted provider makes that distinction meaningful.
