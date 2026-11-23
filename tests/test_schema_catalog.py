"""Tests for ColumnSchema/TableSchema/SchemaCatalog, including live DuckDB introspection."""

from __future__ import annotations

import duckdb
import pytest

from text_to_sql_agent.schema_catalog import ColumnSchema, SchemaCatalog, TableSchema


def test_column_schema_to_text_includes_description_and_samples():
    column = ColumnSchema(
        name="status",
        data_type="VARCHAR",
        description="Order lifecycle state.",
        sample_values=("pending", "completed"),
    )
    text = column.to_text()
    assert "status" in text
    assert "VARCHAR" in text
    assert "Order lifecycle state." in text
    assert "pending" in text and "completed" in text


def test_column_schema_to_text_omits_absent_fields():
    column = ColumnSchema(name="id", data_type="BIGINT")
    text = column.to_text()
    assert text == "id | BIGINT"


def test_table_schema_column_names_and_get_column():
    columns = (
        ColumnSchema(name="a", data_type="INT"),
        ColumnSchema(name="b", data_type="TEXT"),
    )
    table = TableSchema(name="t", columns=columns)
    assert table.column_names == ("a", "b")
    assert table.get_column("b") is columns[1]
    assert table.get_column("missing") is None


def test_table_schema_to_text_includes_header_and_columns():
    table = TableSchema(
        name="orders",
        description="One row per order.",
        columns=(ColumnSchema(name="order_id", data_type="BIGINT"),),
    )
    text = table.to_text()
    assert text.startswith("orders: One row per order.")
    assert "order_id" in text


def test_schema_catalog_table_names_and_get():
    orders = TableSchema(name="orders")
    catalog = SchemaCatalog(tables={"orders": orders})
    assert catalog.table_names == ("orders",)
    assert catalog.get("orders") is orders
    assert catalog.get("missing") is None


def test_schema_catalog_to_text_joins_all_tables():
    catalog = SchemaCatalog(
        tables={
            "a": TableSchema(name="a", columns=(ColumnSchema(name="x", data_type="INT"),)),
            "b": TableSchema(name="b", columns=(ColumnSchema(name="y", data_type="INT"),)),
        }
    )
    text = catalog.to_text()
    assert "a" in text and "b" in text
    assert "\n\n" in text


def test_from_duckdb_introspects_tables_columns_types_and_descriptions():
    connection = duckdb.connect()
    connection.execute("create table widgets(id bigint, name varchar)")
    connection.execute("insert into widgets values (1, 'bolt'), (2, 'nut'), (3, null)")

    catalog = SchemaCatalog.from_duckdb(
        connection,
        table_descriptions={"widgets": "Things we sell."},
        column_descriptions={"widgets": {"name": "Widget name."}},
    )

    assert catalog.table_names == ("widgets",)
    table = catalog.get("widgets")
    assert table is not None
    assert table.description == "Things we sell."
    assert table.column_names == ("id", "name")

    name_column = table.get_column("name")
    assert name_column is not None
    assert name_column.description == "Widget name."
    assert table.get_column("id").data_type == "BIGINT"
    assert table.get_column("name").data_type == "VARCHAR"


def test_from_duckdb_excludes_null_sample_values():
    connection = duckdb.connect()
    connection.execute("create table t(v varchar)")
    connection.execute("insert into t values ('x'), (null), ('y')")

    catalog = SchemaCatalog.from_duckdb(connection)
    samples = catalog.get("t").get_column("v").sample_values
    assert None not in samples
    assert set(samples) <= {"x", "y"}
    assert len(samples) == 2


def test_from_duckdb_respects_sample_value_limit():
    connection = duckdb.connect()
    connection.execute("create table t(v integer)")
    connection.executemany("insert into t values (?)", [(i,) for i in range(20)])

    catalog = SchemaCatalog.from_duckdb(connection, sample_value_limit=3)
    assert len(catalog.get("t").get_column("v").sample_values) == 3


def test_from_duckdb_sample_value_limit_zero_yields_no_samples():
    connection = duckdb.connect()
    connection.execute("create table t(v integer)")
    connection.execute("insert into t values (1), (2)")

    catalog = SchemaCatalog.from_duckdb(connection, sample_value_limit=0)
    assert catalog.get("t").get_column("v").sample_values == ()


def test_from_duckdb_attaches_partition_columns():
    connection = duckdb.connect()
    connection.execute("create table events(event_id integer)")

    catalog = SchemaCatalog.from_duckdb(
        connection, partition_columns={"events": ("event_date",)}
    )
    assert catalog.get("events").partition_columns == ("event_date",)


def test_sample_warehouse_catalog_has_expected_tables(catalog: SchemaCatalog):
    assert set(catalog.table_names) == {"customers", "orders", "events"}
    events = catalog.get("events")
    assert "event_date" in events.column_names
    assert events.partition_columns == ("event_date",)


def test_sample_warehouse_catalog_columns_have_descriptions(catalog: SchemaCatalog):
    orders = catalog.get("orders")
    status_column = orders.get_column("status")
    assert status_column is not None
    assert status_column.description != ""
    assert len(status_column.sample_values) > 0
