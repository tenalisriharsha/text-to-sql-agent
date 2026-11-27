"""Generate a small, deterministic sample warehouse of Parquet files.

The warehouse models a toy e-commerce business: ``customers`` who place
``orders``, and a separate ``events`` click-stream, hive-partitioned by day
the way a real lake table would be, so partition pruning has something real
to prune.
"""

from __future__ import annotations

import datetime
import random
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from faker import Faker

from text_to_sql_agent.cost_estimate import TableLocation

ORDER_STATUSES = ("pending", "completed", "cancelled", "refunded")
EVENT_TYPES = ("page_view", "add_to_cart", "purchase", "signup", "login")
COUNTRIES = ("US", "GB", "DE", "IN", "BR", "CA", "AU", "FR")

TABLE_DESCRIPTIONS: dict[str, str] = {
    "customers": "One row per registered customer.",
    "orders": "One row per order placed by a customer.",
    "events": "Click-stream events, one row per user action, partitioned by day.",
}

COLUMN_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "customers": {
        "customer_id": "Surrogate key for the customer.",
        "name": "Customer's full name.",
        "email": "Customer's contact email.",
        "country": "ISO-ish country code where the customer signed up.",
        "signup_date": "Date the customer created their account.",
    },
    "orders": {
        "order_id": "Surrogate key for the order.",
        "customer_id": "Foreign key to customers.customer_id.",
        "order_date": "Date the order was placed.",
        "status": "Order lifecycle state: pending, completed, cancelled, or refunded.",
        "total_amount": "Order total in US dollars.",
    },
    "events": {
        "event_id": "Surrogate key for the event.",
        "customer_id": "Foreign key to customers.customer_id.",
        "event_type": "Kind of action: page_view, add_to_cart, purchase, signup, or login.",
        "event_timestamp": "Exact time the event occurred.",
        "event_date": "Calendar date of the event; this is the partition column.",
    },
}

PARTITION_COLUMNS: dict[str, tuple[str, ...]] = {
    "events": ("event_date",),
}


@dataclass(frozen=True)
class WarehouseInfo:
    """Where the generated warehouse landed on disk."""

    root: Path
    customers_path: Path
    orders_path: Path
    events_root: Path


def warehouse_info_for(output_dir: Path) -> WarehouseInfo:
    """The :class:`WarehouseInfo` for a warehouse already written to ``output_dir``.

    Pure path construction -- it doesn't generate or read anything -- so any
    caller that already has a warehouse on disk (the CLI, the evaluation
    harness) can rebuild the same layout `generate_warehouse` returns
    without regenerating it.
    """
    output_dir = Path(output_dir)
    return WarehouseInfo(
        root=output_dir,
        customers_path=output_dir / "customers.parquet",
        orders_path=output_dir / "orders.parquet",
        events_root=output_dir / "events",
    )


def table_locations_for(output_dir: Path) -> dict[str, TableLocation]:
    """Where each table's Parquet data lives, for `cost_estimate.estimate_bytes_scanned`."""
    info = warehouse_info_for(output_dir)
    return {
        "customers": TableLocation(path=info.customers_path),
        "orders": TableLocation(path=info.orders_path),
        "events": TableLocation(
            path=info.events_root, partition_column=PARTITION_COLUMNS["events"][0]
        ),
    }


def generate_warehouse(
    output_dir: Path,
    seed: int = 42,
    num_customers: int = 200,
    num_orders: int = 1000,
    num_days: int = 30,
    events_per_day: int = 150,
) -> WarehouseInfo:
    """Write customers.parquet, orders.parquet, and a partitioned events/ tree.

    Deterministic for a given ``seed``: re-running with the same arguments
    regenerates byte-identical data (modulo Parquet write metadata).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    faker = Faker()
    Faker.seed(seed)

    customers_path = output_dir / "customers.parquet"
    orders_path = output_dir / "orders.parquet"
    events_root = output_dir / "events"

    customer_ids = list(range(1, num_customers + 1))
    signup_dates = [
        faker.date_between(start_date="-2y", end_date="-30d") for _ in customer_ids
    ]
    _write_customers(customers_path, customer_ids, signup_dates, faker, rng)
    _write_orders(orders_path, customer_ids, signup_dates, num_orders, rng, faker)
    _write_events(events_root, customer_ids, num_days, events_per_day, rng, faker)

    return WarehouseInfo(
        root=output_dir,
        customers_path=customers_path,
        orders_path=orders_path,
        events_root=events_root,
    )


def _write_customers(
    path: Path,
    customer_ids: list[int],
    signup_dates: list[datetime.date],
    faker: Faker,
    rng: random.Random,
) -> None:
    table = pa.table(
        {
            "customer_id": pa.array(customer_ids, type=pa.int64()),
            "name": pa.array([faker.name() for _ in customer_ids], type=pa.string()),
            "email": pa.array([faker.email() for _ in customer_ids], type=pa.string()),
            "country": pa.array(
                [rng.choice(COUNTRIES) for _ in customer_ids], type=pa.string()
            ),
            "signup_date": pa.array(signup_dates, type=pa.date32()),
        }
    )
    pq.write_table(table, path)


def _write_orders(
    path: Path,
    customer_ids: list[int],
    signup_dates: list[datetime.date],
    num_orders: int,
    rng: random.Random,
    faker: Faker,
) -> None:
    signup_by_customer = dict(zip(customer_ids, signup_dates))
    order_ids = list(range(1, num_orders + 1))
    chosen_customers = [rng.choice(customer_ids) for _ in order_ids]
    order_dates = []
    for customer_id in chosen_customers:
        earliest = signup_by_customer[customer_id]
        order_dates.append(faker.date_between(start_date=earliest, end_date="today"))

    table = pa.table(
        {
            "order_id": pa.array(order_ids, type=pa.int64()),
            "customer_id": pa.array(chosen_customers, type=pa.int64()),
            "order_date": pa.array(order_dates, type=pa.date32()),
            "status": pa.array(
                [rng.choice(ORDER_STATUSES) for _ in order_ids], type=pa.string()
            ),
            "total_amount": pa.array(
                [round(rng.uniform(5.0, 500.0), 2) for _ in order_ids],
                type=pa.float64(),
            ),
        }
    )
    pq.write_table(table, path)


def _write_events(
    events_root: Path,
    customer_ids: list[int],
    num_days: int,
    events_per_day: int,
    rng: random.Random,
    faker: Faker,
) -> None:
    events_root.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today()
    event_id = 1
    for day_offset in range(num_days):
        event_date = today - datetime.timedelta(days=day_offset)
        partition_dir = events_root / f"event_date={event_date.isoformat()}"
        partition_dir.mkdir(parents=True, exist_ok=True)

        event_ids, cust_ids, event_types, timestamps = [], [], [], []
        for _ in range(events_per_day):
            event_ids.append(event_id)
            cust_ids.append(rng.choice(customer_ids))
            event_types.append(rng.choice(EVENT_TYPES))
            timestamps.append(
                datetime.datetime.combine(event_date, datetime.time())
                + datetime.timedelta(seconds=rng.randint(0, 86399))
            )
            event_id += 1

        table = pa.table(
            {
                "event_id": pa.array(event_ids, type=pa.int64()),
                "customer_id": pa.array(cust_ids, type=pa.int64()),
                "event_type": pa.array(event_types, type=pa.string()),
                "event_timestamp": pa.array(timestamps, type=pa.timestamp("us")),
            }
        )
        pq.write_table(table, partition_dir / "part-0.parquet")
