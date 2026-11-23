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

from text_to_sql_agent.audit import AuditTrace
from text_to_sql_agent.config import AgentConfig
from text_to_sql_agent.llm.base import CorrectionContext
from text_to_sql_agent.llm.scripted import ScriptedProvider, UnsupportedQuestionError
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
def ask(
    question: str,
    warehouse_dir: Path,
    dialect: str,
    target_dialect: str | None,
    audit_log: Path | None,
    yes: bool,
    max_correction_attempts: int | None,
    row_limit: int | None,
) -> None:
    """Draft, validate, and (after approval) execute SQL for QUESTION."""
    config = AgentConfig(
        max_correction_attempts=max_correction_attempts
        if max_correction_attempts is not None
        else AgentConfig().max_correction_attempts,
        default_row_limit=row_limit if row_limit is not None else AgentConfig().default_row_limit,
    )

    trace = AuditTrace(audit_log)
    trace.record("question_received", question=question, dialect=dialect)

    catalog = _build_catalog(warehouse_dir)
    trace.record("schema_retrieved", tables=list(catalog.table_names))

    provider = ScriptedProvider()
    sql, validation = _draft_and_validate(
        provider, question, catalog, dialect, config, trace
    )
    if validation is None:
        click.echo(f"could not answer the question: {sql}", err=True)
        sys.exit(1)
    if not validation.is_valid:
        click.echo(
            "validation failed after exhausting correction attempts:\n  "
            + "\n  ".join(validation.errors),
            err=True,
        )
        trace.record("final", outcome="validation_failed", errors=list(validation.errors))
        sys.exit(1)

    final_sql = validation.normalized_sql
    assert final_sql is not None

    if target_dialect is not None and target_dialect != dialect:
        from text_to_sql_agent.sql.dialect import translate_dialect

        final_sql = translate_dialect(final_sql, dialect, target_dialect)
        click.echo(final_sql)
        trace.record("final", outcome="translated", dialect=target_dialect, sql=final_sql)
        return

    connection = duckdb.connect()
    load_into_duckdb(connection, _warehouse_info(warehouse_dir))

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

    plan = connection.execute(f"EXPLAIN {final_sql}").fetchall()
    trace.record("explain", plan="\n".join(row[-1] for row in plan))

    result = connection.execute(final_sql)
    columns = [description[0] for description in result.description]
    rows = result.fetchall()
    trace.record("execution_result", row_count=len(rows), columns=columns)

    click.echo("\n" + " | ".join(columns))
    for row in rows:
        click.echo(" | ".join(str(value) for value in row))

    trace.record("final", outcome="executed", sql=final_sql, row_count=len(rows))


def _draft_and_validate(provider, question, catalog, dialect, config, trace):
    correction = None
    schema_text = catalog.to_text()
    attempts = config.max_correction_attempts + 1
    validation = None
    for attempt in range(1, attempts + 1):
        try:
            sql = provider.draft_sql(question, schema_text, dialect, correction)
        except UnsupportedQuestionError as exc:
            trace.record("draft_failed", attempt=attempt, error=str(exc))
            return str(exc), None

        trace.record("draft", attempt=attempt, sql=sql)
        validation = validate_query(
            sql,
            table_allowlist=catalog.table_names,
            dialect=dialect,
            default_row_limit=config.default_row_limit,
        )
        trace.record(
            "validation",
            attempt=attempt,
            is_valid=validation.is_valid,
            errors=list(validation.errors),
        )
        if validation.is_valid:
            return sql, validation
        correction = CorrectionContext(previous_sql=sql, error_message="; ".join(validation.errors))

    return sql, validation


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
