"""Tests for the ScriptedProvider: the deterministic, no-network LLM stand-in.

This provider is the project's documented way to exercise the full
draft/validate pipeline without an API key, so every rule the README/CLI
help implies must exist (counts, totals, group-bys, a join, a top-N query)
needs to actually produce valid SQL for the sample warehouse.
"""

from __future__ import annotations

import pytest

from text_to_sql_agent.llm.base import CorrectionContext, LLMProvider
from text_to_sql_agent.llm.scripted import (
    ClarificationNeededError,
    ScriptedProvider,
    UnsupportedQuestionError,
)
from text_to_sql_agent.sql.validator import validate_query

TABLE_ALLOWLIST = ("customers", "orders", "events")

# The six phrasings README/PROGRESS.md document as deliberately ambiguous:
# recognizable, but with no stated metric, time window, or table.
AMBIGUOUS_QUESTIONS = [
    "who are our top performers",
    "best performers this month",
    "show me recent activity",
    "latest activity",
    "how is the business doing",
    "how's the business doing",
    "business doing well",
    "what are last month's numbers",
    "last month numbers",
    "give me a customer activity summary",
    "activity summary for customers",
    "how much have we grown",
    "our growth",
    "growth this quarter",
]


def test_scripted_provider_is_an_llm_provider():
    assert isinstance(ScriptedProvider(), LLMProvider)


@pytest.mark.parametrize(
    "question",
    [
        "how many customers",
        "How Many Customers?",
        "  how   many   customers  ",
        "how many orders",
        "what is the total revenue",
        "total sales",
        "total amount",
        "customers by country",
        "customers per country",
        "orders by status",
        "orders per status",
        "top customers",
        "who are the biggest spenders",
        "highest spending customers",
        "events by type",
        "events per type",
        "orders with customer names",
        "give me order details",
        "average order value",
        "average order amount",
        "orders in the last 7 days",
        "orders from the last week",
        "recent orders",
        "running total of revenue",
        "cumulative revenue",
        "running total",
        "events per customer",
        "event count by customer",
        "events by customer",
    ],
)
def test_recognized_questions_draft_sql_that_passes_validation(question: str):
    provider = ScriptedProvider()
    sql = provider.draft_sql(question, schema_context="", dialect="duckdb")

    result = validate_query(sql, table_allowlist=TABLE_ALLOWLIST, dialect="duckdb")
    assert result.is_valid, result.errors


def test_unrecognized_question_raises_unsupported_question_error():
    provider = ScriptedProvider()
    with pytest.raises(UnsupportedQuestionError):
        provider.draft_sql("what is the airspeed velocity of an unladen swallow", "")


def test_unsupported_question_error_message_includes_the_question():
    provider = ScriptedProvider()
    with pytest.raises(UnsupportedQuestionError, match="unanswerable question"):
        provider.draft_sql("an unanswerable question", "")


def test_correction_context_returns_previous_sql_unchanged():
    provider = ScriptedProvider()
    correction = CorrectionContext(
        previous_sql="SELECT * FROM nope", error_message="table not allowed"
    )
    result = provider.draft_sql("how many customers", "", correction=correction)
    assert result == correction.previous_sql


def test_join_question_actually_joins_customers_and_orders():
    provider = ScriptedProvider()
    sql = provider.draft_sql("order details", "")
    result = validate_query(sql, table_allowlist=TABLE_ALLOWLIST, dialect="duckdb")
    assert result.is_valid
    assert set(result.referenced_tables) == {"customers", "orders"}


def test_average_order_value_question_drafts_an_aggregate():
    provider = ScriptedProvider()
    sql = provider.draft_sql("average order value", "")
    assert "AVG(" in sql.upper()
    result = validate_query(sql, table_allowlist=TABLE_ALLOWLIST, dialect="duckdb")
    assert result.is_valid, result.errors


def test_recent_orders_question_drafts_a_relative_date_time_filter():
    provider = ScriptedProvider()
    sql = provider.draft_sql("orders in the last 7 days", "")
    assert "CURRENT_DATE" in sql.upper()
    assert "INTERVAL" in sql.upper()
    result = validate_query(sql, table_allowlist=TABLE_ALLOWLIST, dialect="duckdb")
    assert result.is_valid, result.errors


def test_running_total_question_drafts_a_window_function():
    provider = ScriptedProvider()
    sql = provider.draft_sql("running total of revenue", "")
    assert "OVER" in sql.upper()
    result = validate_query(sql, table_allowlist=TABLE_ALLOWLIST, dialect="duckdb")
    assert result.is_valid, result.errors


def test_events_per_customer_question_joins_events_and_customers():
    provider = ScriptedProvider()
    sql = provider.draft_sql("events per customer", "")
    result = validate_query(sql, table_allowlist=TABLE_ALLOWLIST, dialect="duckdb")
    assert result.is_valid, result.errors
    assert set(result.referenced_tables) == {"customers", "events"}


@pytest.mark.parametrize("question", AMBIGUOUS_QUESTIONS)
def test_ambiguous_question_raises_clarification_needed_instead_of_guessing(question: str):
    """PROGRESS.md Phase 5: these are recognizable but under-specified (no
    stated metric, time window, or table), so draft_sql must refuse to
    guess one interpretation rather than silently drafting SQL."""
    provider = ScriptedProvider()
    with pytest.raises(ClarificationNeededError) as excinfo:
        provider.draft_sql(question, "")

    assert excinfo.value.question == question
    assert excinfo.value.reason
    assert excinfo.value.options


def test_clarification_needed_error_is_not_an_unsupported_question_error():
    """The two error types mean different things (PROGRESS.md: "needs_clarification"
    is distinct from "no rule matched at all") and callers branch on which one
    was raised, so one must not be a subclass of the other."""
    assert not issubclass(ClarificationNeededError, UnsupportedQuestionError)
    assert not issubclass(UnsupportedQuestionError, ClarificationNeededError)


@pytest.mark.parametrize("question", AMBIGUOUS_QUESTIONS)
def test_ambiguous_question_options_are_concrete_rephrasings_that_resolve(question: str):
    """Each ambiguous rule's `options` are documented as rephrasings that
    *do* match a normal rule and draft real, valid SQL -- so a caller can
    hand them back to the user as working suggestions, not more guesses."""
    provider = ScriptedProvider()
    with pytest.raises(ClarificationNeededError) as excinfo:
        provider.draft_sql(question, "")

    for option in excinfo.value.options:
        sql = ScriptedProvider().draft_sql(option, "")
        result = validate_query(sql, table_allowlist=TABLE_ALLOWLIST, dialect="duckdb")
        assert result.is_valid, (option, result.errors)


@pytest.mark.parametrize("question", AMBIGUOUS_QUESTIONS)
def test_correction_bypasses_ambiguous_check_and_returns_previous_sql(question: str):
    """draft_sql checks `correction` before the ambiguous-phrase rules, so a
    retry attempt against a question that *looks* ambiguous on a fresh
    draft must still just resend the previous SQL, not raise again."""
    provider = ScriptedProvider()
    correction = CorrectionContext(previous_sql="SELECT 1", error_message="irrelevant")
    result = provider.draft_sql(question, "", correction=correction)
    assert result == "SELECT 1"
