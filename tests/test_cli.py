"""End-to-end tests for the CLI documented in README's Usage section.

These drive the real `text-to-sql-agent` entry point (via click's
CliRunner) against a generated sample warehouse, with no module of the
project's own mocked out -- only the actual ScriptedProvider answers
questions, exactly as a user running the documented commands would see.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from text_to_sql_agent.audit import AuditTrace
from text_to_sql_agent.cli import main
from text_to_sql_agent.evaluation.benchmark import BENCHMARK, BenchmarkQuestion


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


def test_estimate_cost_prints_total_bytes_and_usd_estimate(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["estimate-cost", "SELECT * FROM customers", "--warehouse-dir", str(warehouse_dir)],
    )

    assert result.exit_code == 0, result.output
    assert "total:" in result.output
    assert "bytes" in result.output
    assert "athena pricing" in result.output
    assert "customers:" in result.output
    assert "columns: all" in result.output


def test_estimate_cost_rejects_invalid_sql_without_printing_an_estimate(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["estimate-cost", "DELETE FROM orders", "--warehouse-dir", str(warehouse_dir)],
    )

    assert result.exit_code == 1
    assert "REJECTED" in result.output
    assert "total:" not in result.output


def test_estimate_cost_rejects_a_disallowed_table(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["estimate-cost", "SELECT * FROM secret_table", "--warehouse-dir", str(warehouse_dir)],
    )

    assert result.exit_code == 1
    assert "REJECTED" in result.output


def test_estimate_cost_narrow_projection_lists_specific_columns_not_all(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "estimate-cost",
            "SELECT o.order_id, c.name FROM orders o JOIN customers c "
            "ON o.customer_id = c.customer_id",
            "--warehouse-dir",
            str(warehouse_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "columns: all" not in result.output
    assert "order_id" in result.output
    assert "name" in result.output


def test_estimate_cost_partition_filter_narrows_partitions_scanned(warehouse_dir: Path):
    import datetime

    today = datetime.date.today().isoformat()
    runner = CliRunner()

    unfiltered = runner.invoke(
        main,
        ["estimate-cost", "SELECT * FROM events", "--warehouse-dir", str(warehouse_dir)],
    )
    filtered = runner.invoke(
        main,
        [
            "estimate-cost",
            f"SELECT * FROM events WHERE event_date = '{today}'",
            "--warehouse-dir",
            str(warehouse_dir),
        ],
    )

    assert unfiltered.exit_code == 0, unfiltered.output
    assert filtered.exit_code == 0, filtered.output
    assert "partitions scanned: 30" in unfiltered.output
    assert "partitions scanned: 1" in filtered.output


def test_estimate_cost_pricing_model_option_changes_the_dollar_estimate(warehouse_dir: Path):
    runner = CliRunner()
    athena = runner.invoke(
        main,
        [
            "estimate-cost",
            "SELECT * FROM orders",
            "--warehouse-dir",
            str(warehouse_dir),
            "--pricing-model",
            "athena",
        ],
    )
    bigquery = runner.invoke(
        main,
        [
            "estimate-cost",
            "SELECT * FROM orders",
            "--warehouse-dir",
            str(warehouse_dir),
            "--pricing-model",
            "bigquery",
        ],
    )

    assert athena.exit_code == 0, athena.output
    assert bigquery.exit_code == 0, bigquery.output
    assert "athena pricing" in athena.output
    assert "bigquery pricing" in bigquery.output
    # same bytes scanned, but bigquery's $6.25/TiB costs more than athena's $5/TiB.
    athena_cost = float(athena.output.split("~$")[1].split(",")[0])
    bigquery_cost = float(bigquery.output.split("~$")[1].split(",")[0])
    assert bigquery_cost > athena_cost


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

    cost_step = next(step for step in steps if step["step_type"] == "cost_estimate")
    assert cost_step["payload"]["pricing_model"] == "athena"
    assert "customers" in cost_step["payload"]["per_table"]
    assert cost_step["payload"]["per_table"]["customers"]["bytes_scanned"] > 0


def test_ask_max_bytes_scanned_refuses_to_run_and_does_not_execute(
    warehouse_dir: Path, tmp_path: Path
):
    """PROGRESS.md: `ask --max-bytes-scanned 10` must correctly refuse to run."""
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
            "--max-bytes-scanned",
            "10",
            "--audit-log",
            str(audit_log),
        ],
    )

    assert result.exit_code == 1
    assert "refusing to run" in result.output.lower()

    steps = AuditTrace.load(audit_log)
    step_types = [step["step_type"] for step in steps]
    assert "execution_result" not in step_types
    assert step_types[-1] == "final"
    assert steps[-1]["payload"]["outcome"] == "budget_exceeded"


def test_ask_max_cost_usd_refuses_to_run_and_does_not_execute(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "ask",
            "how many customers",
            "--warehouse-dir",
            str(warehouse_dir),
            "--yes",
            "--max-cost-usd",
            "0.0000001",
        ],
    )

    assert result.exit_code == 1
    assert "refusing to run" in result.output.lower()
    assert "customer_count" not in result.output


def test_ask_pricing_model_option_is_recorded_in_the_audit_trace(
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
            "--pricing-model",
            "bigquery",
            "--audit-log",
            str(audit_log),
        ],
    )

    assert result.exit_code == 0, result.output
    steps = AuditTrace.load(audit_log)
    cost_step = next(step for step in steps if step["step_type"] == "cost_estimate")
    assert cost_step["payload"]["pricing_model"] == "bigquery"


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


def test_ask_ambiguous_question_declines_and_exits_nonzero(warehouse_dir: Path):
    """README: an ambiguous question is surfaced as "question is ambiguous: ..."
    and exits non-zero rather than running a query that might answer a
    different question than the one asked."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["ask", "who are our top performers", "--warehouse-dir", str(warehouse_dir), "--yes"],
    )

    assert result.exit_code == 1
    assert "question is ambiguous" in result.output
    assert "SQL:" not in result.output


def test_ask_ambiguous_question_records_needs_clarification_outcome(
    warehouse_dir: Path, tmp_path: Path
):
    audit_log = tmp_path / "trace.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "ask",
            "how is the business doing",
            "--warehouse-dir",
            str(warehouse_dir),
            "--yes",
            "--audit-log",
            str(audit_log),
        ],
    )

    assert result.exit_code == 1
    steps = AuditTrace.load(audit_log)
    step_types = [step["step_type"] for step in steps]
    assert "needs_clarification" in step_types
    assert "execution_result" not in step_types
    final = steps[-1]
    assert final["payload"]["outcome"] == "needs_clarification"
    assert final["payload"]["reason"]


def test_benchmark_default_run_passes_every_bundled_question(warehouse_dir: Path):
    """PROGRESS.md: the bundled 44-question benchmark passes 100% against
    the real sample warehouse -- this is the evaluation harness's own
    headline claim, exercised end to end with nothing mocked."""
    runner = CliRunner()
    result = runner.invoke(main, ["benchmark", "--warehouse-dir", str(warehouse_dir)])

    assert result.exit_code == 0, result.output
    total = len(BENCHMARK)
    assert f"{total}/{total} passed (100%)" in result.output
    assert "by category:" in result.output
    assert "by difficulty:" in result.output
    assert "failing question(s)" not in result.output


def test_benchmark_category_filter_restricts_report_to_requested_categories(
    warehouse_dir: Path,
):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "benchmark",
            "--warehouse-dir",
            str(warehouse_dir),
            "--category",
            "ambiguous",
            "--category",
            "unanswerable",
        ],
    )

    assert result.exit_code == 0, result.output
    expected_total = sum(
        1 for q in BENCHMARK if q.category in {"ambiguous", "unanswerable"}
    )
    assert f"{expected_total}/{expected_total} passed (100%)" in result.output
    category_section = result.output.split("by category:")[1].split("by difficulty:")[0]
    assert "ambiguous" in category_section
    assert "unanswerable" in category_section
    assert "count" not in category_section
    assert "aggregate" not in category_section


def test_benchmark_unknown_category_is_a_usage_error(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["benchmark", "--warehouse-dir", str(warehouse_dir), "--category", "not-a-real-category"],
    )

    assert result.exit_code != 0
    assert "no benchmark questions match category filter" in result.output


def test_benchmark_output_option_writes_a_valid_json_report(
    warehouse_dir: Path, tmp_path: Path
):
    report_path = tmp_path / "report.json"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "benchmark",
            "--warehouse-dir",
            str(warehouse_dir),
            "--category",
            "count",
            "--output",
            str(report_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert report_path.is_file()
    report = json.loads(report_path.read_text())
    assert report["total"] == sum(1 for q in BENCHMARK if q.category == "count")
    assert report["passed"] == report["total"]
    assert report["accuracy"] == 1.0
    assert len(report["results"]) == report["total"]
    assert all(r["category"] == "count" for r in report["results"])


def test_benchmark_pricing_model_option_changes_estimated_cost(warehouse_dir: Path):
    runner = CliRunner()
    athena = runner.invoke(
        main,
        [
            "benchmark",
            "--warehouse-dir",
            str(warehouse_dir),
            "--category",
            "aggregate",
            "--pricing-model",
            "athena",
        ],
    )
    bigquery = runner.invoke(
        main,
        [
            "benchmark",
            "--warehouse-dir",
            str(warehouse_dir),
            "--category",
            "aggregate",
            "--pricing-model",
            "bigquery",
        ],
    )

    assert athena.exit_code == 0, athena.output
    assert bigquery.exit_code == 0, bigquery.output
    assert "(athena pricing)" in athena.output
    assert "(bigquery pricing)" in bigquery.output

    def _cost(output: str) -> float:
        return float(output.split("est. cost $")[1].split(" ")[0])

    assert _cost(bigquery.output) > _cost(athena.output)


def test_benchmark_reports_failing_questions_and_exits_nonzero(
    monkeypatch, warehouse_dir: Path, tmp_path: Path
):
    """Swaps in a single bundled question with a deliberately wrong
    expected_sql (same technique PROGRESS.md describes using to confirm
    scoring isn't vacuous) to check the CLI's own failure-reporting path --
    exit code, the failing-question listing, and a JSON report that
    reflects the failure -- without mocking anything the pipeline itself
    runs through."""
    wrong_question = BenchmarkQuestion(
        "count-01",
        "how many customers do we have",
        "count",
        1,
        "success",
        "SELECT COUNT(*) AS n FROM orders",  # wrong table on purpose
    )
    monkeypatch.setattr("text_to_sql_agent.cli.BENCHMARK", (wrong_question,))

    report_path = tmp_path / "report.json"
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["benchmark", "--warehouse-dir", str(warehouse_dir), "--output", str(report_path)],
    )

    assert result.exit_code == 1
    assert "0/1 passed (0%)" in result.output
    assert "1 failing question(s):" in result.output
    assert "count-01" in result.output

    report = json.loads(report_path.read_text())
    assert report["passed"] == 0
    assert report["results"][0]["result_match"] is False


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
