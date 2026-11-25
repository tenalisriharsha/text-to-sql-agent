"""Tests for the partition- and column-pruning-aware scan-cost estimate.

These exercise `estimate_bytes_scanned` directly against the real sample
warehouse on disk (no mocking of pyarrow or sqlglot), covering both the
documented prunings (partition pruning on `events.event_date`, column
pruning via each Parquet file's own row-group metadata) and the documented
fail-safe fallbacks (`*`, an ambiguous unqualified column in a join, an
`OR`-combined partition predicate, and a dynamic predicate that can't be
evaluated statically).
"""

from __future__ import annotations

import datetime

import pyarrow.parquet as pq
import pytest

from text_to_sql_agent.cost_estimate import TableLocation, estimate_bytes_scanned
from text_to_sql_agent.warehouse.generate import PARTITION_COLUMNS, WarehouseInfo


@pytest.fixture()
def table_locations(warehouse_info: WarehouseInfo) -> dict[str, TableLocation]:
    return {
        "customers": TableLocation(path=warehouse_info.customers_path),
        "orders": TableLocation(path=warehouse_info.orders_path),
        "events": TableLocation(
            path=warehouse_info.events_root, partition_column=PARTITION_COLUMNS["events"][0]
        ),
    }


def _all_events_files(warehouse_info: WarehouseInfo) -> list:
    return sorted(warehouse_info.events_root.rglob("*.parquet"))


def _column_bytes(path, columns: set[str]) -> int:
    """An independent re-computation of per-column compressed size, for cross-checks."""
    metadata = pq.ParquetFile(path).metadata
    sizes = dict.fromkeys(columns, 0)
    for row_group_index in range(metadata.num_row_groups):
        row_group = metadata.row_group(row_group_index)
        for column_index in range(row_group.num_columns):
            column = row_group.column(column_index)
            if column.path_in_schema in sizes:
                sizes[column.path_in_schema] += column.total_compressed_size
    return sum(sizes.values())


def test_select_star_on_unpartitioned_table_charges_full_file_size(table_locations, warehouse_info):
    scan = estimate_bytes_scanned(
        "SELECT * FROM customers", "duckdb", table_locations
    )

    expected = warehouse_info.customers_path.stat().st_size
    assert scan.total_bytes == expected
    assert scan.per_table["customers"].bytes_scanned == expected
    assert scan.per_table["customers"].all_columns is True
    assert scan.per_table["customers"].partitions_scanned is None


def test_select_star_on_events_scans_every_partition_at_full_width(table_locations, warehouse_info):
    scan = estimate_bytes_scanned("SELECT * FROM events", "duckdb", table_locations)

    files = _all_events_files(warehouse_info)
    expected = sum(path.stat().st_size for path in files)
    assert scan.total_bytes == expected
    estimate = scan.per_table["events"]
    assert estimate.all_columns is True
    assert len(estimate.partitions_scanned) == 30


def test_equality_filter_on_partition_column_prunes_to_one_partition(table_locations, warehouse_info):
    today = datetime.date.today().isoformat()
    scan = estimate_bytes_scanned(
        f"SELECT * FROM events WHERE event_date = '{today}'", "duckdb", table_locations
    )

    estimate = scan.per_table["events"]
    assert estimate.partitions_scanned == (f"event_date={today}",)

    matching_file = warehouse_info.events_root / f"event_date={today}" / "part-0.parquet"
    assert estimate.bytes_scanned == matching_file.stat().st_size


def test_in_predicate_on_partition_column_prunes_to_matching_partitions(table_locations, warehouse_info):
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    scan = estimate_bytes_scanned(
        "SELECT * FROM events WHERE event_date IN "
        f"('{today.isoformat()}', '{yesterday.isoformat()}')",
        "duckdb",
        table_locations,
    )

    estimate = scan.per_table["events"]
    assert len(estimate.partitions_scanned) == 2
    assert f"event_date={today.isoformat()}" in estimate.partitions_scanned
    assert f"event_date={yesterday.isoformat()}" in estimate.partitions_scanned


def test_range_predicate_on_partition_column_prunes_to_the_bounded_range(table_locations, warehouse_info):
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=6)
    scan = estimate_bytes_scanned(
        f"SELECT * FROM events WHERE event_date >= '{cutoff.isoformat()}'",
        "duckdb",
        table_locations,
    )

    estimate = scan.per_table["events"]
    # day offsets 0..6 inclusive -> 7 partitions out of 30.
    assert len(estimate.partitions_scanned) == 7


def test_or_combined_partition_predicate_falls_back_to_scanning_every_partition(
    table_locations, warehouse_info
):
    today = datetime.date.today().isoformat()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    scan = estimate_bytes_scanned(
        f"SELECT * FROM events WHERE event_date = '{today}' OR event_date = '{yesterday}'",
        "duckdb",
        table_locations,
    )

    estimate = scan.per_table["events"]
    assert len(estimate.partitions_scanned) == 30


def test_dynamic_predicate_on_partition_column_falls_back_to_scanning_every_partition(
    table_locations,
):
    scan = estimate_bytes_scanned(
        "SELECT * FROM events WHERE event_date >= CURRENT_DATE - INTERVAL 7 DAY",
        "duckdb",
        table_locations,
    )

    estimate = scan.per_table["events"]
    assert len(estimate.partitions_scanned) == 30


def test_narrow_select_list_prunes_columns_and_matches_independent_metadata_sum(
    table_locations, warehouse_info
):
    scan = estimate_bytes_scanned(
        "SELECT o.order_id, o.total_amount FROM orders o", "duckdb", table_locations
    )

    estimate = scan.per_table["orders"]
    assert estimate.all_columns is False
    assert set(estimate.columns_scanned) == {"order_id", "total_amount"}

    expected = _column_bytes(warehouse_info.orders_path, {"order_id", "total_amount"})
    assert estimate.bytes_scanned == expected
    assert estimate.bytes_scanned < warehouse_info.orders_path.stat().st_size


def test_column_pruning_also_considers_where_and_group_by_and_order_by(
    table_locations, warehouse_info
):
    # COUNT(order_id), not COUNT(*): a bare `*` is documented to always fall
    # back to all-columns, which would defeat the point of this test.
    scan = estimate_bytes_scanned(
        "SELECT status, COUNT(order_id) FROM orders WHERE total_amount > 100 "
        "GROUP BY status ORDER BY status",
        "duckdb",
        table_locations,
    )

    estimate = scan.per_table["orders"]
    assert estimate.all_columns is False
    assert set(estimate.columns_scanned) == {"status", "total_amount", "order_id"}


def test_two_table_join_with_qualified_columns_prunes_each_table_independently(
    table_locations, warehouse_info
):
    scan = estimate_bytes_scanned(
        "SELECT o.order_id, c.name FROM orders o JOIN customers c "
        "ON o.customer_id = c.customer_id",
        "duckdb",
        table_locations,
    )

    orders_estimate = scan.per_table["orders"]
    customers_estimate = scan.per_table["customers"]
    assert orders_estimate.all_columns is False
    assert customers_estimate.all_columns is False
    # the join predicate references customer_id on both sides too.
    assert set(orders_estimate.columns_scanned) == {"order_id", "customer_id"}
    assert set(customers_estimate.columns_scanned) == {"name", "customer_id"}
    assert orders_estimate.bytes_scanned < warehouse_info.orders_path.stat().st_size
    assert customers_estimate.bytes_scanned < warehouse_info.customers_path.stat().st_size


def test_ambiguous_unqualified_column_in_a_join_falls_back_to_all_columns_both_tables(
    table_locations, warehouse_info
):
    scan = estimate_bytes_scanned(
        "SELECT customer_id FROM orders o JOIN customers c "
        "ON o.customer_id = c.customer_id",
        "duckdb",
        table_locations,
    )

    assert scan.per_table["orders"].all_columns is True
    assert scan.per_table["customers"].all_columns is True
    assert scan.total_bytes == (
        warehouse_info.orders_path.stat().st_size + warehouse_info.customers_path.stat().st_size
    )


def test_count_star_falls_back_to_all_columns(table_locations, warehouse_info):
    scan = estimate_bytes_scanned("SELECT COUNT(*) FROM orders", "duckdb", table_locations)

    estimate = scan.per_table["orders"]
    assert estimate.all_columns is True
    assert estimate.bytes_scanned == warehouse_info.orders_path.stat().st_size


def test_referencing_the_partition_column_itself_falls_back_to_full_file_size(
    table_locations, warehouse_info
):
    today = datetime.date.today().isoformat()
    scan = estimate_bytes_scanned(
        f"SELECT event_date, event_type FROM events WHERE event_date = '{today}'",
        "duckdb",
        table_locations,
    )

    estimate = scan.per_table["events"]
    # partition pruning still applies...
    assert estimate.partitions_scanned == (f"event_date={today}",)
    # ...but event_date isn't a physical column, so column pruning bails out
    # to the whole file for that (single) matched partition.
    matching_file = warehouse_info.events_root / f"event_date={today}" / "part-0.parquet"
    assert estimate.bytes_scanned == matching_file.stat().st_size


def test_table_absent_from_table_locations_is_skipped_rather_than_erroring(warehouse_info):
    partial_locations = {"customers": TableLocation(path=warehouse_info.customers_path)}
    scan = estimate_bytes_scanned(
        "SELECT o.order_id FROM orders o JOIN customers c ON o.customer_id = c.customer_id",
        "duckdb",
        partial_locations,
    )

    assert "orders" not in scan.per_table
    assert "customers" in scan.per_table
    assert scan.total_bytes == scan.per_table["customers"].bytes_scanned


def test_multiple_tables_referenced_sum_into_total_bytes(table_locations, warehouse_info):
    scan = estimate_bytes_scanned(
        "SELECT o.order_id, c.name FROM orders o JOIN customers c "
        "ON o.customer_id = c.customer_id",
        "duckdb",
        table_locations,
    )

    assert scan.total_bytes == (
        scan.per_table["orders"].bytes_scanned + scan.per_table["customers"].bytes_scanned
    )
