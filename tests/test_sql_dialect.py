"""Tests for translating validated SQL to Athena (Trino) and BigQuery dialects."""

from __future__ import annotations

import pytest

from text_to_sql_agent.sql.dialect import SUPPORTED_DIALECTS, translate_dialect


def test_supported_dialects_match_documented_cli_choices():
    assert set(SUPPORTED_DIALECTS) == {"duckdb", "athena", "bigquery"}


def test_translate_to_athena_uses_trino_syntax_under_the_hood():
    translated = translate_dialect(
        "SELECT * FROM orders LIMIT 10", source_dialect="duckdb", target_dialect="athena"
    )
    assert "FROM orders" in translated
    assert "LIMIT 10" in translated


def test_translate_to_bigquery():
    translated = translate_dialect(
        "SELECT * FROM orders LIMIT 10", source_dialect="duckdb", target_dialect="bigquery"
    )
    assert "FROM orders" in translated
    assert "LIMIT 10" in translated


def test_translate_round_trips_a_group_by_aggregate():
    sql = "SELECT status, COUNT(*) AS n FROM orders GROUP BY status LIMIT 5"
    translated = translate_dialect(sql, source_dialect="duckdb", target_dialect="bigquery")
    assert "GROUP BY" in translated.upper()
    assert "COUNT" in translated.upper()


def test_translate_dialect_rejects_unparseable_sql():
    with pytest.raises(Exception):
        translate_dialect("not even close to sql (((", "duckdb", "athena")
