"""Tests for the sample warehouse generator: shape, content, and determinism."""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq

from text_to_sql_agent.warehouse.generate import (
    EVENT_TYPES,
    ORDER_STATUSES,
    generate_warehouse,
)


def test_generate_warehouse_writes_customers_orders_and_partitioned_events(tmp_path: Path):
    info = generate_warehouse(tmp_path, seed=1, num_customers=10, num_orders=20, num_days=3)

    assert info.customers_path == tmp_path / "customers.parquet"
    assert info.orders_path == tmp_path / "orders.parquet"
    assert info.events_root == tmp_path / "events"

    assert info.customers_path.is_file()
    assert info.orders_path.is_file()
    partition_dirs = sorted(p.name for p in info.events_root.iterdir())
    assert len(partition_dirs) == 3
    assert all(name.startswith("event_date=") for name in partition_dirs)


def test_customers_table_has_requested_row_count_and_columns(tmp_path: Path):
    info = generate_warehouse(tmp_path, seed=1, num_customers=15)
    table = pq.read_table(info.customers_path)

    assert table.num_rows == 15
    assert set(table.column_names) == {
        "customer_id",
        "name",
        "email",
        "country",
        "signup_date",
    }
    assert sorted(table.column("customer_id").to_pylist()) == list(range(1, 16))


def test_orders_reference_only_known_customers_and_valid_statuses(tmp_path: Path):
    info = generate_warehouse(tmp_path, seed=2, num_customers=10, num_orders=50)
    orders = pq.read_table(info.orders_path).to_pylist()

    assert len(orders) == 50
    customer_ids = set(range(1, 11))
    for order in orders:
        assert order["customer_id"] in customer_ids
        assert order["status"] in ORDER_STATUSES
        assert order["total_amount"] >= 5.0
        assert order["total_amount"] <= 500.0


def test_order_dates_are_never_before_the_customers_signup_date(tmp_path: Path):
    info = generate_warehouse(tmp_path, seed=3, num_customers=10, num_orders=100)
    customers = {
        row["customer_id"]: row["signup_date"]
        for row in pq.read_table(info.customers_path).to_pylist()
    }
    orders = pq.read_table(info.orders_path).to_pylist()

    for order in orders:
        assert order["order_date"] >= customers[order["customer_id"]]


def test_events_are_partitioned_one_file_per_day_with_requested_row_count(tmp_path: Path):
    info = generate_warehouse(
        tmp_path, seed=4, num_customers=5, num_days=4, events_per_day=7
    )
    partition_dirs = list(info.events_root.iterdir())
    assert len(partition_dirs) == 4

    total_rows = 0
    all_event_ids = []
    for partition_dir in partition_dirs:
        files = list(partition_dir.glob("*.parquet"))
        assert len(files) == 1
        table = pq.read_table(files[0])
        assert table.num_rows == 7
        total_rows += table.num_rows
        all_event_ids.extend(table.column("event_id").to_pylist())

    assert total_rows == 4 * 7
    # event ids are a single increasing sequence across every partition.
    assert sorted(all_event_ids) == list(range(1, total_rows + 1))


def test_event_types_are_drawn_from_the_documented_set(tmp_path: Path):
    info = generate_warehouse(
        tmp_path, seed=5, num_customers=5, num_days=2, events_per_day=30
    )
    for partition_dir in info.events_root.iterdir():
        table = pq.read_table(next(partition_dir.glob("*.parquet")))
        assert set(table.column("event_type").to_pylist()) <= set(EVENT_TYPES)


def test_generate_warehouse_is_deterministic_for_a_fixed_seed(tmp_path: Path):
    first = generate_warehouse(tmp_path / "a", seed=99, num_customers=8, num_orders=12)
    second = generate_warehouse(tmp_path / "b", seed=99, num_customers=8, num_orders=12)

    assert pq.read_table(first.customers_path).to_pylist() == pq.read_table(
        second.customers_path
    ).to_pylist()
    assert pq.read_table(first.orders_path).to_pylist() == pq.read_table(
        second.orders_path
    ).to_pylist()


def test_generate_warehouse_different_seeds_produce_different_customers(tmp_path: Path):
    first = generate_warehouse(tmp_path / "a", seed=1, num_customers=8)
    second = generate_warehouse(tmp_path / "b", seed=2, num_customers=8)

    assert pq.read_table(first.customers_path).to_pylist() != pq.read_table(
        second.customers_path
    ).to_pylist()
