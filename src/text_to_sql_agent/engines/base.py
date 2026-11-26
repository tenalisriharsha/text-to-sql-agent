"""The connection interface the agent loop and CLI execute against.

``run_agent_loop`` and ``cli.ask`` only ever call two things on whatever
"connection" they are handed: ``connection.execute(sql)``, and
``.description``/``.fetchall()`` on the object that returns -- including
the literal string ``EXPLAIN <query>`` to get a query plan before a
candidate is approved. DuckDB's native connection already satisfies this
without any wrapping, which is why it has been passed around directly
since Phase 1. The Athena and BigQuery adapters in this package exist
because their drivers don't: pyathena's cursor needs translating into
this shape, and BigQuery has no ``EXPLAIN`` statement at all (its closest
equivalent is a dry-run job), so its adapter intercepts that literal
prefix instead of sending it to the warehouse.

``Connection``/``Cursor`` below are documentation, not enforcement --
nothing in this codebase calls ``isinstance`` against them.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence


class Cursor(Protocol):
    description: Sequence[Sequence[Any]] | None

    def fetchall(self) -> Sequence[Sequence[Any]]: ...


class Connection(Protocol):
    def execute(self, sql: str) -> Cursor: ...


class EngineExecutionError(Exception):
    """A query failed against a non-demo engine (Athena or BigQuery).

    Raised in place of the driver's own exception type so
    ``run_agent_loop`` can catch one thing regardless of which optional
    driver is installed, exactly as it already catches ``duckdb.Error``
    for the bundled demo source.
    """
