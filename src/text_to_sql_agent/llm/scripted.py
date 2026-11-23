"""A deterministic, rule-based provider for tests, CI, and offline demos.

It matches keywords in the question against a fixed set of rules and
returns the corresponding canned SQL. There is no model and no network
call involved, so behavior never varies between runs, which is exactly
what an automated test suite needs. It is not a general text-to-SQL
engine: questions it doesn't recognize raise ``UnsupportedQuestionError``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from text_to_sql_agent.llm.base import CorrectionContext, LLMProvider


class UnsupportedQuestionError(Exception):
    """Raised when no scripted rule matches the question."""


@dataclass(frozen=True)
class _Rule:
    name: str
    keywords: tuple[str, ...]
    sql: str


_RULES: tuple[_Rule, ...] = (
    _Rule(
        name="customer_count",
        keywords=("how many customers",),
        sql="SELECT COUNT(*) AS customer_count FROM customers",
    ),
    _Rule(
        name="order_count",
        keywords=("how many orders",),
        sql="SELECT COUNT(*) AS order_count FROM orders",
    ),
    _Rule(
        name="total_revenue",
        keywords=("total revenue", "total sales", "total amount"),
        sql=(
            "SELECT SUM(total_amount) AS total_revenue FROM orders "
            "WHERE status = 'completed'"
        ),
    ),
    _Rule(
        name="customers_by_country",
        keywords=("customers by country", "customers per country"),
        sql=(
            "SELECT country, COUNT(*) AS customer_count FROM customers "
            "GROUP BY country ORDER BY customer_count DESC"
        ),
    ),
    _Rule(
        name="orders_by_status",
        keywords=("orders by status", "orders per status"),
        sql=(
            "SELECT status, COUNT(*) AS order_count FROM orders "
            "GROUP BY status ORDER BY order_count DESC"
        ),
    ),
    _Rule(
        name="top_customers_by_spend",
        keywords=("top customers", "biggest spenders", "highest spending"),
        sql=(
            "SELECT customer_id, SUM(total_amount) AS total_spent FROM orders "
            "WHERE status = 'completed' "
            "GROUP BY customer_id ORDER BY total_spent DESC LIMIT 10"
        ),
    ),
    _Rule(
        name="events_by_type",
        keywords=("events by type", "events per type"),
        sql=(
            "SELECT event_type, COUNT(*) AS event_count FROM events "
            "GROUP BY event_type ORDER BY event_count DESC"
        ),
    ),
    _Rule(
        name="customer_orders_join",
        keywords=("orders with customer", "orders and customer names", "order details"),
        sql=(
            "SELECT o.order_id, c.name, o.order_date, o.total_amount FROM orders o "
            "JOIN customers c ON o.customer_id = c.customer_id "
            "ORDER BY o.order_date DESC"
        ),
    ),
)


class ScriptedProvider(LLMProvider):
    """Keyword-matched canned SQL; no model, no network, fully deterministic."""

    def draft_sql(
        self,
        question: str,
        schema_context: str,
        dialect: str = "duckdb",
        correction: CorrectionContext | None = None,
    ) -> str:
        if correction is not None:
            return correction.previous_sql

        normalized = _normalize(question)
        for rule in _RULES:
            if any(keyword in normalized for keyword in rule.keywords):
                return rule.sql

        raise UnsupportedQuestionError(
            f"no scripted rule matches question: {question!r}"
        )


def _normalize(question: str) -> str:
    return re.sub(r"\s+", " ", question.strip().lower())
