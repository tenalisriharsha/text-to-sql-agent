"""Command-line entry point.

``init-warehouse`` generates the local sample data, ``inspect-schema``
prints the catalog built from it, ``validate`` checks a SQL string against
the safety contract without running it, and ``ask`` runs a question
through the full draft/validate/correct/approve/execute loop. Everything
``ask`` does is written to an audit trace.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
import duckdb

from text_to_sql_agent.agent import run_agent_loop
from text_to_sql_agent.audit import AuditTrace
from text_to_sql_agent.config import AgentConfig
from text_to_sql_agent.llm.scripted import ScriptedProvider
from text_to_sql_agent.retrieval import SchemaRetriever
from text_to_sql_agent.schema_catalog import SchemaCatalog
from text_to_sql_agent.sql.validator import validate_query
from text_to_sql_agent.warehouse.generate import (
    COLUMN_DESCRIPTIONS,
    PARTITION_COLUMNS,
    TABLE_DESCRIPTIONS,
    generate_warehouse,
)
from text_to_sql_agent.warehouse.load import load_into_duckdb

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
    catalog = _build_catalog(warehouse_dir)
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
    catalog = _build_catalog(warehouse_dir)
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
@click.option("--dialect", type=click.Choice(["duckdb"]), default="duckdb", show_default=True)
@click.option("--row-limit", type=int, default=None)
def validate(sql: str, warehouse_dir: Path, dialect: str, row_limit: int | None) -> None:
    """Check SQL against the safety contract without executing it.

    Reports whether SQL is a single, read-only, allowlisted statement, and
    prints the normalized query (with LIMIT enforced) if so.
    """
    catalog = _build_catalog(warehouse_dir)
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
    type=click.Choice(["duckdb"]),
    default="duckdb",
    show_default=True,
    help="Execution dialect. Only duckdb executes locally; see --target-dialect to emit SQL for another engine.",
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
    help="Skip the human-approval prompt before executing against the warehouse.",
)
@click.option("--max-correction-attempts", type=int, default=None)
@click.option("--row-limit", type=int, default=None)
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
def ask(
    question: str,
    warehouse_dir: Path,
    dialect: str,
    target_dialect: str | None,
    audit_log: Path | None,
    yes: bool,
    max_correction_attempts: int | None,
    row_limit: int | None,
    top_k_tables: int | None,
    top_k_columns: int | None,
) -> None:
    """Draft, validate, and (after approval) execute SQL for QUESTION."""
    defaults = AgentConfig()
    config = AgentConfig(
        max_correction_attempts=max_correction_attempts
        if max_correction_attempts is not None
        else defaults.max_correction_attempts,
        default_row_limit=row_limit if row_limit is not None else defaults.default_row_limit,
        retrieval_top_k_tables=top_k_tables if top_k_tables is not None else defaults.retrieval_top_k_tables,
        retrieval_top_k_columns_per_table=top_k_columns
        if top_k_columns is not None
        else defaults.retrieval_top_k_columns_per_table,
    )

    trace = AuditTrace(audit_log)
    trace.record("question_received", question=question, dialect=dialect)

    catalog = _build_catalog(warehouse_dir)
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

    connection = duckdb.connect()
    load_into_duckdb(connection, _warehouse_info(warehouse_dir))

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
    if agent_result.draft_error is not None:
        click.echo(f"could not answer the question: {agent_result.draft_error}", err=True)
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

    bytes_estimate = _estimate_bytes_scanned(warehouse_dir, validation.referenced_tables)
    cost_estimate = config.budget.estimate_cost_usd(bytes_estimate)
    trace.record(
        "cost_estimate",
        bytes_scanned=bytes_estimate,
        cost_usd=round(cost_estimate, 6),
        pricing_model=config.budget.pricing_model.name,
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

    if config.require_human_approval and not yes:
        if not click.confirm("Proceed with execution?", default=False):
            trace.record("final", outcome="rejected_by_user")
            click.echo("aborted.")
            return
        trace.record("approval", decision="confirmed", auto=False)
    else:
        trace.record("approval", decision="confirmed", auto=True)

    click.echo(f"SQL:\n{final_sql}\n")

    trace.record("explain", plan=agent_result.explain_plan)

    result = connection.execute(final_sql)
    columns = [description[0] for description in result.description]
    rows = result.fetchall()
    trace.record("execution_result", row_count=len(rows), columns=columns)

    click.echo("\n" + " | ".join(columns))
    for row in rows:
        click.echo(" | ".join(str(value) for value in row))

    trace.record("final", outcome="executed", sql=final_sql, row_count=len(rows))


def _build_catalog(warehouse_dir: Path) -> SchemaCatalog:
    connection = duckdb.connect()
    load_into_duckdb(connection, _warehouse_info(warehouse_dir))
    return SchemaCatalog.from_duckdb(
        connection,
        table_descriptions=TABLE_DESCRIPTIONS,
        column_descriptions=COLUMN_DESCRIPTIONS,
        partition_columns=PARTITION_COLUMNS,
    )


def _warehouse_info(warehouse_dir: Path):
    from text_to_sql_agent.warehouse.generate import WarehouseInfo

    return WarehouseInfo(
        root=warehouse_dir,
        customers_path=warehouse_dir / "customers.parquet",
        orders_path=warehouse_dir / "orders.parquet",
        events_root=warehouse_dir / "events",
    )


def _estimate_bytes_scanned(warehouse_dir: Path, referenced_tables: tuple[str, ...]) -> int:
    """Naive bytes-scanned estimate: file sizes of every referenced table's data.

    This ignores partition and column pruning; a pruning-aware estimate is a
    later phase's scan-cost guard. It is enough for a hard-budget sanity check.
    """
    total = 0
    info = _warehouse_info(warehouse_dir)
    for table in referenced_tables:
        if table == "customers":
            total += info.customers_path.stat().st_size
        elif table == "orders":
            total += info.orders_path.stat().st_size
        elif table == "events":
            for root, _dirs, files in os.walk(info.events_root):
                for name in files:
                    total += (Path(root) / name).stat().st_size
    return total


if __name__ == "__main__":
    main()
