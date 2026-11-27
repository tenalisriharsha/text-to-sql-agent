"""Runs the benchmark through the real agent loop and scores the outcome.

Every question goes through exactly the same path `ask` does -- schema
retrieval narrows the catalog, `run_agent_loop` drafts/validates/EXPLAINs
against the local DuckDB warehouse -- so this is the first thing in the
project that calls `run_agent_loop` dozens of times in a row rather than
once per CLI invocation; anything non-deterministic in retrieval or the
retry loop would show up here first.

Scoring has two independent axes, matching the plan: did the agent reach
the *right kind* of outcome (answered / asked for clarification / declined
as unanswerable), and, when it answered, did it return the *right rows*
(checked against the question's own ``expected_sql``, run directly against
the same connection, not against anything `ScriptedProvider` produced).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb

from text_to_sql_agent.agent import run_agent_loop
from text_to_sql_agent.audit import AuditTrace
from text_to_sql_agent.config import AgentConfig, PricingModel, pricing_model_for
from text_to_sql_agent.cost_estimate import TableLocation, estimate_bytes_scanned
from text_to_sql_agent.evaluation.benchmark import BenchmarkQuestion
from text_to_sql_agent.llm.scripted import ScriptedProvider
from text_to_sql_agent.retrieval import SchemaRetriever
from text_to_sql_agent.schema_catalog import SchemaCatalog
from text_to_sql_agent.warehouse.generate import table_locations_for, warehouse_info_for
from text_to_sql_agent.warehouse.load import build_catalog, load_into_duckdb

ActualOutcome = str  # "success" | "clarification" | "unanswerable" | "unresolved"


@dataclass(frozen=True)
class QuestionResult:
    """The scored outcome of running one benchmark question."""

    question_id: str
    category: str
    difficulty: int
    expected_outcome: str
    actual_outcome: ActualOutcome
    outcome_correct: bool
    result_match: bool | None
    attempts: int
    bytes_scanned: int | None = None
    cost_usd: float | None = None
    detail: str | None = None

    @property
    def correct(self) -> bool:
        """Whether this question counts as fully passed.

        The outcome category must match, and, when a result set was
        checked at all (only "success" questions get one), it must match
        too -- a question that merely *ran* without error but returned the
        wrong rows is not a pass.
        """
        return self.outcome_correct and self.result_match is not False


@dataclass(frozen=True)
class BenchmarkReport:
    """Aggregate scoring over every question in a benchmark run."""

    results: tuple[QuestionResult, ...]
    pricing_model_name: str

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.correct)

    @property
    def accuracy(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def mean_attempts(self) -> float:
        return sum(result.attempts for result in self.results) / self.total if self.total else 0.0

    @property
    def total_cost_usd(self) -> float:
        return sum(result.cost_usd for result in self.results if result.cost_usd is not None)

    def by_category(self) -> dict[str, tuple[int, int]]:
        """``{category: (passed, total)}``, in first-seen category order."""
        return self._group(lambda result: result.category)

    def by_difficulty(self) -> dict[int, tuple[int, int]]:
        """``{difficulty: (passed, total)}``, sorted by difficulty."""
        grouped = self._group(lambda result: result.difficulty)
        return dict(sorted(grouped.items()))

    def _group(self, key_fn) -> dict:
        totals: dict = {}
        passed: dict = {}
        for result in self.results:
            key = key_fn(result)
            totals[key] = totals.get(key, 0) + 1
            if result.correct:
                passed[key] = passed.get(key, 0) + 1
        return {key: (passed.get(key, 0), total) for key, total in totals.items()}

    def failures(self) -> tuple[QuestionResult, ...]:
        return tuple(result for result in self.results if not result.correct)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "accuracy": self.accuracy,
            "mean_attempts": self.mean_attempts,
            "total_cost_usd": self.total_cost_usd,
            "pricing_model": self.pricing_model_name,
            "by_category": {
                category: {"passed": passed, "total": total}
                for category, (passed, total) in self.by_category().items()
            },
            "by_difficulty": {
                str(difficulty): {"passed": passed, "total": total}
                for difficulty, (passed, total) in self.by_difficulty().items()
            },
            "results": [
                {
                    "id": result.question_id,
                    "category": result.category,
                    "difficulty": result.difficulty,
                    "expected_outcome": result.expected_outcome,
                    "actual_outcome": result.actual_outcome,
                    "correct": result.correct,
                    "result_match": result.result_match,
                    "attempts": result.attempts,
                    "bytes_scanned": result.bytes_scanned,
                    "cost_usd": result.cost_usd,
                    "detail": result.detail,
                }
                for result in self.results
            ],
        }


def run_benchmark(
    questions: Sequence[BenchmarkQuestion],
    warehouse_dir: Path,
    config: AgentConfig | None = None,
    pricing_model: str = "athena",
) -> BenchmarkReport:
    """Run every question in ``questions`` through the real agent loop and score it.

    Always runs against the local DuckDB sample warehouse at
    ``warehouse_dir`` (``dialect="duckdb"``) -- the benchmark measures the
    pipeline's own behavior (retrieval, drafting, validation, the retry
    loop, the scan-cost guard), not any particular cloud account, and
    reuses one connection for every question since none of them mutate
    the warehouse.
    """
    config = config or AgentConfig()
    pricing = pricing_model_for(pricing_model)

    catalog = build_catalog(warehouse_dir)
    retriever = SchemaRetriever(catalog)
    provider = ScriptedProvider()
    locations = table_locations_for(warehouse_dir)

    connection = duckdb.connect()
    try:
        load_into_duckdb(connection, warehouse_info_for(warehouse_dir))
        results = tuple(
            _run_one(question, connection, catalog, retriever, provider, config, locations, pricing)
            for question in questions
        )
    finally:
        connection.close()

    return BenchmarkReport(results=results, pricing_model_name=pricing.name)


def _run_one(
    question: BenchmarkQuestion,
    connection: duckdb.DuckDBPyConnection,
    catalog: SchemaCatalog,
    retriever: SchemaRetriever,
    provider: ScriptedProvider,
    config: AgentConfig,
    locations: dict[str, TableLocation],
    pricing: PricingModel,
) -> QuestionResult:
    trace = AuditTrace()
    retrieved = retriever.retrieve(
        question.question,
        top_k_tables=config.retrieval_top_k_tables,
        top_k_columns_per_table=config.retrieval_top_k_columns_per_table,
    )
    agent_result = run_agent_loop(
        provider,
        question.question,
        retrieved.to_text(),
        catalog.table_names,
        "duckdb",
        connection,
        config,
        trace,
    )

    if agent_result.needs_clarification is not None:
        actual_outcome = "clarification"
    elif agent_result.draft_error is not None:
        actual_outcome = "unanswerable"
    elif agent_result.succeeded:
        actual_outcome = "success"
    else:
        actual_outcome = "unresolved"

    outcome_correct = actual_outcome == question.expected_outcome
    result_match: bool | None = None
    bytes_scanned: int | None = None
    cost_usd: float | None = None
    detail: str | None = None

    if question.expected_outcome == "success" and actual_outcome == "success":
        assert agent_result.validation is not None
        final_sql = agent_result.validation.normalized_sql
        assert final_sql is not None and question.expected_sql is not None

        actual_rows = sorted(map(tuple, connection.execute(final_sql).fetchall()))
        expected_rows = sorted(map(tuple, connection.execute(question.expected_sql).fetchall()))
        result_match = actual_rows == expected_rows
        if not result_match:
            detail = f"row mismatch: expected {len(expected_rows)} rows, got {len(actual_rows)}"

        scan = estimate_bytes_scanned(final_sql, "duckdb", locations)
        bytes_scanned = scan.total_bytes
        cost_usd = pricing.estimate_cost_usd(bytes_scanned)
    elif not outcome_correct:
        detail = f"expected {question.expected_outcome!r}, got {actual_outcome!r}"
        if agent_result.draft_error:
            detail += f" ({agent_result.draft_error})"
        elif agent_result.needs_clarification:
            detail += f" ({agent_result.needs_clarification})"

    return QuestionResult(
        question_id=question.id,
        category=question.category,
        difficulty=question.difficulty,
        expected_outcome=question.expected_outcome,
        actual_outcome=actual_outcome,
        outcome_correct=outcome_correct,
        result_match=result_match,
        attempts=agent_result.attempts,
        bytes_scanned=bytes_scanned,
        cost_usd=cost_usd,
        detail=detail,
    )
