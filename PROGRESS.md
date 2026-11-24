# Progress

## Vision

An agentic natural-language-to-SQL assistant for a data warehouse. Given a
question, it:

1. Retrieves the relevant tables and columns from a schema catalog
   (embedding search over table/column descriptions and sample values).
2. Drafts a candidate SQL query with a pluggable LLM provider.
3. Validates the draft with a SQL parser (`sqlglot`): `SELECT`-only, no DDL
   or DML, no multi-statement input, an allowlist of tables, and an
   enforced `LIMIT`.
4. Runs `EXPLAIN` against the target engine and revises the query from
   parser and engine errors in a bounded self-correction loop.
5. Estimates bytes scanned (accounting for partition and column pruning)
   with a scan-cost guard that refuses, or asks for confirmation, above a
   configurable budget — with pricing models for Athena and BigQuery.
6. Writes every step (retrieved schema, drafts, validation verdicts,
   errors, final SQL, cost estimate) to an audit trace, and requires a
   human approval step before anything executes against a non-demo source.

It runs end to end on DuckDB against a generated sample warehouse
(`orders`, `customers`, `events`, with `events` partitioned Parquet), and
can emit Athena (Trino) or BigQuery dialect through `sqlglot`. An
evaluation harness scores a benchmark of 40-60 questions of rising
difficulty (joins, aggregates, window functions, time filters, ambiguous
questions) on execution accuracy, result-set match, steps taken, and cost
per question. LLM access is pluggable: a scripted deterministic policy for
tests and CI, with room for a local model or a hosted API — no paid API key
is required to run the project.

## Architecture

```
question
   |
   v
SchemaCatalog  <---- warehouse (DuckDB / Parquet introspection)
   |
   v
SchemaRetriever.retrieve(question)  -- TF-IDF cosine similarity over each
   |                                    table's/column's name, description,
   |                                    and sample values; narrows to the
   |                                    top-k tables/columns (falls back to
   |                                    the full catalog on an all-zero score)
   v
LLMProvider.draft_sql(question, schema_context, correction?)
   |
   v
validate_query()  --sqlglot AST checks--> reject (loop back with error)
   |  (single statement, SELECT-only, table allowlist, LIMIT enforced)
   v
EXPLAIN on target engine  --engine error--> reject (loop back with error)
   |
   v
scan-cost guard (bytes scanned estimate -> PricingModel -> USD)
   |  refuse above max_bytes_scanned / max_cost_usd
   |  ask for human confirmation above confirm_above_usd
   v
human approval (required unless --yes, and always for non-demo sources)
   |
   v
execute -> results
   |
   v
AuditTrace (JSON Lines: every step above, in order)
```

`SchemaRetriever` only narrows what the *provider* sees; the validator's
table allowlist is always the full catalog's table names, independent of
retrieval, so narrowing context can never narrow what a query is allowed
to reference. Everything from `LLMProvider.draft_sql` through the
`EXPLAIN` check runs inside a bounded loop (default 3 retries, in
`agent.run_agent_loop`): a validator rejection *or* an `EXPLAIN` failure
from the real target engine becomes a `CorrectionContext` fed back into
`LLMProvider.draft_sql` rather than a hard stop. Note that `ScriptedProvider`
ignores both `schema_context` and `correction` (it is a fixed keyword-to-SQL
map), so today the retrieved context and the correction-driven retry are
real, exercised machinery with nothing yet downstream that reads them
semantically -- that payoff lands once a real LLM provider is plugged in.

### Package layout

```
src/text_to_sql_agent/
  config.py          AgentConfig, BudgetConfig, PricingModel (Athena/BigQuery)
  schema_catalog.py  ColumnSchema, TableSchema, SchemaCatalog (+ DuckDB introspection)
  retrieval.py        SchemaRetriever (TF-IDF table/column ranking and narrowing)
  agent.py           run_agent_loop: draft/validate/EXPLAIN/retry, independent of the CLI
  warehouse/         generate.py (sample Parquet warehouse), load.py (register as DuckDB views)
  llm/               base.py (LLMProvider ABC, CorrectionContext), scripted.py (deterministic provider)
  sql/               validator.py (sqlglot-based validation + LIMIT enforcement), dialect.py (Athena/BigQuery translation)
  audit.py           AuditTrace (JSON Lines trace writer/reader)
  cli.py             `text-to-sql-agent init-warehouse|inspect-schema|retrieve|validate|ask`
```

## Build plan

### Phase 1 — Scaffold and core foundation (complete)
- [x] Project scaffold: `pyproject.toml`, package layout, MIT license, `.gitignore`.
- [x] `config.py`: `PricingModel` (Athena $5/TiB, BigQuery $6.25/TiB), `BudgetConfig`
      (hard ceilings + soft confirmation threshold), `AgentConfig` (retry/limit knobs).
- [x] `schema_catalog.py`: `ColumnSchema`/`TableSchema`/`SchemaCatalog`, built by
      introspecting a live DuckDB connection (types + sample values).
- [x] `warehouse/`: deterministic sample warehouse generator (`customers`,
      `orders`, hive-partitioned `events`) and a DuckDB loader that registers
      them as views with partition pruning available.
- [x] `llm/`: `LLMProvider` ABC + `CorrectionContext`, and `ScriptedProvider`,
      a deterministic keyword-matched provider for CI/offline use (no API key).
- [x] `sql/validator.py`: sqlglot-based validation — single statement,
      SELECT/UNION/EXCEPT/INTERSECT only, forbidden-construct scan, table
      allowlist, `LIMIT` injection/clamping.
- [x] `sql/dialect.py`: SQL translation between duckdb/athena(trino)/bigquery.
- [x] `audit.py`: `AuditTrace`, an append-only JSON Lines step recorder.
- [x] `cli.py`: `init-warehouse`, `inspect-schema`, `validate`, and `ask` —
      `validate` exposes the safety contract directly, and `ask` wires the
      draft/validate/correct loop, a naive scan-cost estimate, a human
      approval prompt, `EXPLAIN`, and execution together end to end.
- [x] Manually exercised every command and the correction loop against the
      sample warehouse (see commit history); independent tests land from
      the test-writing pass against this plan and the README.

### Phase 2 — Retrieval and real self-correction (complete)
- [x] `retrieval.py`: `SchemaRetriever`, a TF-IDF/cosine-similarity index
      over each table's and column's name, description, and sample values.
      This is a local, dependency-free stand-in for an embedding index --
      no network call or API key, consistent with the project's no-API-key
      requirement -- exposed both as a library (`rank_tables`,
      `rank_columns`, `retrieve`) and via the new `retrieve` CLI command.
- [x] `ask` narrows `SchemaCatalog.to_text()` to the top-k relevant
      tables/columns per question (`AgentConfig.retrieval_top_k_tables`/
      `retrieval_top_k_columns_per_table`, CLI: `--top-k-tables`/
      `--top-k-columns`) instead of passing the whole catalog -- falling
      back to the full catalog if every table scores zero. The validator's
      table allowlist is unaffected: it is always built from the full
      catalog, never the retrieved subset.
- [x] `agent.py`: extracted the draft/validate/correct loop out of `cli.py`
      into `run_agent_loop`, usable without the CLI, and added a real
      `EXPLAIN` check against the target connection *inside* the loop --
      an engine error (unknown column, type mismatch) now produces a
      `CorrectionContext` and consumes a retry, exactly like a validator
      rejection, instead of crashing unhandled after approval.
- [x] Expanded `ScriptedProvider` with 4 more rules spanning the
      benchmark's eventual difficulty curve: an aggregate (`AVG`), a
      relative-date time filter (`CURRENT_DATE - INTERVAL`), a window
      function (`SUM(...) OVER (ORDER BY ...)` running total), and a second
      join/aggregate combination (`events` x `customers`). Ambiguous
      questions are deliberately not covered here -- they need
      disambiguation logic, not more canned SQL, so they're left to the
      Phase 5 benchmark design.

### Phase 3 — Scan-cost guard for real
- [ ] Partition-pruning-aware bytes-scanned estimate (read the query's
      `WHERE` clause against `events`' partition columns) instead of the
      current naive "sum of file sizes" placeholder.
- [ ] Column-pruning-aware estimate (Parquet column statistics).
- [ ] Wire `BudgetConfig`/`PricingModel` selection into the CLI (currently
      only the Python API exposes it).

### Phase 4 — Multi-dialect execution
- [ ] Execution adapters for Athena (Trino) and BigQuery behind the same
      interface DuckDB uses, gated by the existing human-approval step.
- [ ] Non-demo-source approval is mandatory and cannot be bypassed with
      `--yes` (today `--yes` applies uniformly since only DuckDB executes).

### Phase 5 — Evaluation harness
- [ ] 40-60 question benchmark, rising difficulty (joins, aggregates,
      window functions, time filters, ambiguous questions).
- [ ] Scoring: execution accuracy, result-set match, steps taken, cost per
      question.
- [ ] A report command summarizing a benchmark run.

### Phase 6 — Polish
- [ ] Optional local-model provider.
- [ ] CI workflow running the test suite on every push.
- [ ] Packaging/distribution polish and documentation pass.

## Resume point

Phases 1 and 2 are implemented, installed in editable mode, and have been
run by hand end to end: `init-warehouse`, `inspect-schema`, the new
`retrieve` command (ranking and top-k narrowing against a live question),
`validate`, and `ask` across the broadened `ScriptedProvider` rule set
(count, average, time filter, window function, two joins), dialect
translation to Athena/BigQuery, and the audit trace. The engine-error
retry path was exercised directly against `agent.run_agent_loop` with a
custom `LLMProvider` that drafts a query referencing a nonexistent column:
it correctly produces three `engine_error` trace steps (bounded by
`max_correction_attempts`) and a final `validation.errors` message quoting
DuckDB's own binder error. The full test suite (112 tests, pre-existing --
a fresh test pass against this update is expected to land more) passes
unchanged.

Pick up at **Phase 3** — scan-cost guard for real: today's
`_estimate_bytes_scanned` in `cli.py` is a naive "sum of every referenced
table's on-disk file size" placeholder that ignores both partition pruning
(a `WHERE event_date = ...` filter on `events` still charges for every
partition) and column pruning (a `SELECT one_column` still charges for the
whole table). Start by parsing the validated query's `WHERE` clause
against `events`' partition column (`event_date`) using sqlglot, restrict
the file-walk to matching partition directories, and compare against
Parquet column statistics (`pyarrow.parquet.ParquetFile.metadata`) for
column-level pruning. Then wire `BudgetConfig`/`PricingModel` selection
into the CLI (currently only the Python API exposes it -- `ask` always
uses the `AgentConfig` default budget).

STATUS: IN_PROGRESS
