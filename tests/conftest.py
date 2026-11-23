"""Shared fixtures: a sample warehouse and the catalog built from it.

The warehouse is expensive-ish to generate (faker + parquet writes), so it
is built once per test session into a session-scoped tmp directory and
reused read-only by every test that needs real data on disk.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from text_to_sql_agent.schema_catalog import SchemaCatalog
from text_to_sql_agent.warehouse.generate import (
    COLUMN_DESCRIPTIONS,
    PARTITION_COLUMNS,
    TABLE_DESCRIPTIONS,
    WarehouseInfo,
    generate_warehouse,
)
from text_to_sql_agent.warehouse.load import load_into_duckdb


@pytest.fixture(scope="session")
def warehouse_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output_dir = tmp_path_factory.mktemp("warehouse")
    generate_warehouse(output_dir, seed=42)
    return output_dir


@pytest.fixture(scope="session")
def warehouse_info(warehouse_dir: Path) -> WarehouseInfo:
    return WarehouseInfo(
        root=warehouse_dir,
        customers_path=warehouse_dir / "customers.parquet",
        orders_path=warehouse_dir / "orders.parquet",
        events_root=warehouse_dir / "events",
    )


@pytest.fixture()
def warehouse_connection(warehouse_info: WarehouseInfo) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    load_into_duckdb(connection, warehouse_info)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="session")
def catalog(warehouse_info: WarehouseInfo) -> SchemaCatalog:
    connection = duckdb.connect()
    try:
        load_into_duckdb(connection, warehouse_info)
        return SchemaCatalog.from_duckdb(
            connection,
            table_descriptions=TABLE_DESCRIPTIONS,
            column_descriptions=COLUMN_DESCRIPTIONS,
            partition_columns=PARTITION_COLUMNS,
        )
    finally:
        connection.close()
