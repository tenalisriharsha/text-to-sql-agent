"""Register a generated sample warehouse as tables in a DuckDB connection."""

from __future__ import annotations

from pathlib import Path

import duckdb

from text_to_sql_agent.schema_catalog import SchemaCatalog
from text_to_sql_agent.warehouse.generate import (
    COLUMN_DESCRIPTIONS,
    PARTITION_COLUMNS,
    TABLE_DESCRIPTIONS,
    WarehouseInfo,
    warehouse_info_for,
)


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


def build_catalog(warehouse_dir: Path | str) -> SchemaCatalog:
    """Load the warehouse at ``warehouse_dir`` and build the catalog from it.

    A short-lived DuckDB connection is used just for introspection and
    closed before returning; every command (`inspect-schema`, `retrieve`,
    `validate`, `estimate-cost`, `ask`, `benchmark`) that needs the catalog
    without the connection itself goes through this one path.
    """
    connection = duckdb.connect()
    try:
        load_into_duckdb(connection, warehouse_info_for(Path(warehouse_dir)))
        return SchemaCatalog.from_duckdb(
            connection,
            table_descriptions=TABLE_DESCRIPTIONS,
            column_descriptions=COLUMN_DESCRIPTIONS,
            partition_columns=PARTITION_COLUMNS,
        )
    finally:
        connection.close()
