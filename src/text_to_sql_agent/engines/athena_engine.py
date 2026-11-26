"""Athena execution adapter: a thin wrapper over ``pyathena``.

Athena speaks Trino SQL and Trino supports a real ``EXPLAIN <query>``
statement, so unlike BigQuery this adapter needs no special-casing for
EXPLAIN -- it passes every statement straight to pyathena and only
translates the resulting cursor and any driver exception into the shapes
``engines.base.Connection``/``EngineExecutionError`` describe.

``pyathena`` is an optional dependency (the ``athena`` extra) so that the
default install -- which requires no API key or cloud account -- stays
dependency-light; importing it is deferred to ``AthenaConnection.__init__``
so the rest of the package works without it installed.
"""

from __future__ import annotations

from dataclasses import dataclass

from text_to_sql_agent.engines.base import EngineExecutionError

_INSTALL_HINT = (
    "the 'athena' extra is required to execute against Athena: "
    "install with `pip install text-to-sql-agent[athena]`"
)


@dataclass(frozen=True)
class AthenaConfig:
    """Connection parameters for ``pyathena.connect``.

    ``s3_staging_dir`` and ``region_name`` are required by Athena itself
    (query results are always staged to S3); ``schema_name`` is the
    Glue/Athena database to run against, and ``work_group`` selects a
    workgroup for cost/quota controls if your account uses one.
    """

    s3_staging_dir: str
    region_name: str
    schema_name: str = "default"
    work_group: str | None = None


class _AthenaCursor:
    """Adapts a pyathena cursor to the ``Cursor`` shape run_agent_loop expects."""

    def __init__(self, raw_cursor) -> None:
        self._cursor = raw_cursor

    @property
    def description(self):
        return self._cursor.description

    def fetchall(self):
        return self._cursor.fetchall()


class AthenaConnection:
    """Executes validated SQL against Athena.

    Raises :class:`EngineExecutionError` -- never a raw pyathena
    exception -- both when the optional dependency is missing and when a
    query fails, so callers only need to catch one exception type
    regardless of which engine they're talking to.
    """

    def __init__(self, config: AthenaConfig) -> None:
        try:
            import pyathena
        except ImportError as exc:
            raise EngineExecutionError(_INSTALL_HINT) from exc

        try:
            self._connection = pyathena.connect(
                s3_staging_dir=config.s3_staging_dir,
                region_name=config.region_name,
                schema_name=config.schema_name,
                work_group=config.work_group,
            )
        except Exception as exc:  # pragma: no cover - network/credential failures
            raise EngineExecutionError(f"could not connect to Athena: {exc}") from exc

    def execute(self, sql: str) -> _AthenaCursor:
        try:
            cursor = self._connection.cursor()
            cursor.execute(sql)
        except Exception as exc:  # pragma: no cover - pyathena's own error hierarchy
            raise EngineExecutionError(f"Athena query failed: {exc}") from exc
        return _AthenaCursor(cursor)

    def close(self) -> None:
        self._connection.close()
