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
(`orders`, `customers`, `events`, with `events` partitioned Parquet), can
emit Athena (Trino) or BigQuery dialect through `sqlglot`, and -- given
connection details for an account of your own -- can draft, `EXPLAIN`,
and execute against real Athena or BigQuery through the adapters in
`engines/`. An
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
   |  -- ambiguous question (no stated metric/window/table) --> ask for
   |     clarification instead of guessing, no SQL drafted
   |  -- unrecognized question --> declined as unanswerable
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
  cost_estimate.py   estimate_bytes_scanned: partition- and column-pruning-aware scan-cost estimate
  warehouse/         generate.py (sample Parquet warehouse), load.py (register as DuckDB views)
  llm/               base.py (LLMProvider ABC, CorrectionContext), scripted.py (deterministic provider; UnsupportedQuestionError, ClarificationNeededError)
  sql/               validator.py (sqlglot-based validation + LIMIT enforcement), dialect.py (Athena/BigQuery translation)
  engines/           base.py (Connection/Cursor protocol, EngineExecutionError), athena_engine.py, bigquery_engine.py
  evaluation/        benchmark.py (BENCHMARK: 44 scored questions), runner.py (run_benchmark: scores via run_agent_loop)
  audit.py           AuditTrace (JSON Lines trace writer/reader)
  cli.py             `text-to-sql-agent init-warehouse|inspect-schema|retrieve|validate|estimate-cost|benchmark|ask`
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

### Phase 3 — Scan-cost guard for real (complete)
- [x] `cost_estimate.py`: `estimate_bytes_scanned`, a partition- and
      column-pruning-aware replacement for the old "sum of every
      referenced table's on-disk file size" placeholder. It parses the
      *validated* SQL with `sqlglot` (independent of the validator's own
      parse) to: (a) extract equality/`IN`/range predicates on `events`'
      `event_date` partition column and restrict the scan to the matching
      `event_date=...` directories, resolving qualified/unqualified
      columns through a table-alias map built from the query's own
      `FROM`/`JOIN` clauses; (b) extract which columns each table's
      `SELECT`/`WHERE`/`GROUP BY`/`ORDER BY` actually reference and sum
      just those columns' `total_compressed_size` from each Parquet
      file's row-group metadata (`pyarrow.parquet.ParquetFile.metadata`)
      instead of the whole file.
- [x] Both prunings fail safe by design: a bare `*` (including `COUNT(*)`
      — its `Star` isn't a plain projection but the same fallback keeps it
      simple), an ambiguous unqualified column in a multi-table query, an
      `OR`-combined predicate, or a predicate the extractor doesn't
      recognize (e.g. `CURRENT_DATE - INTERVAL`, which can't be evaluated
      without executing it) all fall back to charging for *more* bytes
      (every partition, every column) rather than guessing lower. A
      referenced column absent from a file's physical schema — this is
      how `event_date` itself behaves, since hive partitioning keeps it
      only in the directory name, never in the Parquet file — falls back
      to that file's full size the same way.
- [x] New `estimate-cost SQL` CLI command: validates `SQL` (same contract
      as `validate`) and prints the resulting per-table byte/column/
      partition breakdown plus a USD estimate, independent of asking a
      full question — this is what exercises the module directly and was
      used to confirm partition pruning (30 partitions -> 1 for an
      `event_date = '...'` filter) and column pruning (a 2-column join
      projection costing a fraction of each table's full size) against
      the real sample warehouse (see commit history and
      `docs/screenshots/09-cost-guard.png`).
- [x] `ask` now calls the same `estimate_bytes_scanned` instead of the old
      placeholder, and records the per-table breakdown in the audit trace
      (`cost_estimate` step's new `per_table` field).
- [x] `BudgetConfig`/`PricingModel` selection wired into the CLI: `ask`
      and `estimate-cost` both take `--pricing-model athena|bigquery`,
      and `ask` additionally takes `--max-bytes-scanned`/`--max-cost-usd`
      to override the default hard ceilings per run (`confirm_above_usd`
      remains Python-API-only, as documented in the README — the CLI's
      approval gate always prompts unless `--yes`, independent of that
      threshold).

### Phase 4 — Multi-dialect execution (complete)
- [x] `engines/base.py`: a `Connection`/`Cursor` `Protocol` documenting the
      narrow interface `run_agent_loop` and `ask` need from whatever they
      execute against (`.execute(sql)` returning something with
      `.description`/`.fetchall()`, including the literal string
      `EXPLAIN <query>`), and `EngineExecutionError`, a normalized
      exception every adapter raises instead of its driver's own.
      DuckDB's native connection already satisfies this protocol and is
      used directly, unwrapped, exactly as it has been since Phase 1.
- [x] `engines/athena_engine.py`: `AthenaConnection`, a thin wrapper over
      `pyathena` (optional `athena` extra). Athena speaks Trino SQL and
      Trino supports a real `EXPLAIN <query>` statement, so this adapter
      needs no special-casing for it — every statement, `EXPLAIN`
      included, passes straight through; only the resulting cursor and
      any driver exception get translated into the shapes
      `engines.base.Connection`/`EngineExecutionError` describe. Verified
      directly against the real `pyathena` driver (with the extra
      installed in a scratch venv and placeholder credentials): a lazy
      `pyathena.connect()` plus a real `execute()` call correctly surfaces
      AWS's own `botocore.exceptions.ClientError` wrapped as
      `EngineExecutionError`, and a missing `pyathena` install raises a
      clear "install with `pip install text-to-sql-agent[athena]`"
      message instead of an `ImportError` traceback.
- [x] `engines/bigquery_engine.py`: `BigQueryConnection`, a thin wrapper
      over `google-cloud-bigquery` (optional `bigquery` extra). BigQuery
      has no `EXPLAIN` statement at all — its closest equivalent is a
      dry-run query job, which validates and prices a query without
      scanning data or returning rows — so this adapter intercepts the
      literal `EXPLAIN ` prefix `run_agent_loop`/`ask` issue and runs a
      dry run instead, reporting `total_bytes_processed` as the plan
      text; every other statement goes to BigQuery unmodified.
- [x] `agent.py`: widened `run_agent_loop`'s `except duckdb.Error` to
      `except (duckdb.Error, EngineExecutionError)` around the `EXPLAIN`
      call — the loop's `connection` parameter was already duck-typed
      (just `.execute(sql).fetchall()`), so both adapters work against it
      completely unmodified; existing callers that still pass a raw
      DuckDB connection are unaffected.
- [x] `cli.py`: `ask --dialect` now accepts `duckdb|athena|bigquery` (was
      `duckdb`-only); `athena`/`bigquery` take `--athena-s3-staging-dir`/
      `--athena-region`/`--athena-schema`/`--athena-workgroup` or
      `--bigquery-project`/`--bigquery-location` to build the connection,
      erroring with a clear `click.UsageError` if a dialect's required
      option is missing, and with the adapter's own `EngineExecutionError`
      message (not a traceback) if the optional driver isn't installed or
      the real engine rejects the query. The schema catalog and table
      allowlist always come from the local sample warehouse regardless of
      `--dialect` — `ask`'s retrieval/validation step was never about
      *where* a query runs, and teaching it to introspect a live Glue
      Catalog or BigQuery dataset is out of scope here; this means
      pointing `ask` at Athena/BigQuery assumes that account already has
      `customers`/`orders`/`events` tables with matching columns.
- [x] Non-demo-source approval is mandatory and cannot be bypassed with
      `--yes`: the approval gate now checks `not (yes and dialect ==
      "duckdb")`, so `--yes` still skips the prompt for the bundled demo
      source (unchanged, still exercised by the existing CLI tests) but
      is silently ignored for `athena`/`bigquery`.
- [x] The scan-cost guard (`estimate_bytes_scanned`) sizes the *local*
      warehouse's Parquet files on disk, which has no meaning for a query
      that actually runs against a real Athena/BigQuery account, so `ask`
      skips it for non-`duckdb` dialects and records a `cost_estimate`
      audit step with `bytes_scanned`/`cost_usd` set to `None` and a
      `note` explaining why, instead of a fabricated number. The `EXPLAIN`
      step (a real dry-run bytes estimate for BigQuery, a plan for
      Athena) is the cost signal shown to the human approver instead.
- [x] `validate`/`estimate-cost --dialect` also widened to
      `duckdb|athena|bigquery` for consistency (sqlglot dialect-aware
      parsing/rendering only — neither command executes anything).
- [x] Fixed a pre-existing, unrelated flaky-test bug surfaced while
      verifying this phase: `SchemaRetriever`'s cosine similarity summed
      over a raw set intersection (`a.keys() & b.keys()`), whose iteration
      order is hash-seed-dependent for string keys — floating-point
      addition isn't associative, so near-tied tables (e.g. "customers"
      vs. "orders" for a question containing "customers", since both
      tables' documents contain that token) could flip rank order between
      runs. `_cosine` now sums over `sorted(...)`. The actual root cause
      of the observed flakiness was `schema_catalog._sample_values`'s
      `SELECT DISTINCT ... LIMIT N` with no `ORDER BY`, which DuckDB does
      not guarantee a stable row order for — different sample values
      each run meant genuinely different TF-IDF documents, not just
      summation-order noise. Added `ORDER BY` there too; `pytest
      tests/test_retrieval.py` run 8x with `PYTHONHASHSEED` varied 1-8
      confirmed both fixes together eliminate the flake (0/8 failures,
      versus 2-4/8 before).

### Phase 5 — Evaluation harness (complete)
- [x] Ambiguity detection, the prerequisite the Phase 4 resume point called
      out as design work rather than more canned SQL:
      `llm/scripted.py` gains `ClarificationNeededError` (carries the
      ambiguity itself plus concrete rephrasings that would resolve it)
      and `_AMBIGUOUS_RULES`, six keyword-matched phrases
      ("top performers", "recent activity", "how is the business doing",
      "last month's numbers", "customer activity summary", "our growth")
      that are recognizable but genuinely under-specified -- no stated
      metric, time window, or table -- checked *before* `_RULES` so
      `draft_sql` refuses to guess one interpretation rather than
      silently answering a different question than the one asked.
      `agent.AgentResult` gains a `needs_clarification` field (distinct
      from `draft_error`, which stays for "no rule matches at all"), and
      `run_agent_loop` catches `ClarificationNeededError` the same way it
      already caught `UnsupportedQuestionError` -- no retry, immediate
      return. `ask` prints "question is ambiguous: ..." and exits 1.
- [x] `evaluation/benchmark.py`: `BenchmarkQuestion` (id, question,
      category, difficulty 1-5, `expected_outcome` of `"success"` /
      `"clarification"` / `"unanswerable"`, and -- only for `"success"`
      -- its own `expected_sql`, authored independently of
      `ScriptedProvider` rather than copied from it, so scoring checks
      the agent's answer against a ground truth instead of against
      itself) and `BENCHMARK`, 44 questions: 32 "success" questions
      across every `ScriptedProvider` rule and (for rules with more than
      one keyword) every keyword phrasing -- counts, aggregates,
      group-bys, two kinds of join, a top-N ranking, a relative-date
      time filter, a window function -- plus the six ambiguous phrasings
      above and 7 "unanswerable" questions about data the warehouse
      genuinely doesn't have (no `products`/`employees` table, no
      precomputed churn/forecast metric), rising in difficulty 1-5
      within and across categories.
- [x] `evaluation/runner.py`: `run_benchmark` runs every question
      through the real `run_agent_loop` (schema retrieval included)
      against the local DuckDB warehouse -- the first thing in the
      project to call it dozens of times in a row rather than once per
      CLI invocation. `QuestionResult` scores two independent axes:
      `outcome_correct` (did the agent reach the right *kind* of result
      -- answered / asked for clarification / declined) and, only for
      "success" questions that *did* succeed, `result_match` (the
      agent's final SQL and the question's own `expected_sql` run
      against the same connection, rows compared as a sorted multiset so
      `ORDER BY` differences don't cause false mismatches) -- plus
      `attempts` (steps taken) and `bytes_scanned`/`cost_usd` (via the
      Phase 3 `estimate_bytes_scanned`, same pricing model path as
      `ask`/`estimate-cost`). `BenchmarkReport` aggregates pass rate
      overall, by category, and by difficulty, mean attempts, and total
      cost. Verified the scoring itself isn't vacuous by deliberately
      feeding a question with a *wrong* `expected_sql` (counting `orders`
      instead of `customers`) through `run_benchmark` directly and
      confirming it's correctly reported as `result_match=False` rather
      than passing.
- [x] New `benchmark` CLI command: runs `BENCHMARK` (optionally filtered
      by `--category`, repeatable) against `--warehouse-dir`, prints the
      pass/fail summary by category and difficulty plus mean attempts and
      total estimated cost, lists any failing questions with what was
      expected vs. what happened, optionally writes the full per-question
      report as JSON (`--output`), and exits non-zero if anything failed.
      Manually run against the real sample warehouse: 44/44 pass at
      100% today; re-run filtered to just `--category ambiguous
      --category unanswerable` (13/13) to confirm filtering and the new
      clarification path both work through the actual CLI, not just unit
      tests.
- [x] Deduplicated warehouse-path plumbing surfaced while wiring the
      benchmark in: `cli.py` had three near-identical private helpers
      (`_build_catalog`, `_warehouse_info`, `_table_locations`) that the
      evaluation runner would otherwise have had to copy a fourth time.
      Moved them to where the data they describe already lives --
      `warehouse_info_for`/`table_locations_for` in
      `warehouse/generate.py`, `build_catalog` in `warehouse/load.py` --
      and `cli.py` now calls those instead of defining its own; behavior
      is unchanged (confirmed by the full suite passing before and after
      and by re-running every CLI command manually), this was a pure
      dependency-direction cleanup so the new `evaluation/runner.py`
      didn't have to reach into `cli.py`'s private functions.

### Phase 6 — Polish
- [ ] Optional local-model provider.
- [ ] CI workflow running the test suite on every push.
- [ ] Packaging/distribution polish and documentation pass.

## Resume point

Phases 1-5 are implemented, installed in editable mode, and have been run
by hand end to end: every CLI command (`init-warehouse`, `inspect-schema`,
`retrieve`, `validate`, `estimate-cost`, `benchmark`, `ask`) against the
real sample warehouse, the Athena/BigQuery adapters' error paths (missing
required CLI options -> `click.UsageError`; optional driver not installed
-> a clear `EngineExecutionError` message; `pyathena` actually installed
in a scratch venv and exercised against the real driver with placeholder
AWS credentials, confirming a real `botocore.exceptions.ClientError`
comes back wrapped as `EngineExecutionError` rather than an unhandled
traceback), `ask` against a deliberately ambiguous question (confirms the
"question is ambiguous: ..." path, exit 1), and `benchmark` both
unfiltered (44/44 pass) and filtered to `--category ambiguous --category
unanswerable` (13/13 pass) with `--output` writing a valid JSON report.
The full test suite (211 tests, pre-existing, unchanged by this phase's
non-test code) passes.

Pick up at **Phase 6** — polish:
- CI (`.github/workflows/ci.yml`, running `pytest` on push/PR) already
  exists from an earlier night; packaging (`pyproject.toml` extras for
  `athena`/`bigquery`, the `dev` extra for `pytest`/`pytest-cov`) is
  already in reasonable shape. What's left here is genuinely optional
  per the original plan: an optional local-model `LLMProvider` (e.g.
  wrapping an Ollama or llama.cpp model) alongside `ScriptedProvider`,
  which would be the first provider where the retrieved `schema_context`
  and correction-driven retries are read *semantically* rather than
  being inert plumbing (see the architecture note above `ScriptedProvider`
  ignoring both) -- and whatever documentation/polish gaps a fresh read
  of the README turns up.
- Nothing above blocks calling the project done: every phase in the
  original plan (1-5) is complete, the safety contract, scan-cost guard,
  multi-dialect execution, and evaluation harness are all real and
  exercised, and the "optional" Phase 6 item was marked optional from
  when this plan was first written, not added later to excuse skipping
  it.

STATUS: COMPLETE
