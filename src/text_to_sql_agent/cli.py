"""Command-line entry point.

``init-warehouse`` generates the local sample data, ``inspect-schema``
prints the catalog built from it, ``validate`` checks a SQL string against
the safety contract without running it, and ``ask`` runs a question
through the full draft/validate/correct/approve/execute loop. Everything
``ask`` does is written to an audit trace.

``ask --dialect`` chooses where that loop's EXPLAIN and execution steps
run: ``duckdb`` (the default) against the local sample warehouse, or
``athena``/``bigquery`` against your own account via the adapters in
``engines/`` -- the schema catalog and table allowlist always come from
the local warehouse, so this assumes your account already has tables of
the same names. Non-``duckdb`` dialects always require interactive
approval, regardless of ``--yes``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import click
import duckdb

from text_to_sql_agent.agent import run_agent_loop
from text_to_sql_agent.audit import AuditTrace
from text_to_sql_agent.config import AgentConfig, BudgetConfig, pricing_model_for
from text_to_sql_agent.cost_estimate import estimate_bytes_scanned
from text_to_sql_agent.engines.athena_engine import AthenaConfig, AthenaConnection
from text_to_sql_agent.engines.base import EngineExecutionError
from text_to_sql_agent.engines.bigquery_engine import BigQueryConfig, BigQueryConnection
from text_to_sql_agent.evaluation.benchmark import BENCHMARK
from text_to_sql_agent.evaluation.runner import run_benchmark
from text_to_sql_agent.llm.scripted import ScriptedProvider
from text_to_sql_agent.retrieval import SchemaRetriever
from text_to_sql_agent.sql.validator import validate_query
from text_to_sql_agent.warehouse.generate import generate_warehouse, table_locations_for, warehouse_info_for
from text_to_sql_agent.warehouse.load import build_catalog, load_into_duckdb

DEFAULT_WAREHOUSE_DIR = Path("data/warehouse")


@click.group()
def main() -> None:
    """text-to-sql-agent: an agentic natural-language-to-SQL assistant."""


@main.command("init-warehouse")
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_WAREHOUSE_DIR,
    show_default=True,
    help="Directory to write the generated Parquet warehouse into.",
)
@click.option("--seed", type=int, default=42, show_default=True)
def init_warehouse(output_dir: Path, seed: int) -> None:
    """Generate the local sample warehouse (customers, orders, events)."""
    info = generate_warehouse(output_dir, seed=seed)
    click.echo(f"customers: {info.customers_path}")
    click.echo(f"orders:    {info.orders_path}")
    click.echo(f"events:    {info.events_root}/event_date=.../part-0.parquet")


@main.command("inspect-schema")
@click.option(
    "--warehouse-dir",
    type=click.Path(path_type=Path, exists=True),
    default=DEFAULT_WAREHOUSE_DIR,
    show_default=True,
)
def inspect_schema(warehouse_dir: Path) -> None:
    """Print the schema catalog built from the sample warehouse."""
    catalog = build_catalog(warehouse_dir)
    click.echo(catalog.to_text())


@main.command("retrieve")
@click.argument("question")
@click.option(
    "--warehouse-dir",
    type=click.Path(path_type=Path, exists=True),
    default=DEFAULT_WAREHOUSE_DIR,
    show_default=True,
)
@click.option(
    "--top-k-tables",
    type=int,
    default=None,
    help="Keep only the top-k ranked tables. Default: rank, show all.",
)
@click.option(
    "--top-k-columns",
    type=int,
    default=None,
    help="Within each kept table, keep only the top-k ranked columns.",
)
def retrieve(question: str, warehouse_dir: Path, top_k_tables: int | None, top_k_columns: int | None) -> None:
    """Rank the catalog's tables against QUESTION and show the narrowed schema.

    This is the retrieval step `ask` runs internally before drafting SQL,
    exposed directly so the ranking and narrowing can be inspected on their
    own. Scores are TF-IDF cosine similarity between the question and each
    table's/column's name, description, and sample values -- no network
    call or embedding API is involved.
    """
    catalog = build_catalog(warehouse_dir)
    retriever = SchemaRetriever(catalog)

    click.echo("ranked tables:")
    for match in retriever.rank_tables(question):
        click.echo(f"  {match.score:.4f}  {match.table}")

    narrowed = retriever.retrieve(
        question, top_k_tables=top_k_tables, top_k_columns_per_table=top_k_columns
    )
    click.echo("\nretrieved schema context:")
    click.echo(narrowed.to_text())


@main.command("validate")
@click.argument("sql")
@click.option(
    "--warehouse-dir",
    type=click.Path(path_type=Path, exists=True),
    default=DEFAULT_WAREHOUSE_DIR,
    show_default=True,
)
@click.option(
    "--dialect", type=click.Choice(["duckdb", "athena", "bigquery"]), default="duckdb", show_default=True
)
@click.option("--row-limit", type=int, default=None)
def validate(sql: str, warehouse_dir: Path, dialect: str, row_limit: int | None) -> None:
    """Check SQL against the safety contract without executing it.

    Reports whether SQL is a single, read-only, allowlisted statement, and
    prints the normalized query (with LIMIT enforced) if so.
    """
    catalog = build_catalog(warehouse_dir)
    default_row_limit = row_limit if row_limit is not None else AgentConfig().default_row_limit
    result = validate_query(
        sql,
        table_allowlist=catalog.table_names,
        dialect=dialect,
        default_row_limit=default_row_limit,
    )
    if result.is_valid:
        click.echo("VALID")
        click.echo(result.normalized_sql)
    else:
        click.echo("REJECTED")
        for error in result.errors:
            click.echo(f"  - {error}")
        sys.exit(1)


@main.command("estimate-cost")
@click.argument("sql")
@click.option(
    "--warehouse-dir",
    type=click.Path(path_type=Path, exists=True),
    default=DEFAULT_WAREHOUSE_DIR,
    show_default=True,
)
@click.option(
    "--dialect", type=click.Choice(["duckdb", "athena", "bigquery"]), default="duckdb", show_default=True
)
@click.option("--row-limit", type=int, default=None)
@click.option(
    "--pricing-model",
    type=click.Choice(["athena", "bigquery"]),
    default="athena",
    show_default=True,
)
def estimate_cost(
    sql: str, warehouse_dir: Path, dialect: str, row_limit: int | None, pricing_model: str
) -> None:
    """Validate SQL, then show its partition/column-pruning-aware scan estimate.

    This is the scan-cost guard `ask` runs internally before execution,
    exposed directly: SQL is validated first (same safety contract as
    `validate`), then the normalized query is sized against the sample
    warehouse on disk -- restricting `events` to the partitions its WHERE
    clause actually allows, and restricting every table to the columns its
    SELECT list, WHERE clause, GROUP BY, and ORDER BY actually reference.
    """
    catalog = build_catalog(warehouse_dir)
    default_row_limit = row_limit if row_limit is not None else AgentConfig().default_row_limit
    result = validate_query(
        sql,
        table_allowlist=catalog.table_names,
        dialect=dialect,
        default_row_limit=default_row_limit,
    )
    if not result.is_valid:
        click.echo("REJECTED")
        for error in result.errors:
            click.echo(f"  - {error}")
        sys.exit(1)

    assert result.normalized_sql is not None
    scan = estimate_bytes_scanned(result.normalized_sql, dialect, table_locations_for(warehouse_dir))
    cost = pricing_model_for(pricing_model).estimate_cost_usd(scan.total_bytes)

    click.echo(f"total: {scan.total_bytes} bytes (~${cost:.6f}, {pricing_model} pricing)")
    for table, estimate in sorted(scan.per_table.items()):
        click.echo(f"\n  {table}: {estimate.bytes_scanned} bytes")
        if estimate.all_columns:
            click.echo("    columns: all")
        else:
            click.echo(f"    columns: {', '.join(estimate.columns_scanned) or '(none referenced)'}")
        if estimate.partitions_scanned is not None:
            click.echo(
                f"    partitions scanned: {len(estimate.partitions_scanned)} "
                f"({', '.join(estimate.partitions_scanned) or 'none'})"
            )


@main.command("benchmark")
@click.option(
    "--warehouse-dir",
    type=click.Path(path_type=Path, exists=True),
    default=DEFAULT_WAREHOUSE_DIR,
    show_default=True,
)
@click.option(
    "--category",
    multiple=True,
    help=(
        "Restrict to one or more categories (repeatable): count, aggregate, "
        "groupby, join, top_n, time_filter, window, ambiguous, unanswerable. "
        "Default: every question in the benchmark."
    ),
)
@click.option(
    "--pricing-model",
    type=click.Choice(["athena", "bigquery"]),
    default="athena",
    show_default=True,
)
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Write the full per-question report as JSON to this path.",
)
@click.option("--max-correction-attempts", type=int, default=None)
@click.option("--top-k-tables", type=int, default=None)
@click.option("--top-k-columns", type=int, default=None)
def benchmark(
    warehouse_dir: Path,
    category: tuple[str, ...],
    pricing_model: str,
    output: Path | None,
    max_correction_attempts: int | None,
    top_k_tables: int | None,
    top_k_columns: int | None,
) -> None:
    """Run the bundled benchmark against the sample warehouse and score it.

    Every question goes through the same draft/validate/EXPLAIN loop `ask`
    runs, against `--dialect duckdb` only -- this measures the pipeline
    itself (retrieval, drafting, validation, self-correction, scan cost),
    independent of any particular cloud account. Scoring checks both
    whether the agent reached the right *kind* of outcome (answered /
    asked for clarification / declined as unanswerable) and, for questions
    it should answer, whether it returned the right rows.
    """
    defaults = AgentConfig()
    config = AgentConfig(
        max_correction_attempts=max_correction_attempts
        if max_correction_attempts is not None
        else defaults.max_correction_attempts,
        retrieval_top_k_tables=top_k_tables if top_k_tables is not None else defaults.retrieval_top_k_tables,
        retrieval_top_k_columns_per_table=top_k_columns
        if top_k_columns is not None
        else defaults.retrieval_top_k_columns_per_table,
    )

    questions = BENCHMARK
    if category:
        wanted = set(category)
        questions = tuple(q for q in questions if q.category in wanted)
        if not questions:
            raise click.UsageError(f"no benchmark questions match category filter: {', '.join(category)}")

    report = run_benchmark(questions, warehouse_dir, config=config, pricing_model=pricing_model)

    click.echo(
        f"{report.passed}/{report.total} passed ({report.accuracy:.0%}), "
        f"mean attempts {report.mean_attempts:.2f}, "
        f"est. cost ${report.total_cost_usd:.4f} ({pricing_model} pricing)"
    )

    click.echo("\nby category:")
    for cat, (passed, total) in report.by_category().items():
        click.echo(f"  {cat:<14} {passed}/{total}")

    click.echo("\nby difficulty:")
    for difficulty, (passed, total) in report.by_difficulty().items():
        click.echo(f"  {difficulty}  {passed}/{total}")

    failures = report.failures()
    if failures:
        click.echo(f"\n{len(failures)} failing question(s):")
        for result in failures:
            detail = f" -- {result.detail}" if result.detail else ""
            click.echo(
                f"  [{result.question_id}] expected {result.expected_outcome}, "
                f"got {result.actual_outcome}{detail}"
            )

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report.to_dict(), indent=2))
        click.echo(f"\nfull report written to {output}")

    if failures:
        sys.exit(1)


@main.command("ask")
@click.argument("question")
@click.option(
    "--warehouse-dir",
    type=click.Path(path_type=Path, exists=True),
    default=DEFAULT_WAREHOUSE_DIR,
    show_default=True,
)
@click.option(
    "--dialect",
    type=click.Choice(["duckdb", "athena", "bigquery"]),
    default="duckdb",
    show_default=True,
    help=(
        "Execution dialect. 'duckdb' runs against the local sample warehouse; "
        "'athena'/'bigquery' run against your own account (see --athena-* / "
        "--bigquery-* below) and assume it already has tables matching the "
        "catalog's names. See --target-dialect to print translated SQL "
        "without executing anywhere."
    ),
)
@click.option(
    "--target-dialect",
    type=click.Choice(["duckdb", "athena", "bigquery"]),
    default=None,
    help="Print the validated query translated to this dialect instead of executing it.",
)
@click.option("--audit-log", type=click.Path(path_type=Path), default=None)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help=(
        "Skip the human-approval prompt before executing. Only applies to "
        "--dialect duckdb, the bundled demo source; Athena and BigQuery "
        "always require interactive approval."
    ),
)
@click.option("--max-correction-attempts", type=int, default=None)
@click.option("--row-limit", type=int, default=None)
@click.option(
    "--athena-s3-staging-dir",
    default=None,
    help="Required with --dialect athena: S3 URI Athena stages query results to.",
)
@click.option(
    "--athena-region", default=None, help="Required with --dialect athena: AWS region of the Athena workgroup."
)
@click.option(
    "--athena-schema",
    default="default",
    show_default=True,
    help="Athena/Glue schema (database) to run against.",
)
@click.option("--athena-workgroup", default=None, help="Athena workgroup, if your account requires one.")
@click.option(
    "--bigquery-project",
    default=None,
    help="Required with --dialect bigquery: GCP project to bill and run jobs in.",
)
@click.option(
    "--bigquery-location",
    default=None,
    help="BigQuery job location, if your dataset isn't in the default location.",
)
@click.option(
    "--top-k-tables",
    type=int,
    default=None,
    help="Retrieval: keep only the top-k ranked tables in the provider's schema context.",
)
@click.option(
    "--top-k-columns",
    type=int,
    default=None,
    help="Retrieval: within each kept table, keep only the top-k ranked columns.",
)
@click.option(
    "--pricing-model",
    type=click.Choice(["athena", "bigquery"]),
    default=None,
    help="Scan-cost pricing model. Default: athena.",
)
@click.option(
    "--max-bytes-scanned",
    type=int,
    default=None,
    help="Hard ceiling on estimated bytes scanned; above it, ask refuses to run.",
)
@click.option(
    "--max-cost-usd",
    type=float,
    default=None,
    help="Hard ceiling on estimated cost in USD; above it, ask refuses to run.",
)
def ask(
    question: str,
    warehouse_dir: Path,
    dialect: str,
    target_dialect: str | None,
    audit_log: Path | None,
    yes: bool,
    max_correction_attempts: int | None,
    row_limit: int | None,
    athena_s3_staging_dir: str | None,
    athena_region: str | None,
    athena_schema: str,
    athena_workgroup: str | None,
    bigquery_project: str | None,
    bigquery_location: str | None,
    top_k_tables: int | None,
    top_k_columns: int | None,
    pricing_model: str | None,
    max_bytes_scanned: int | None,
    max_cost_usd: float | None,
) -> None:
    """Draft, validate, and (after approval) execute SQL for QUESTION."""
    defaults = AgentConfig()
    budget = BudgetConfig(
        pricing_model=pricing_model_for(pricing_model) if pricing_model is not None else defaults.budget.pricing_model,
        max_bytes_scanned=max_bytes_scanned if max_bytes_scanned is not None else defaults.budget.max_bytes_scanned,
        max_cost_usd=max_cost_usd if max_cost_usd is not None else defaults.budget.max_cost_usd,
    )
    config = AgentConfig(
        max_correction_attempts=max_correction_attempts
        if max_correction_attempts is not None
        else defaults.max_correction_attempts,
        default_row_limit=row_limit if row_limit is not None else defaults.default_row_limit,
        retrieval_top_k_tables=top_k_tables if top_k_tables is not None else defaults.retrieval_top_k_tables,
        retrieval_top_k_columns_per_table=top_k_columns
        if top_k_columns is not None
        else defaults.retrieval_top_k_columns_per_table,
        budget=budget,
    )

    trace = AuditTrace(audit_log)
    trace.record("question_received", question=question, dialect=dialect)

    catalog = build_catalog(warehouse_dir)
    retriever = SchemaRetriever(catalog)
    retrieved_catalog = retriever.retrieve(
        question,
        top_k_tables=config.retrieval_top_k_tables,
        top_k_columns_per_table=config.retrieval_top_k_columns_per_table,
    )
    trace.record(
        "schema_retrieved",
        tables=list(catalog.table_names),
        retrieved_tables=list(retrieved_catalog.table_names),
    )

    try:
        connection = _build_connection(
            dialect,
            warehouse_dir,
            athena_s3_staging_dir,
            athena_region,
            athena_schema,
            athena_workgroup,
            bigquery_project,
            bigquery_location,
        )
    except EngineExecutionError as exc:
        click.echo(str(exc), err=True)
        trace.record("final", outcome="connection_failed", error=str(exc))
        sys.exit(1)

    provider = ScriptedProvider()
    agent_result = run_agent_loop(
        provider,
        question,
        retrieved_catalog.to_text(),
        catalog.table_names,
        dialect,
        connection,
        config,
        trace,
    )
    if agent_result.needs_clarification is not None:
        click.echo(f"question is ambiguous: {agent_result.needs_clarification}", err=True)
        trace.record("final", outcome="needs_clarification", reason=agent_result.needs_clarification)
        sys.exit(1)

    if agent_result.draft_error is not None:
        click.echo(f"could not answer the question: {agent_result.draft_error}", err=True)
        trace.record("final", outcome="draft_failed", error=agent_result.draft_error)
        sys.exit(1)

    validation = agent_result.validation
    if validation is None or not validation.is_valid:
        errors = list(validation.errors) if validation is not None else []
        click.echo(
            "validation failed after exhausting correction attempts:\n  " + "\n  ".join(errors),
            err=True,
        )
        trace.record("final", outcome="validation_failed", errors=errors)
        sys.exit(1)

    final_sql = validation.normalized_sql
    assert final_sql is not None

    if target_dialect is not None and target_dialect != dialect:
        from text_to_sql_agent.sql.dialect import translate_dialect

        final_sql = translate_dialect(final_sql, dialect, target_dialect)
        click.echo(final_sql)
        trace.record("final", outcome="translated", dialect=target_dialect, sql=final_sql)
        return

    demo_source = dialect == "duckdb"
    if demo_source:
        scan = estimate_bytes_scanned(final_sql, dialect, table_locations_for(warehouse_dir))
        bytes_estimate = scan.total_bytes
        cost_estimate = config.budget.estimate_cost_usd(bytes_estimate)
        trace.record(
            "cost_estimate",
            bytes_scanned=bytes_estimate,
            cost_usd=round(cost_estimate, 6),
            pricing_model=config.budget.pricing_model.name,
            per_table={name: asdict(estimate) for name, estimate in scan.per_table.items()},
        )

        if bytes_estimate > config.budget.max_bytes_scanned or cost_estimate > config.budget.max_cost_usd:
            click.echo(
                f"refusing to run: estimated cost ${cost_estimate:.4f} "
                f"({bytes_estimate} bytes) exceeds the configured budget",
                err=True,
            )
            trace.record("final", outcome="budget_exceeded")
            sys.exit(1)

        click.echo(f"estimated scan: {bytes_estimate} bytes (~${cost_estimate:.4f})")
    else:
        # The scan-cost guard sizes the *local* sample warehouse's Parquet
        # files, which only stands in for a real engine's cost when that
        # engine is the local demo itself. Against a real Athena/BigQuery
        # account there is no local file to size, so the guard is skipped
        # here -- the EXPLAIN plan recorded below is the cost signal
        # instead (BigQuery's is a dry-run bytes-processed estimate).
        trace.record(
            "cost_estimate",
            bytes_scanned=None,
            cost_usd=None,
            pricing_model=config.budget.pricing_model.name,
            per_table={},
            note="scan-cost estimation is only available for --dialect duckdb",
        )
        click.echo("scan-cost estimate unavailable for this engine; review the EXPLAIN plan before approving.")

    if config.require_human_approval and not (yes and demo_source):
        if not click.confirm("Proceed with execution?", default=False):
            trace.record("final", outcome="rejected_by_user")
            click.echo("aborted.")
            return
        trace.record("approval", decision="confirmed", auto=False)
    else:
        trace.record("approval", decision="confirmed", auto=True)

    click.echo(f"SQL:\n{final_sql}\n")

    trace.record("explain", plan=agent_result.explain_plan)

    try:
        result = connection.execute(final_sql)
        columns = [description[0] for description in result.description]
        rows = result.fetchall()
    except EngineExecutionError as exc:
        click.echo(f"execution failed: {exc}", err=True)
        trace.record("final", outcome="execution_failed", error=str(exc))
        sys.exit(1)
    trace.record("execution_result", row_count=len(rows), columns=columns)

    click.echo("\n" + " | ".join(columns))
    for row in rows:
        click.echo(" | ".join(str(value) for value in row))

    trace.record("final", outcome="executed", sql=final_sql, row_count=len(rows))


def _build_connection(
    dialect: str,
    warehouse_dir: Path,
    athena_s3_staging_dir: str | None,
    athena_region: str | None,
    athena_schema: str,
    athena_workgroup: str | None,
    bigquery_project: str | None,
    bigquery_location: str | None,
):
    """Build the connection `ask` drafts/validates/EXPLAINs/executes against.

    `duckdb` loads the local sample warehouse, exactly as every other
    command does. `athena`/`bigquery` connect to your own account instead
    -- the schema catalog and table allowlist always come from the local
    warehouse regardless of dialect, so this assumes your account already
    has tables named `customers`/`orders`/`events` with matching columns.
    """
    if dialect == "duckdb":
        connection = duckdb.connect()
        load_into_duckdb(connection, warehouse_info_for(warehouse_dir))
        return connection
    if dialect == "athena":
        if not athena_s3_staging_dir or not athena_region:
            raise click.UsageError(
                "--dialect athena requires --athena-s3-staging-dir and --athena-region"
            )
        return AthenaConnection(
            AthenaConfig(
                s3_staging_dir=athena_s3_staging_dir,
                region_name=athena_region,
                schema_name=athena_schema,
                work_group=athena_workgroup,
            )
        )
    if dialect == "bigquery":
        if not bigquery_project:
            raise click.UsageError("--dialect bigquery requires --bigquery-project")
        return BigQueryConnection(BigQueryConfig(project=bigquery_project, location=bigquery_location))
    raise click.UsageError(f"unsupported dialect '{dialect}'")


if __name__ == "__main__":
    main()
