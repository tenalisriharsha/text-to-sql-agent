"""A deterministic, rule-based provider for tests, CI, and offline demos.

It matches keywords in the question against a fixed set of rules and
returns the corresponding canned SQL. There is no model and no network
call involved, so behavior never varies between runs, which is exactly
what an automated test suite needs. It is not a general text-to-SQL
engine: questions it doesn't recognize raise ``UnsupportedQuestionError``,
and questions that are recognized but have no single correct SQL
interpretation raise ``ClarificationNeededError`` instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from text_to_sql_agent.llm.base import CorrectionContext, LLMProvider


class UnsupportedQuestionError(Exception):
    """Raised when no scripted rule matches the question."""


class ClarificationNeededError(Exception):
    """Raised when a question is recognized but has no single correct query.

    A question like "top performers" doesn't say what makes a performer
    "top" (revenue? order count?) or even which table to look in
    (customers' orders? raw events?) -- picking one interpretation
    silently would answer a different question than the one asked. This
    carries the ambiguity itself (``reason``) and the concrete rephrasings
    that would resolve it (``options``), so a caller can surface them
    instead of guessing.
    """

    def __init__(self, question: str, reason: str, options: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.question = question
        self.reason = reason
        self.options = options


@dataclass(frozen=True)
class _Rule:
    name: str
    keywords: tuple[str, ...]
    sql: str


@dataclass(frozen=True)
class _AmbiguousRule:
    name: str
    keywords: tuple[str, ...]
    reason: str
    options: tuple[str, ...]


# Checked before `_RULES`: a phrase here is recognizable but under-specified
# enough that two people could mean different queries by it. Each entry's
# `options` are concrete rephrasings that *do* match a `_RULES` keyword, so
# a caller can offer them back to the user verbatim instead of guessing.
_AMBIGUOUS_RULES: tuple[_AmbiguousRule, ...] = (
    _AmbiguousRule(
        name="top_performers",
        keywords=("top performers", "best performers"),
        reason=(
            "'performer' isn't a metric -- it could mean highest revenue, "
            "most orders, or most events, and over customers or orders."
        ),
        options=("top customers by spend", "orders by status", "events by type"),
    ),
    _AmbiguousRule(
        name="recent_activity",
        keywords=("recent activity", "latest activity"),
        reason=(
            "'activity' spans two different tables here (orders and "
            "events) with no shared definition, and 'recent' has no "
            "stated window."
        ),
        options=("orders from the last week", "events by type"),
    ),
    _AmbiguousRule(
        name="business_health",
        keywords=("how is the business doing", "how's the business doing", "business doing well"),
        reason="no metric or time window is named -- revenue, order volume, and signups would each tell a different story.",
        options=("total revenue", "how many orders", "customers by country"),
    ),
    _AmbiguousRule(
        name="last_month_numbers",
        keywords=("last month's numbers", "last month numbers"),
        reason=(
            "'last month' could mean the trailing 30 days or the previous "
            "calendar month, and 'numbers' doesn't say which metric."
        ),
        options=("orders from the last week", "total revenue"),
    ),
    _AmbiguousRule(
        name="customer_activity_summary",
        keywords=("customer activity summary", "activity summary for customers"),
        reason="'activity' could mean orders placed, events logged, or both, with no stated metric for either.",
        options=("events per customer", "order details"),
    ),
    _AmbiguousRule(
        name="growth",
        keywords=("our growth", "how much have we grown", "growth this quarter"),
        reason="growth of what (customers, revenue, orders) and over what baseline period isn't stated.",
        options=("customers by country", "total revenue", "how many orders"),
    ),
)


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
    _Rule(
        name="average_order_value",
        keywords=("average order value", "average order amount"),
        sql=(
            "SELECT AVG(total_amount) AS average_order_value FROM orders "
            "WHERE status = 'completed'"
        ),
    ),
    _Rule(
        name="recent_orders",
        keywords=("orders in the last 7 days", "orders from the last week", "recent orders"),
        sql=(
            "SELECT order_id, customer_id, order_date, total_amount FROM orders "
            "WHERE order_date >= CURRENT_DATE - INTERVAL 7 DAY "
            "ORDER BY order_date DESC"
        ),
    ),
    _Rule(
        name="running_revenue_total",
        keywords=("running total of revenue", "cumulative revenue", "running total"),
        sql=(
            "SELECT order_date, "
            "SUM(total_amount) OVER (ORDER BY order_date) AS running_total "
            "FROM orders WHERE status = 'completed' ORDER BY order_date"
        ),
    ),
    _Rule(
        name="events_per_customer",
        keywords=("events per customer", "event count by customer", "events by customer"),
        sql=(
            "SELECT c.customer_id, c.name, COUNT(e.event_id) AS event_count FROM customers c "
            "LEFT JOIN events e ON c.customer_id = e.customer_id "
            "GROUP BY c.customer_id, c.name ORDER BY event_count DESC"
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

        for ambiguous in _AMBIGUOUS_RULES:
            if any(keyword in normalized for keyword in ambiguous.keywords):
                raise ClarificationNeededError(question, ambiguous.reason, ambiguous.options)

        for rule in _RULES:
            if any(keyword in normalized for keyword in rule.keywords):
                return rule.sql

        raise UnsupportedQuestionError(
            f"no scripted rule matches question: {question!r}"
        )


def _normalize(question: str) -> str:
    return re.sub(r"\s+", " ", question.strip().lower())
