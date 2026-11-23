"""Tests for the ScriptedProvider: the deterministic, no-network LLM stand-in.

This provider is the project's documented way to exercise the full
draft/validate pipeline without an API key, so every rule the README/CLI
help implies must exist (counts, totals, group-bys, a join, a top-N query)
needs to actually produce valid SQL for the sample warehouse.
"""

from __future__ import annotations

import pytest

from text_to_sql_agent.llm.base import CorrectionContext, LLMProvider
from text_to_sql_agent.llm.scripted import ScriptedProvider, UnsupportedQuestionError
from text_to_sql_agent.sql.validator import validate_query

TABLE_ALLOWLIST = ("customers", "orders", "events")


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
