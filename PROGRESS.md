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
   |  (Phase 2: embedding search narrows this to the relevant tables/columns)
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

Every box above after `SchemaCatalog` runs inside a bounded loop (default 3
retries): a validation or engine failure becomes a `CorrectionContext` fed
back into `LLMProvider.draft_sql` rather than a hard stop.

### Package layout

```
src/text_to_sql_agent/
  config.py          AgentConfig, BudgetConfig, PricingModel (Athena/BigQuery)
  schema_catalog.py  ColumnSchema, TableSchema, SchemaCatalog (+ DuckDB introspection)
  warehouse/         generate.py (sample Parquet warehouse), load.py (register as DuckDB views)
  llm/               base.py (LLMProvider ABC, CorrectionContext), scripted.py (deterministic provider)
  sql/               validator.py (sqlglot-based validation + LIMIT enforcement), dialect.py (Athena/BigQuery translation)
  audit.py           AuditTrace (JSON Lines trace writer/reader)
  cli.py             `text-to-sql-agent init-warehouse|inspect-schema|ask`
```

## Build plan

### Phase 1 — Scaffold and core foundation (this phase)
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
- [x] `cli.py`: `init-warehouse`, `inspect-schema`, and `ask` — wires the
      draft/validate/correct loop, a naive scan-cost estimate, a human
      approval prompt, `EXPLAIN`, and execution together end to end.
- [x] Manually exercised every command and the correction loop against the
      sample warehouse (see commit history); independent tests land from
      the test-writing pass against this plan and the README.

### Phase 2 — Retrieval and real self-correction
- [ ] Embedding index over table/column descriptions + sample values.
- [ ] Narrow `SchemaCatalog.to_text()` to the top-k relevant tables/columns
      per question instead of passing the whole catalog.
- [ ] Feed real engine errors (not just validator errors) from `EXPLAIN`
      back into the correction loop.
- [ ] Expand `ScriptedProvider` coverage (joins, aggregates, window
      functions, time filters) to match the eventual benchmark's difficulty
      curve.

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

Everything in Phase 1 is implemented, installed in editable mode, and has
been run by hand end to end (`init-warehouse`, `inspect-schema`, `ask` with
single-shot and self-correcting drafts, dialect translation to
Athena/BigQuery, and the audit trace). Pick up at **Phase 2**: start with
the embedding index (new `retrieval.py` module under `text_to_sql_agent/`)
and wire it into `cli.py`'s `_build_catalog`/`ask` path so the schema
context sent to the provider is the retrieved subset, not the whole
catalog's text.

STATUS: IN_PROGRESS
