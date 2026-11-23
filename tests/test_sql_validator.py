"""Tests for the safety contract documented in README's "Safety contract" section:

exactly one statement, SELECT-only, table allowlist, and an enforced LIMIT.
"""

from __future__ import annotations

import pytest

from text_to_sql_agent.sql.validator import validate_query

ALLOWLIST = ("orders", "customers", "events")


def test_valid_select_passes_and_normalizes():
    result = validate_query(
        "SELECT * FROM orders", table_allowlist=ALLOWLIST, default_row_limit=100
    )
    assert result.is_valid
    assert result.errors == ()
    assert "LIMIT 100" in result.normalized_sql
    assert result.referenced_tables == ("orders",)


def test_union_of_selects_is_allowed():
    result = validate_query(
        "SELECT order_id FROM orders UNION SELECT order_id FROM orders",
        table_allowlist=ALLOWLIST,
        default_row_limit=10,
    )
    assert result.is_valid


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO orders VALUES (1, 1, '2024-01-01', 'pending', 9.99)",
        "UPDATE orders SET status = 'cancelled'",
        "DELETE FROM orders",
        "DROP TABLE orders",
        "CREATE TABLE evil AS SELECT * FROM orders",
        "ALTER TABLE orders ADD COLUMN x INT",
    ],
)
def test_non_select_statements_are_rejected(sql: str):
    result = validate_query(sql, table_allowlist=ALLOWLIST, default_row_limit=10)
    assert not result.is_valid
    assert result.normalized_sql is None


def test_empty_sql_is_rejected():
    result = validate_query("", table_allowlist=ALLOWLIST, default_row_limit=10)
    assert not result.is_valid
    assert "no SQL statement found" in result.errors[0]


def test_whitespace_only_sql_is_rejected():
    result = validate_query("   ", table_allowlist=ALLOWLIST, default_row_limit=10)
    assert not result.is_valid


def test_unparseable_sql_is_rejected_not_raised():
    result = validate_query(
        "SELECT FROM WHERE *** garbage", table_allowlist=ALLOWLIST, default_row_limit=10
    )
    assert not result.is_valid
    assert result.errors


def test_multiple_statements_are_rejected():
    result = validate_query(
        "SELECT 1; SELECT 2;", table_allowlist=ALLOWLIST, default_row_limit=10
    )
    assert not result.is_valid
    assert "exactly one SQL statement" in result.errors[0]


def test_single_statement_with_trailing_semicolon_is_allowed():
    result = validate_query("SELECT 1;", table_allowlist=ALLOWLIST, default_row_limit=10)
    assert result.is_valid


def test_table_outside_allowlist_is_rejected():
    result = validate_query(
        "SELECT * FROM secret_table", table_allowlist=ALLOWLIST, default_row_limit=10
    )
    assert not result.is_valid
    assert "secret_table" in result.errors[0]


def test_join_requires_every_table_on_the_allowlist():
    result = validate_query(
        "SELECT * FROM orders JOIN secret_table ON orders.order_id = secret_table.id",
        table_allowlist=ALLOWLIST,
        default_row_limit=10,
    )
    assert not result.is_valid
    assert "secret_table" in result.errors[0]
    assert "orders" not in result.errors[0]


def test_table_allowlist_matching_is_case_insensitive():
    result = validate_query(
        "SELECT * FROM ORDERS", table_allowlist=("orders",), default_row_limit=10
    )
    assert result.is_valid


def test_missing_limit_is_injected():
    result = validate_query(
        "SELECT * FROM orders", table_allowlist=ALLOWLIST, default_row_limit=50
    )
    assert result.is_valid
    assert result.normalized_sql.rstrip().endswith("LIMIT 50")


def test_limit_smaller_than_cap_is_preserved():
    result = validate_query(
        "SELECT * FROM orders LIMIT 5", table_allowlist=ALLOWLIST, default_row_limit=50
    )
    assert result.is_valid
    assert "LIMIT 5" in result.normalized_sql
    assert "LIMIT 50" not in result.normalized_sql


def test_limit_larger_than_cap_is_clamped():
    result = validate_query(
        "SELECT * FROM orders LIMIT 999999", table_allowlist=ALLOWLIST, default_row_limit=50
    )
    assert result.is_valid
    assert result.normalized_sql.rstrip().endswith("LIMIT 50")


def test_limit_equal_to_cap_is_preserved():
    result = validate_query(
        "SELECT * FROM orders LIMIT 50", table_allowlist=ALLOWLIST, default_row_limit=50
    )
    assert result.is_valid
    assert result.normalized_sql.rstrip().endswith("LIMIT 50")


def test_referenced_tables_are_reported_even_when_valid():
    result = validate_query(
        "SELECT o.order_id, c.name FROM orders o JOIN customers c"
        " ON o.customer_id = c.customer_id",
        table_allowlist=ALLOWLIST,
        default_row_limit=10,
    )
    assert result.is_valid
    assert set(result.referenced_tables) == {"orders", "customers"}


def test_validation_result_failure_helper_sets_is_valid_false():
    from text_to_sql_agent.sql.validator import ValidationResult

    result = ValidationResult.failure("one error", "another error")
    assert result.is_valid is False
    assert result.errors == ("one error", "another error")
    assert result.normalized_sql is None
    assert result.referenced_tables == ()
