"""Tests for the Phase 5 evaluation harness: the benchmark question set and its scorer.

These drive `run_benchmark` -- the real agent loop, schema retrieval, SQL
validation, and the scan-cost estimator, none of it mocked -- against the
generated sample warehouse, exactly the way the bundled `benchmark` CLI
command does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from text_to_sql_agent.evaluation.benchmark import BENCHMARK, BenchmarkQuestion
from text_to_sql_agent.evaluation.runner import run_benchmark

# README/PROGRESS.md: 44 questions total, spanning this exact category/count
# breakdown and a 1-5 difficulty curve.
EXPECTED_CATEGORY_COUNTS = {
    "count": 5,
    "aggregate": 5,
    "groupby": 6,
    "join": 6,
    "top_n": 3,
    "time_filter": 3,
    "window": 3,
    "ambiguous": 6,
    "unanswerable": 7,
}


def test_benchmark_question_requires_expected_sql_for_success_outcome():
    with pytest.raises(ValueError, match="expected_sql"):
        BenchmarkQuestion("bad-1", "how many customers", "count", 1, "success")


def test_benchmark_question_rejects_expected_sql_for_non_success_outcome():
    with pytest.raises(ValueError, match="expected_sql"):
        BenchmarkQuestion(
            "bad-2",
            "who are our top performers",
            "ambiguous",
            1,
            "clarification",
            "SELECT 1",
        )


@pytest.mark.parametrize("difficulty", [0, 6, -1])
def test_benchmark_question_rejects_difficulty_outside_one_to_five(difficulty: int):
    with pytest.raises(ValueError, match="difficulty"):
        BenchmarkQuestion("bad-3", "how many orders", "count", difficulty, "success", "SELECT 1")


def test_benchmark_question_accepts_a_well_formed_success_question():
    question = BenchmarkQuestion("ok-1", "how many orders", "count", 1, "success", "SELECT 1")
    assert question.expected_sql == "SELECT 1"


def test_benchmark_question_accepts_a_well_formed_non_success_question():
    question = BenchmarkQuestion("ok-2", "what's the weather", "unanswerable", 1, "unanswerable")
    assert question.expected_sql is None


def test_benchmark_has_the_documented_total_and_category_breakdown():
    assert len(BENCHMARK) == 44

    counts: dict[str, int] = {}
    for question in BENCHMARK:
        counts[question.category] = counts.get(question.category, 0) + 1
    assert counts == EXPECTED_CATEGORY_COUNTS


def test_benchmark_question_ids_are_unique():
    ids = [question.id for question in BENCHMARK]
    assert len(ids) == len(set(ids))


def test_benchmark_difficulties_are_all_in_range_and_cover_the_curve():
    difficulties = {question.difficulty for question in BENCHMARK}
    assert difficulties <= {1, 2, 3, 4, 5}
    assert all(1 <= question.difficulty <= 5 for question in BENCHMARK)


def test_benchmark_success_questions_all_carry_expected_sql_others_do_not():
    for question in BENCHMARK:
        if question.expected_outcome == "success":
            assert question.expected_sql, question.id
        else:
            assert question.expected_sql is None, question.id


def test_run_benchmark_passes_every_bundled_question_end_to_end(warehouse_dir: Path):
    """The evaluation harness's own headline claim (PROGRESS.md): 44/44 pass
    against the real sample warehouse through the real draft/validate/EXPLAIN
    loop -- nothing in this call is mocked."""
    report = run_benchmark(BENCHMARK, warehouse_dir)

    assert report.total == 44
    assert report.passed == 44
    assert report.accuracy == 1.0
    assert report.failures() == ()

    by_category = report.by_category()
    assert sum(total for _, total in by_category.values()) == 44
    assert all(passed == total for passed, total in by_category.values())

    by_difficulty = report.by_difficulty()
    assert sum(total for _, total in by_difficulty.values()) == 44
    assert list(by_difficulty) == sorted(by_difficulty)


def test_run_benchmark_success_questions_get_a_cost_estimate_others_do_not(warehouse_dir: Path):
    report = run_benchmark(BENCHMARK, warehouse_dir)
    by_id = {result.question_id: result for result in report.results}

    success_ids = [q.id for q in BENCHMARK if q.expected_outcome == "success"]
    other_ids = [q.id for q in BENCHMARK if q.expected_outcome != "success"]

    for question_id in success_ids:
        result = by_id[question_id]
        assert result.result_match is True
        assert result.bytes_scanned is not None
        assert result.cost_usd is not None

    for question_id in other_ids:
        result = by_id[question_id]
        assert result.result_match is None
        assert result.bytes_scanned is None
        assert result.cost_usd is None


def test_run_benchmark_detects_a_wrong_expected_sql_as_a_result_mismatch(warehouse_dir: Path):
    """PROGRESS.md: scoring was verified to not be vacuous by feeding a
    question with a deliberately wrong `expected_sql` (counting `orders`
    instead of `customers`) and confirming it is reported as a failure,
    not a pass. This is that check, made automatic."""
    wrong_question = BenchmarkQuestion(
        "wrong-01",
        "how many customers do we have",
        "count",
        1,
        "success",
        "SELECT COUNT(*) AS n FROM orders",
    )

    report = run_benchmark((wrong_question,), warehouse_dir)

    assert report.total == 1
    assert report.passed == 0
    result = report.results[0]
    assert result.outcome_correct is True  # right kind of outcome: it did succeed
    assert result.result_match is False  # but the rows are wrong
    assert not result.correct
    assert "row mismatch" in (result.detail or "")
    assert report.failures() == (result,)


def test_run_benchmark_ambiguous_questions_resolve_to_clarification_outcome(warehouse_dir: Path):
    ambiguous_questions = tuple(q for q in BENCHMARK if q.category == "ambiguous")
    report = run_benchmark(ambiguous_questions, warehouse_dir)

    assert report.total == 6
    for result in report.results:
        assert result.actual_outcome == "clarification"
        assert result.outcome_correct is True
        assert result.result_match is None


def test_run_benchmark_unanswerable_questions_resolve_to_unanswerable_outcome(warehouse_dir: Path):
    unanswerable_questions = tuple(q for q in BENCHMARK if q.category == "unanswerable")
    report = run_benchmark(unanswerable_questions, warehouse_dir)

    assert report.total == 7
    for result in report.results:
        assert result.actual_outcome == "unanswerable"
        assert result.outcome_correct is True


def test_run_benchmark_pricing_model_changes_total_cost(warehouse_dir: Path):
    aggregate_questions = tuple(q for q in BENCHMARK if q.category == "aggregate")

    athena_report = run_benchmark(aggregate_questions, warehouse_dir, pricing_model="athena")
    bigquery_report = run_benchmark(aggregate_questions, warehouse_dir, pricing_model="bigquery")

    assert athena_report.pricing_model_name == "athena"
    assert bigquery_report.pricing_model_name == "bigquery"
    # same bytes scanned both times, but bigquery's $6.25/TiB costs more than athena's $5/TiB.
    assert bigquery_report.total_cost_usd > athena_report.total_cost_usd


def test_benchmark_report_to_dict_is_json_serializable_and_matches_the_report(
    warehouse_dir: Path,
):
    count_questions = tuple(q for q in BENCHMARK if q.category == "count")
    report = run_benchmark(count_questions, warehouse_dir)

    payload = report.to_dict()
    serialized = json.dumps(payload)  # must not raise
    reloaded = json.loads(serialized)

    assert reloaded["total"] == report.total
    assert reloaded["passed"] == report.passed
    assert reloaded["accuracy"] == report.accuracy
    assert reloaded["pricing_model"] == report.pricing_model_name
    assert len(reloaded["results"]) == report.total
    assert {r["id"] for r in reloaded["results"]} == {q.id for q in count_questions}
