"""Execution adapters: the same ``connection.execute``/``EXPLAIN`` interface
DuckDB's native connection provides, implemented for Athena and BigQuery.

DuckDB needs no adapter here -- its own connection already satisfies
``engines.base.Connection`` and is used directly, as it has been since
Phase 1.
"""

from text_to_sql_agent.engines.athena_engine import AthenaConfig, AthenaConnection
from text_to_sql_agent.engines.base import Connection, Cursor, EngineExecutionError
from text_to_sql_agent.engines.bigquery_engine import BigQueryConfig, BigQueryConnection

__all__ = [
    "AthenaConfig",
    "AthenaConnection",
    "BigQueryConfig",
    "BigQueryConnection",
    "Connection",
    "Cursor",
    "EngineExecutionError",
]
