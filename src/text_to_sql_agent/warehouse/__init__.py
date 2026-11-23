"""Sample warehouse generation and loading for local, offline demos."""

from text_to_sql_agent.warehouse.generate import (
    TABLE_DESCRIPTIONS,
    WarehouseInfo,
    generate_warehouse,
)
from text_to_sql_agent.warehouse.load import load_into_duckdb

__all__ = [
    "TABLE_DESCRIPTIONS",
    "WarehouseInfo",
    "generate_warehouse",
    "load_into_duckdb",
]
