"""BigQuery execution adapter: a thin wrapper over ``google-cloud-bigquery``.

BigQuery has no ``EXPLAIN`` statement. Its closest equivalent is a
dry-run query job: BigQuery validates and prices the query without
scanning any data or returning rows. ``run_agent_loop`` and the CLI don't
know that, though -- they just call ``connection.execute("EXPLAIN " +
sql).fetchall()`` the same way they do against DuckDB and Athena. So this
adapter intercepts that literal prefix and runs a dry-run job instead;
every other statement goes to BigQuery unmodified.

``google-cloud-bigquery`` is an optional dependency (the ``bigquery``
extra); importing it is deferred to ``BigQueryConnection.__init__``.
"""

from __future__ import annotations

from dataclasses import dataclass

from text_to_sql_agent.engines.base import EngineExecutionError

_INSTALL_HINT = (
    "the 'bigquery' extra is required to execute against BigQuery: "
    "install with `pip install text-to-sql-agent[bigquery]`"
)

EXPLAIN_PREFIX = "EXPLAIN "


@dataclass(frozen=True)
class BigQueryConfig:
    """Connection parameters for ``google.cloud.bigquery.Client``.

    ``project`` is the GCP project to bill and run jobs in; ``location``
    pins the job to a specific region (BigQuery requires this when a
    dataset's location isn't the default) and may be left unset.
    """

    project: str
    location: str | None = None


class _BigQueryCursor:
    """A synthetic or real result set in the ``Cursor`` shape run_agent_loop expects."""

    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self.description = [(name,) for name in columns]
        self._rows = rows

    def fetchall(self):
        return self._rows


class BigQueryConnection:
    """Executes validated SQL against BigQuery.

    Raises :class:`EngineExecutionError` -- never a raw google-cloud
    exception -- both when the optional dependency is missing and when a
    query or dry run fails.
    """

    def __init__(self, config: BigQueryConfig) -> None:
        try:
            from google.cloud import bigquery
        except ImportError as exc:
            raise EngineExecutionError(_INSTALL_HINT) from exc

        self._bigquery = bigquery
        self._config = config
        try:
            self._client = bigquery.Client(project=config.project)
        except Exception as exc:  # pragma: no cover - credential failures
            raise EngineExecutionError(f"could not connect to BigQuery: {exc}") from exc

    def execute(self, sql: str) -> _BigQueryCursor:
        if sql.startswith(EXPLAIN_PREFIX):
            return self._dry_run(sql[len(EXPLAIN_PREFIX) :])

        try:
            job = self._client.query(sql, location=self._config.location)
            result = job.result()
        except Exception as exc:  # pragma: no cover - google-cloud's own error hierarchy
            raise EngineExecutionError(f"BigQuery query failed: {exc}") from exc

        columns = [field.name for field in result.schema]
        rows = [tuple(row.values()) for row in result]
        return _BigQueryCursor(columns, rows)

    def _dry_run(self, sql: str) -> _BigQueryCursor:
        job_config = self._bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        try:
            job = self._client.query(sql, job_config=job_config, location=self._config.location)
        except Exception as exc:  # pragma: no cover - google-cloud's own error hierarchy
            raise EngineExecutionError(f"BigQuery dry run failed: {exc}") from exc

        plan = f"dry run: would scan {job.total_bytes_processed} bytes (no rows returned)"
        return _BigQueryCursor(["plan"], [(plan,)])

    def close(self) -> None:
        self._client.close()
