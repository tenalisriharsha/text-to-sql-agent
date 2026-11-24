"""End-to-end tests for the CLI documented in README's Usage section.

These drive the real `text-to-sql-agent` entry point (via click's
CliRunner) against a generated sample warehouse, with no module of the
project's own mocked out -- only the actual ScriptedProvider answers
questions, exactly as a user running the documented commands would see.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from text_to_sql_agent.audit import AuditTrace
from text_to_sql_agent.cli import main


def test_init_warehouse_generates_the_documented_tables(tmp_path: Path):
    runner = CliRunner()
    output_dir = tmp_path / "data" / "warehouse"
    result = runner.invoke(main, ["init-warehouse", "--output-dir", str(output_dir), "--seed", "1"])

    assert result.exit_code == 0, result.output
    assert "customers" in result.output
    assert "orders" in result.output
    assert "events" in result.output
    assert (output_dir / "customers.parquet").is_file()
    assert (output_dir / "orders.parquet").is_file()
    assert any((output_dir / "events").iterdir())


def test_inspect_schema_prints_table_and_column_names(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["inspect-schema", "--warehouse-dir", str(warehouse_dir)])

    assert result.exit_code == 0, result.output
    assert "customers" in result.output
    assert "orders" in result.output
    assert "events" in result.output
    assert "customer_id" in result.output


def test_retrieve_ranks_every_table_with_a_score(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main, ["retrieve", "how many customers", "--warehouse-dir", str(warehouse_dir)]
    )

    assert result.exit_code == 0, result.output
    assert "ranked tables:" in result.output
    assert "customers" in result.output
    assert "orders" in result.output
    assert "events" in result.output
    assert "retrieved schema context:" in result.output


def test_retrieve_top_k_tables_narrows_the_printed_schema_context(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "retrieve",
            "how many customers",
            "--warehouse-dir",
            str(warehouse_dir),
            "--top-k-tables",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    # the ranked-tables listing still shows every table...
    assert result.output.count("customers") >= 1
    assert result.output.count("orders") >= 1
    # ...but the narrowed schema context section keeps only the top match.
    context_section = result.output.split("retrieved schema context:")[1]
    assert "customers" in context_section
    assert "orders" not in context_section
    assert "events" not in context_section


def test_validate_accepts_a_safe_query(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main, ["validate", "SELECT * FROM orders", "--warehouse-dir", str(warehouse_dir)]
    )

    assert result.exit_code == 0, result.output
    assert "VALID" in result.output
    assert "LIMIT" in result.output


def test_validate_rejects_a_disallowed_table(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main, ["validate", "SELECT * FROM secret_table", "--warehouse-dir", str(warehouse_dir)]
    )

    assert result.exit_code == 1
    assert "REJECTED" in result.output
    assert "secret_table" in result.output


def test_validate_rejects_a_write_statement(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main, ["validate", "DELETE FROM orders", "--warehouse-dir", str(warehouse_dir)]
    )

    assert result.exit_code == 1
    assert "REJECTED" in result.output


def test_validate_respects_row_limit_option(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "validate",
            "SELECT * FROM orders",
            "--warehouse-dir",
            str(warehouse_dir),
            "--row-limit",
            "7",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "LIMIT 7" in result.output


def test_ask_with_yes_executes_and_prints_results(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main, ["ask", "how many customers", "--warehouse-dir", str(warehouse_dir), "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert "SQL:" in result.output
    assert "customer_count" in result.output
    assert "200" in result.output


def test_ask_window_function_question_runs_end_to_end(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["ask", "running total of revenue", "--warehouse-dir", str(warehouse_dir), "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert "running_total" in result.output


def test_ask_time_filter_question_runs_end_to_end(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["ask", "orders in the last 7 days", "--warehouse-dir", str(warehouse_dir), "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert "SQL:" in result.output


def test_ask_events_join_question_runs_end_to_end(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["ask", "events per customer", "--warehouse-dir", str(warehouse_dir), "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert "event_count" in result.output


def test_ask_top_k_tables_narrows_retrieved_tables_in_the_audit_trace(
    warehouse_dir: Path, tmp_path: Path
):
    audit_log = tmp_path / "trace.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "ask",
            "how many customers",
            "--warehouse-dir",
            str(warehouse_dir),
            "--yes",
            "--top-k-tables",
            "1",
            "--audit-log",
            str(audit_log),
        ],
    )

    assert result.exit_code == 0, result.output
    steps = AuditTrace.load(audit_log)
    schema_step = next(step for step in steps if step["step_type"] == "schema_retrieved")
    assert set(schema_step["payload"]["tables"]) == {"customers", "orders", "events"}
    assert schema_step["payload"]["retrieved_tables"] == ["customers"]


def test_ask_top_k_tables_narrowing_the_provider_context_does_not_narrow_the_allowlist(
    warehouse_dir: Path, tmp_path: Path
):
    """"order details" ranks only "orders" into the top-1 retrieved table
    (customers scores zero against that question), yet its scripted SQL
    joins orders with customers. Validation must still pass: the
    validator's allowlist is always the full catalog, independent of
    whatever subset of tables retrieval narrowed the provider's context to.
    """
    audit_log = tmp_path / "trace.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "ask",
            "order details",
            "--warehouse-dir",
            str(warehouse_dir),
            "--yes",
            "--top-k-tables",
            "1",
            "--audit-log",
            str(audit_log),
        ],
    )

    assert result.exit_code == 0, result.output
    steps = AuditTrace.load(audit_log)
    schema_step = next(step for step in steps if step["step_type"] == "schema_retrieved")
    assert schema_step["payload"]["retrieved_tables"] == ["orders"]
    validation_step = next(step for step in steps if step["step_type"] == "validation")
    assert validation_step["payload"]["is_valid"] is True


def test_ask_records_a_full_audit_trace_in_order(warehouse_dir: Path, tmp_path: Path):
    audit_log = tmp_path / "trace.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "ask",
            "how many customers",
            "--warehouse-dir",
            str(warehouse_dir),
            "--yes",
            "--audit-log",
            str(audit_log),
        ],
    )

    assert result.exit_code == 0, result.output
    steps = AuditTrace.load(audit_log)
    step_types = [step["step_type"] for step in steps]
    assert step_types == [
        "question_received",
        "schema_retrieved",
        "draft",
        "validation",
        "cost_estimate",
        "approval",
        "explain",
        "execution_result",
        "final",
    ]

    final = steps[-1]
    assert final["payload"]["outcome"] == "executed"
    assert final["payload"]["row_count"] == 1


def test_ask_unsupported_question_fails_without_executing_anything(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "ask",
            "what is the airspeed velocity of an unladen swallow",
            "--warehouse-dir",
            str(warehouse_dir),
            "--yes",
        ],
    )

    assert result.exit_code == 1
    assert "could not answer the question" in result.output


def test_ask_target_dialect_prints_translated_sql_without_executing(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "ask",
            "how many orders",
            "--warehouse-dir",
            str(warehouse_dir),
            "--yes",
            "--target-dialect",
            "bigquery",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "SELECT COUNT(*)" in result.output.upper() or "COUNT(*)" in result.output
    # a translate-only run must not reach the execution/results section.
    assert "estimated scan" not in result.output


def test_ask_target_dialect_records_translated_outcome_not_executed(
    warehouse_dir: Path, tmp_path: Path
):
    audit_log = tmp_path / "trace.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "ask",
            "how many orders",
            "--warehouse-dir",
            str(warehouse_dir),
            "--yes",
            "--target-dialect",
            "athena",
            "--audit-log",
            str(audit_log),
        ],
    )

    assert result.exit_code == 0, result.output
    steps = AuditTrace.load(audit_log)
    step_types = [step["step_type"] for step in steps]
    assert "execution_result" not in step_types
    assert "cost_estimate" not in step_types
    assert step_types[-1] == "final"
    assert steps[-1]["payload"]["outcome"] == "translated"


def test_ask_row_limit_option_is_applied_to_the_executed_query(
    warehouse_dir: Path, tmp_path: Path
):
    audit_log = tmp_path / "trace.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "ask",
            "orders by status",
            "--warehouse-dir",
            str(warehouse_dir),
            "--yes",
            "--row-limit",
            "2",
            "--audit-log",
            str(audit_log),
        ],
    )

    assert result.exit_code == 0, result.output
    steps = AuditTrace.load(audit_log)
    draft_step = next(step for step in steps if step["step_type"] == "validation")
    assert draft_step["payload"]["is_valid"] is True

    final_step = steps[-1]
    assert "LIMIT 2" in final_step["payload"]["sql"]


def test_ask_without_yes_on_a_low_cost_question_still_prompts_for_approval(
    warehouse_dir: Path,
):
    """README's quick start says dropping --yes shows a human-approval prompt.

    The sample warehouse is tiny, so its estimated cost sits far below the
    default $0.50 confirmation threshold. The documented promise is that
    approval is still required before anything executes -- it should not
    silently fall through to execution just because the query is cheap.
    """
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["ask", "how many customers", "--warehouse-dir", str(warehouse_dir)],
        input="",
    )

    output_before_results = result.output.split("customer_count")[0]
    assert "proceed" in output_before_results.lower() or "approve" in output_before_results.lower(), (
        "ask ran to completion without ever asking for approval, "
        "contradicting the README's documented behavior for dropping --yes:\n"
        + result.output
    )
