"""Tests for run_agent_loop: the draft/validate/EXPLAIN/retry loop, independent of the CLI.

PROGRESS.md's resume point describes exact, checkable behavior: an engine
error (unknown column, type mismatch) must become a retry rather than an
unhandled crash, bounded by ``max_correction_attempts``, and the table
allowlist used for validation must always be the full catalog regardless
of how much the schema context handed to the provider was narrowed.
"""

from __future__ import annotations

import duckdb
import pytest

from text_to_sql_agent.agent import run_agent_loop
from text_to_sql_agent.audit import AuditTrace
from text_to_sql_agent.config import AgentConfig
from text_to_sql_agent.engines.base import EngineExecutionError
from text_to_sql_agent.llm.base import CorrectionContext, LLMProvider
from text_to_sql_agent.llm.scripted import ScriptedProvider
from text_to_sql_agent.warehouse.load import load_into_duckdb

TABLE_ALLOWLIST = ("customers", "orders", "events")


class _FixedProvider(LLMProvider):
    """Always drafts the same SQL, ignoring any correction it is handed."""

    def __init__(self, sql: str) -> None:
        self._sql = sql
        self.corrections: list[CorrectionContext | None] = []

    def draft_sql(self, question, schema_context, dialect="duckdb", correction=None):
        self.corrections.append(correction)
        return self._sql


class _SequenceProvider(LLMProvider):
    """Drafts a fixed sequence of SQL strings, one per call, in order."""

    def __init__(self, sql_sequence: list[str]) -> None:
        self._sequence = list(sql_sequence)
        self.corrections: list[CorrectionContext | None] = []

    def draft_sql(self, question, schema_context, dialect="duckdb", correction=None):
        self.corrections.append(correction)
        return self._sequence[len(self.corrections) - 1]


@pytest.fixture()
def connection(warehouse_info):
    conn = duckdb.connect()
    load_into_duckdb(conn, warehouse_info)
    try:
        yield conn
    finally:
        conn.close()


def test_successful_question_returns_sql_and_explain_plan_on_first_attempt(connection):
    trace = AuditTrace()
    result = run_agent_loop(
        ScriptedProvider(),
        "how many customers",
        "",
        TABLE_ALLOWLIST,
        "duckdb",
        connection,
        AgentConfig(),
        trace,
    )
    assert result.succeeded
    assert result.attempts == 1
    assert result.explain_plan
    assert result.validation is not None
    assert "LIMIT" in result.validation.normalized_sql.upper()


def test_successful_run_records_draft_then_validation_in_order(connection):
    trace = AuditTrace()
    run_agent_loop(
        ScriptedProvider(),
        "how many orders",
        "",
        TABLE_ALLOWLIST,
        "duckdb",
        connection,
        AgentConfig(),
        trace,
    )
    step_types = [step.step_type for step in trace.steps]
    assert step_types == ["draft", "validation"]
    assert trace.steps[1].payload["is_valid"] is True


def test_unsupported_question_sets_draft_error_without_validating(connection):
    trace = AuditTrace()
    result = run_agent_loop(
        ScriptedProvider(),
        "what is the airspeed velocity of an unladen swallow",
        "",
        TABLE_ALLOWLIST,
        "duckdb",
        connection,
        AgentConfig(),
        trace,
    )
    assert not result.succeeded
    assert result.draft_error is not None
    assert [step.step_type for step in trace.steps] == ["draft_failed"]


def test_table_allowlist_is_independent_of_the_narrowed_schema_context(connection):
    """Retrieval may narrow what the provider sees, but validation must
    always check against the full catalog's table allowlist, not whatever
    subset of tables happened to be in schema_context."""
    trace = AuditTrace()
    provider = _FixedProvider("SELECT COUNT(*) FROM orders")
    result = run_agent_loop(
        provider,
        "irrelevant question",
        "customers\n  - customer_id INTEGER",  # schema_context narrowed away from orders
        TABLE_ALLOWLIST,
        "duckdb",
        connection,
        AgentConfig(),
        trace,
    )
    assert result.succeeded
    assert result.validation.referenced_tables == ("orders",)


def test_validator_rejection_retries_with_a_correction_context(connection):
    trace = AuditTrace()
    provider = _SequenceProvider(
        ["SELECT * FROM secret_table", "SELECT COUNT(*) FROM customers"]
    )
    result = run_agent_loop(
        provider,
        "how many customers",
        "",
        TABLE_ALLOWLIST,
        "duckdb",
        connection,
        AgentConfig(max_correction_attempts=3),
        trace,
    )
    assert result.succeeded
    assert result.attempts == 2
    assert provider.corrections[0] is None
    assert provider.corrections[1] is not None
    assert "secret_table" in provider.corrections[1].error_message
    step_types = [step.step_type for step in trace.steps]
    assert step_types == ["draft", "validation", "draft", "validation"]


def test_engine_error_is_caught_inside_the_loop_as_a_retry_not_raised(connection):
    trace = AuditTrace()
    provider = _FixedProvider("SELECT nonexistent_column FROM customers")
    config = AgentConfig(max_correction_attempts=2)

    result = run_agent_loop(
        provider,
        "a question with a bad column",
        "",
        TABLE_ALLOWLIST,
        "duckdb",
        connection,
        config,
        trace,
    )

    assert not result.succeeded
    assert result.attempts == config.max_correction_attempts + 1
    engine_error_steps = [step for step in trace.steps if step.step_type == "engine_error"]
    assert len(engine_error_steps) == config.max_correction_attempts + 1
    assert result.validation is not None
    assert "nonexistent_column" in result.validation.errors[0]
    # every retry after the first was fed the previous engine error as correction
    assert all(c is not None for c in provider.corrections[1:])
    assert "nonexistent_column" in provider.corrections[1].error_message


def test_engine_execution_error_from_a_cloud_adapter_is_caught_as_a_retry(connection):
    """Phase 4 widened run_agent_loop's `except duckdb.Error` to
    `except (duckdb.Error, EngineExecutionError)` so the Athena/BigQuery
    adapters' own normalized exception is treated as a retryable engine
    error exactly like duckdb's, instead of propagating unhandled past the
    loop (PROGRESS.md Phase 4)."""

    class _ExplainFailingConnection:
        def execute(self, sql):
            raise EngineExecutionError("engine rejected the query")

    trace = AuditTrace()
    provider = _FixedProvider("SELECT COUNT(*) FROM customers")
    config = AgentConfig(max_correction_attempts=1)

    result = run_agent_loop(
        provider,
        "how many customers",
        "",
        TABLE_ALLOWLIST,
        "athena",
        _ExplainFailingConnection(),
        config,
        trace,
    )

    assert not result.succeeded
    assert result.attempts == config.max_correction_attempts + 1
    engine_error_steps = [step for step in trace.steps if step.step_type == "engine_error"]
    assert len(engine_error_steps) == config.max_correction_attempts + 1
    assert result.validation is not None
    assert "engine rejected the query" in result.validation.errors[0]


def test_scripted_provider_repeats_identical_sql_on_every_correction_attempt(connection):
    """ScriptedProvider ignores correction content by design (PROGRESS.md),
    so retrying against it is deterministic: it resends the same rejected
    SQL on every attempt and exhausts the retry budget without ever fixing
    anything."""
    trace = AuditTrace()
    config = AgentConfig(max_correction_attempts=2)
    result = run_agent_loop(
        ScriptedProvider(),
        "how many customers",
        "",
        ("orders", "events"),  # customers deliberately excluded from the allowlist
        "duckdb",
        connection,
        config,
        trace,
    )
    assert not result.succeeded
    assert result.attempts == config.max_correction_attempts + 1
    drafts = [step.payload["sql"] for step in trace.steps if step.step_type == "draft"]
    assert len(drafts) == config.max_correction_attempts + 1
    assert len(set(drafts)) == 1


def test_max_correction_attempts_bounds_total_attempts(connection):
    trace = AuditTrace()
    provider = _FixedProvider("SELECT * FROM secret_table")
    config = AgentConfig(max_correction_attempts=1)
    result = run_agent_loop(
        provider,
        "q",
        "",
        TABLE_ALLOWLIST,
        "duckdb",
        connection,
        config,
        trace,
    )
    assert result.attempts == 2  # one initial draft plus one retry
    assert not result.succeeded
    assert len(provider.corrections) == 2


def test_ambiguous_question_sets_needs_clarification_without_consuming_retries(connection):
    """PROGRESS.md Phase 5: ClarificationNeededError is caught the same way
    UnsupportedQuestionError already was -- no retry, immediate return --
    and distinct from draft_error (which means "no rule matched at all")."""
    trace = AuditTrace()
    config = AgentConfig(max_correction_attempts=3)
    result = run_agent_loop(
        ScriptedProvider(),
        "who are our top performers",
        "",
        TABLE_ALLOWLIST,
        "duckdb",
        connection,
        config,
        trace,
    )

    assert not result.succeeded
    assert result.needs_clarification is not None
    assert result.draft_error is None
    assert result.validation is None
    assert result.sql is None
    assert result.attempts == 1
    assert [step.step_type for step in trace.steps] == ["needs_clarification"]


def test_needs_clarification_audit_step_carries_reason_and_options(connection):
    trace = AuditTrace()
    result = run_agent_loop(
        ScriptedProvider(),
        "how is the business doing",
        "",
        TABLE_ALLOWLIST,
        "duckdb",
        connection,
        AgentConfig(),
        trace,
    )
    step = trace.steps[0]
    assert step.step_type == "needs_clarification"
    assert step.payload["reason"] == result.needs_clarification
    assert step.payload["options"], "clarification options should be non-empty"


def test_zero_correction_attempts_means_a_single_try(connection):
    trace = AuditTrace()
    provider = _FixedProvider("SELECT * FROM secret_table")
    config = AgentConfig(max_correction_attempts=0)
    result = run_agent_loop(
        provider,
        "q",
        "",
        TABLE_ALLOWLIST,
        "duckdb",
        connection,
        config,
        trace,
    )
    assert result.attempts == 1
    assert not result.succeeded
    assert len(provider.corrections) == 1
