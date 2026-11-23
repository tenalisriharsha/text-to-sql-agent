"""Tests for registering the sample warehouse as DuckDB views, including partition pruning."""

from __future__ import annotations

import duckdb


def test_customers_and_orders_views_expose_all_rows(warehouse_connection: duckdb.DuckDBPyConnection):
    (customer_count,) = warehouse_connection.execute("select count(*) from customers").fetchone()
    (order_count,) = warehouse_connection.execute("select count(*) from orders").fetchone()
    assert customer_count == 200
    assert order_count == 1000


def test_events_view_exposes_hive_partition_column(
    warehouse_connection: duckdb.DuckDBPyConnection,
):
    columns = [
        row[0]
        for row in warehouse_connection.execute(
            "select column_name from information_schema.columns"
            " where table_name = 'events' order by ordinal_position"
        ).fetchall()
    ]
    assert "event_date" in columns
    assert "event_id" in columns


def test_events_view_can_filter_by_partition_column(
    warehouse_connection: duckdb.DuckDBPyConnection,
):
    rows = warehouse_connection.execute(
        "select distinct event_date from events order by event_date limit 1"
    ).fetchall()
    assert rows
    one_day = rows[0][0]

    (count_for_day,) = warehouse_connection.execute(
        "select count(*) from events where event_date = ?", [one_day]
    ).fetchone()
    (total_count,) = warehouse_connection.execute("select count(*) from events").fetchone()

    assert count_for_day > 0
    assert count_for_day < total_count


def test_load_into_duckdb_handles_paths_containing_single_quotes(tmp_path):
    from text_to_sql_agent.warehouse.generate import generate_warehouse
    from text_to_sql_agent.warehouse.load import load_into_duckdb

    tricky_dir = tmp_path / "it's a warehouse"
    info = generate_warehouse(tricky_dir, seed=7, num_customers=5, num_orders=5, num_days=1)

    connection = duckdb.connect()
    try:
        load_into_duckdb(connection, info)
        (count,) = connection.execute("select count(*) from customers").fetchone()
        assert count == 5
    finally:
        connection.close()
