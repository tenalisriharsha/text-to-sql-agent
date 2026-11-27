"""A fixed benchmark of natural-language questions against the sample warehouse.

Each :class:`BenchmarkQuestion` carries, independent of how
`ScriptedProvider` happens to be implemented, what the *correct* outcome
for that question is:

- ``"success"`` questions carry their own ``expected_sql`` -- a plainly
  correct query for the question, authored here rather than borrowed from
  `text_to_sql_agent.llm.scripted`, so scoring checks the agent's answer
  against an independent ground truth rather than against itself.
- ``"clarification"`` questions are recognizable but genuinely
  under-specified (no stated metric, time window, or table); the correct
  behavior is to ask rather than guess.
- ``"unanswerable"`` questions ask for something the warehouse has no data
  for at all (no products/employees table, no precomputed churn metric);
  the correct behavior is to say so rather than draft something unsafe or
  fabricated.

The question set spans the difficulty curve `ScriptedProvider` supports --
counts, aggregates, group-bys, joins, a top-N ranking, a relative-date time
filter, and a window function -- each via two or three independent
phrasings, plus the ambiguous and unanswerable categories above.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ExpectedOutcome = Literal["success", "clarification", "unanswerable"]


@dataclass(frozen=True)
class BenchmarkQuestion:
    """One benchmark item: a question and what the correct response to it is."""

    id: str
    question: str
    category: str
    difficulty: int
    expected_outcome: ExpectedOutcome
    expected_sql: str | None = None

    def __post_init__(self) -> None:
        if self.expected_outcome == "success" and not self.expected_sql:
            raise ValueError(f"{self.id}: 'success' questions need expected_sql")
        if self.expected_outcome != "success" and self.expected_sql:
            raise ValueError(f"{self.id}: only 'success' questions should carry expected_sql")
        if not 1 <= self.difficulty <= 5:
            raise ValueError(f"{self.id}: difficulty must be 1-5, got {self.difficulty}")


_COUNT_CUSTOMERS = "SELECT COUNT(*) AS n FROM customers"
_COUNT_ORDERS = "SELECT COUNT(*) AS n FROM orders"
_TOTAL_REVENUE = "SELECT SUM(total_amount) AS n FROM orders WHERE status = 'completed'"
_AVERAGE_ORDER_VALUE = "SELECT AVG(total_amount) AS n FROM orders WHERE status = 'completed'"
_CUSTOMERS_BY_COUNTRY = "SELECT country, COUNT(*) AS n FROM customers GROUP BY country"
_ORDERS_BY_STATUS = "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"
_EVENTS_BY_TYPE = "SELECT event_type, COUNT(*) AS n FROM events GROUP BY event_type"
_ORDER_DETAILS = (
    "SELECT o.order_id, c.name, o.order_date, o.total_amount FROM orders o "
    "JOIN customers c ON o.customer_id = c.customer_id"
)
_EVENTS_PER_CUSTOMER = (
    "SELECT c.customer_id, c.name, COUNT(e.event_id) AS n FROM customers c "
    "LEFT JOIN events e ON c.customer_id = e.customer_id "
    "GROUP BY c.customer_id, c.name"
)
_TOP_CUSTOMERS_BY_SPEND = (
    "SELECT customer_id, SUM(total_amount) AS n FROM orders WHERE status = 'completed' "
    "GROUP BY customer_id ORDER BY n DESC LIMIT 10"
)
_RECENT_ORDERS = (
    "SELECT order_id, customer_id, order_date, total_amount FROM orders "
    "WHERE order_date >= CURRENT_DATE - INTERVAL 7 DAY"
)
_RUNNING_REVENUE_TOTAL = (
    "SELECT order_date, SUM(total_amount) OVER (ORDER BY order_date) AS running_total "
    "FROM orders WHERE status = 'completed'"
)

BENCHMARK: tuple[BenchmarkQuestion, ...] = (
    # -- count (difficulty 1) --------------------------------------------
    BenchmarkQuestion("count-01", "how many customers do we have", "count", 1, "success", _COUNT_CUSTOMERS),
    BenchmarkQuestion(
        "count-02", "how many customers are there in total", "count", 1, "success", _COUNT_CUSTOMERS
    ),
    BenchmarkQuestion(
        "count-03", "How Many Customers signed up?", "count", 1, "success", _COUNT_CUSTOMERS
    ),
    BenchmarkQuestion(
        "count-04", "how many orders have been placed", "count", 1, "success", _COUNT_ORDERS
    ),
    BenchmarkQuestion(
        "count-05", "how many orders are in the system", "count", 1, "success", _COUNT_ORDERS
    ),
    # -- aggregate (difficulty 2) -----------------------------------------
    BenchmarkQuestion(
        "aggregate-01", "what is the total revenue", "aggregate", 2, "success", _TOTAL_REVENUE
    ),
    BenchmarkQuestion(
        "aggregate-02", "what's our total sales this year", "aggregate", 2, "success", _TOTAL_REVENUE
    ),
    BenchmarkQuestion(
        "aggregate-03", "what is the total amount collected", "aggregate", 2, "success", _TOTAL_REVENUE
    ),
    BenchmarkQuestion(
        "aggregate-04",
        "what is the average order value",
        "aggregate",
        2,
        "success",
        _AVERAGE_ORDER_VALUE,
    ),
    BenchmarkQuestion(
        "aggregate-05",
        "what's the average order amount across all orders",
        "aggregate",
        2,
        "success",
        _AVERAGE_ORDER_VALUE,
    ),
    # -- group-by (difficulty 2) -------------------------------------------
    BenchmarkQuestion(
        "groupby-01", "show customers by country", "groupby", 2, "success", _CUSTOMERS_BY_COUNTRY
    ),
    BenchmarkQuestion(
        "groupby-02", "break down customers per country", "groupby", 2, "success", _CUSTOMERS_BY_COUNTRY
    ),
    BenchmarkQuestion("groupby-03", "show orders by status", "groupby", 2, "success", _ORDERS_BY_STATUS),
    BenchmarkQuestion("groupby-04", "give me orders per status", "groupby", 2, "success", _ORDERS_BY_STATUS),
    BenchmarkQuestion("groupby-05", "show events by type", "groupby", 2, "success", _EVENTS_BY_TYPE),
    BenchmarkQuestion(
        "groupby-06", "break down events per type", "groupby", 2, "success", _EVENTS_BY_TYPE
    ),
    # -- join (difficulty 3) -------------------------------------------------
    BenchmarkQuestion(
        "join-01", "show me orders with customer details", "join", 3, "success", _ORDER_DETAILS
    ),
    BenchmarkQuestion(
        "join-02",
        "I need orders and customer names for reporting",
        "join",
        3,
        "success",
        _ORDER_DETAILS,
    ),
    BenchmarkQuestion(
        "join-03", "give me order details for every order", "join", 3, "success", _ORDER_DETAILS
    ),
    BenchmarkQuestion(
        "join-04", "show events per customer", "join", 3, "success", _EVENTS_PER_CUSTOMER
    ),
    BenchmarkQuestion(
        "join-05",
        "what is the event count by customer",
        "join",
        3,
        "success",
        _EVENTS_PER_CUSTOMER,
    ),
    BenchmarkQuestion(
        "join-06", "break down events by customer", "join", 3, "success", _EVENTS_PER_CUSTOMER
    ),
    # -- top-N (difficulty 3) -------------------------------------------------
    BenchmarkQuestion(
        "topn-01", "who are our top customers", "top_n", 3, "success", _TOP_CUSTOMERS_BY_SPEND
    ),
    BenchmarkQuestion(
        "topn-02", "who are the biggest spenders", "top_n", 3, "success", _TOP_CUSTOMERS_BY_SPEND
    ),
    BenchmarkQuestion(
        "topn-03",
        "list the highest spending customers",
        "top_n",
        3,
        "success",
        _TOP_CUSTOMERS_BY_SPEND,
    ),
    # -- time filter (difficulty 4) --------------------------------------------
    BenchmarkQuestion(
        "timefilter-01", "show orders in the last 7 days", "time_filter", 4, "success", _RECENT_ORDERS
    ),
    BenchmarkQuestion(
        "timefilter-02",
        "can you show orders from the last week",
        "time_filter",
        4,
        "success",
        _RECENT_ORDERS,
    ),
    BenchmarkQuestion(
        "timefilter-03", "show me recent orders", "time_filter", 4, "success", _RECENT_ORDERS
    ),
    # -- window function (difficulty 4) ----------------------------------------
    BenchmarkQuestion(
        "window-01",
        "show the running total of revenue",
        "window",
        4,
        "success",
        _RUNNING_REVENUE_TOTAL,
    ),
    BenchmarkQuestion(
        "window-02",
        "what is the cumulative revenue over time",
        "window",
        4,
        "success",
        _RUNNING_REVENUE_TOTAL,
    ),
    BenchmarkQuestion(
        "window-03", "give me the running total", "window", 4, "success", _RUNNING_REVENUE_TOTAL
    ),
    # -- ambiguous: recognized, but no single correct query (difficulty 2-4) --
    BenchmarkQuestion("ambiguous-01", "who are our top performers", "ambiguous", 3, "clarification"),
    BenchmarkQuestion("ambiguous-02", "show me recent activity", "ambiguous", 3, "clarification"),
    BenchmarkQuestion(
        "ambiguous-03", "how is the business doing", "ambiguous", 2, "clarification"
    ),
    BenchmarkQuestion(
        "ambiguous-04", "what are last month's numbers", "ambiguous", 4, "clarification"
    ),
    BenchmarkQuestion(
        "ambiguous-05", "give me a customer activity summary", "ambiguous", 3, "clarification"
    ),
    BenchmarkQuestion("ambiguous-06", "how much have we grown", "ambiguous", 3, "clarification"),
    # -- unanswerable: no rule, no data for it (difficulty 1-5) ----------------
    BenchmarkQuestion(
        "unanswerable-01", "what's the weather like today", "unanswerable", 1, "unanswerable"
    ),
    BenchmarkQuestion(
        "unanswerable-02", "what is our refund policy", "unanswerable", 1, "unanswerable"
    ),
    BenchmarkQuestion("unanswerable-03", "list all employees", "unanswerable", 2, "unanswerable"),
    BenchmarkQuestion(
        "unanswerable-04", "which products are low in stock", "unanswerable", 3, "unanswerable"
    ),
    BenchmarkQuestion(
        "unanswerable-05", "what is our most popular product", "unanswerable", 3, "unanswerable"
    ),
    BenchmarkQuestion(
        "unanswerable-06", "what is our customer churn rate", "unanswerable", 4, "unanswerable"
    ),
    BenchmarkQuestion(
        "unanswerable-07", "forecast next quarter's revenue", "unanswerable", 5, "unanswerable"
    ),
)
