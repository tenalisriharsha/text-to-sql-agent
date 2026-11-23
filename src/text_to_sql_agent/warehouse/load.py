"""Register a generated sample warehouse as tables in a DuckDB connection."""

from __future__ import annotations

import duckdb

from text_to_sql_agent.warehouse.generate import WarehouseInfo


def load_into_duckdb(
    connection: duckdb.DuckDBPyConnection, warehouse: WarehouseInfo
) -> None:
    """Create ``customers``, ``orders``, and ``events`` views over the Parquet files.

    ``events`` is read with hive partitioning enabled so DuckDB can prune
    partitions on ``event_date`` filters, the same way Athena or BigQuery
    would prune a partitioned lake table.
    """
    connection.execute(
        f"create or replace view customers as "
        f"select * from read_parquet('{_escape(warehouse.customers_path)}')"
    )
    connection.execute(
        f"create or replace view orders as "
        f"select * from read_parquet('{_escape(warehouse.orders_path)}')"
    )
    events_glob = _escape(warehouse.events_root / "*" / "*.parquet")
    connection.execute(
        f"create or replace view events as "
        f"select * from read_parquet('{events_glob}', hive_partitioning = true)"
    )


def _escape(path: object) -> str:
    return str(path).replace("'", "''")
