"""Tests for the Phase 4 execution adapters (PROGRESS.md, README's
"Execution engines" section): ``engines/base.py``, ``engines/athena_engine.py``,
``engines/bigquery_engine.py``, and the CLI wiring in ``cli.py`` that selects
between them via ``ask --dialect``.

``pyathena`` and ``google-cloud-bigquery`` are optional extras that are not
installed in this environment (confirmed: importing either raises
``ModuleNotFoundError``), and neither is installed by CI (``.github/workflows/ci.yml``
only installs ``.[dev]``). Two kinds of tests follow from that:

- The "missing optional dependency" path is exercised directly, with no
  mocking at all, since it is simply what happens in this real environment.
- Everything downstream of a successful connection (cursor adaptation, error
  wrapping, the EXPLAIN/dry-run interception) is exercised by injecting a
  fake ``pyathena``/``google.cloud.bigquery`` module into ``sys.modules`` --
  mocking the external cloud driver, never a module of this project's own,
  consistent with "mocks are allowed for things outside this project
  (network, cloud, LLM)".

The CLI-level tests at the bottom mock only the two cloud ``Connection``
classes (`AthenaConnection`/`BigQueryConnection`) so the documented
mandatory-approval and scan-cost-guard-skip behavior for non-``duckdb``
dialects can be exercised without a real account or network access.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from text_to_sql_agent.audit import AuditTrace
from text_to_sql_agent.cli import main
from text_to_sql_agent.engines.athena_engine import AthenaConfig, AthenaConnection
from text_to_sql_agent.engines.base import EngineExecutionError
from text_to_sql_agent.engines.bigquery_engine import BigQueryConfig, BigQueryConnection

try:
    import pyathena  # noqa: F401

    _HAS_PYATHENA = True
except ImportError:
    _HAS_PYATHENA = False

try:
    from google.cloud import bigquery  # noqa: F401

    _HAS_BIGQUERY = True
except ImportError:
    _HAS_BIGQUERY = False


def test_engine_execution_error_is_a_plain_exception():
    assert issubclass(EngineExecutionError, Exception)
    assert str(EngineExecutionError("boom")) == "boom"


# ---------------------------------------------------------------------------
# Athena
# ---------------------------------------------------------------------------


class _FakeAthenaCursor:
    def __init__(self, description=None, rows=None, raise_exc=None):
        self.description = description or [("n",)]
        self._rows = rows or []
        self.executed_sql = None
        self._raise_exc = raise_exc

    def execute(self, sql):
        self.executed_sql = sql
        if self._raise_exc is not None:
            raise self._raise_exc

    def fetchall(self):
        return self._rows


class _FakeAthenaRawConnection:
    def __init__(self, cursor_factory):
        self._cursor_factory = cursor_factory
        self.closed = False

    def cursor(self):
        return self._cursor_factory()

    def close(self):
        self.closed = True


def _make_fake_pyathena(connect_fn):
    module = types.ModuleType("pyathena")
    module.connect = connect_fn
    return module


@pytest.mark.skipif(_HAS_PYATHENA, reason="pyathena is installed; this exercises the missing-dependency path")
def test_athena_connection_missing_dependency_raises_install_hint():
    with pytest.raises(EngineExecutionError, match="athena"):
        AthenaConnection(AthenaConfig(s3_staging_dir="s3://bucket/path", region_name="us-east-1"))


def test_athena_connection_missing_dependency_message_names_the_pip_extra():
    """Regardless of whether pyathena happens to be installed elsewhere, the
    adapter's own hint text (used only when the import actually fails) must
    name the installable extra, not just raise a bare ImportError."""
    if _HAS_PYATHENA:
        pytest.skip("pyathena is installed; covered by the skipif'd test above instead")
    with pytest.raises(EngineExecutionError) as exc_info:
        AthenaConnection(AthenaConfig(s3_staging_dir="s3://bucket/path", region_name="us-east-1"))
    assert "pip install" in str(exc_info.value)
    assert "athena" in str(exc_info.value)


def test_athena_connection_forwards_config_to_pyathena_connect(monkeypatch):
    captured = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return _FakeAthenaRawConnection(lambda: _FakeAthenaCursor())

    monkeypatch.setitem(sys.modules, "pyathena", _make_fake_pyathena(fake_connect))

    AthenaConnection(
        AthenaConfig(
            s3_staging_dir="s3://bucket/path",
            region_name="us-east-1",
            schema_name="analytics",
            work_group="wg1",
        )
    )

    assert captured == {
        "s3_staging_dir": "s3://bucket/path",
        "region_name": "us-east-1",
        "schema_name": "analytics",
        "work_group": "wg1",
    }


def test_athena_connection_wraps_connect_failure(monkeypatch):
    def fake_connect(**kwargs):
        raise RuntimeError("bad credentials")

    monkeypatch.setitem(sys.modules, "pyathena", _make_fake_pyathena(fake_connect))

    with pytest.raises(EngineExecutionError, match="could not connect to Athena"):
        AthenaConnection(AthenaConfig(s3_staging_dir="s3://bucket/path", region_name="us-east-1"))


def test_athena_connection_wraps_query_failure(monkeypatch):
    failing_cursor = _FakeAthenaCursor(raise_exc=RuntimeError("syntax error"))
    raw_connection = _FakeAthenaRawConnection(lambda: failing_cursor)
    monkeypatch.setitem(sys.modules, "pyathena", _make_fake_pyathena(lambda **kwargs: raw_connection))

    connection = AthenaConnection(AthenaConfig(s3_staging_dir="s3://bucket/path", region_name="us-east-1"))

    with pytest.raises(EngineExecutionError, match="Athena query failed"):
        connection.execute("SELECT * FROM nope")


def test_athena_connection_passes_explain_through_unmodified(monkeypatch):
    """Athena/Trino supports a real EXPLAIN statement, so unlike BigQuery
    this adapter must send the literal 'EXPLAIN ...' string straight to
    pyathena with no special-casing (PROGRESS.md Phase 4)."""
    explain_cursor = _FakeAthenaCursor(description=[("plan",)], rows=[("fake plan",)])
    raw_connection = _FakeAthenaRawConnection(lambda: explain_cursor)
    monkeypatch.setitem(sys.modules, "pyathena", _make_fake_pyathena(lambda **kwargs: raw_connection))

    connection = AthenaConnection(AthenaConfig(s3_staging_dir="s3://bucket/path", region_name="us-east-1"))
    cursor = connection.execute("EXPLAIN SELECT 1 FROM customers")

    assert explain_cursor.executed_sql == "EXPLAIN SELECT 1 FROM customers"
    assert cursor.description == [("plan",)]
    assert cursor.fetchall() == [("fake plan",)]


def test_athena_connection_normal_query_returns_adapted_cursor(monkeypatch):
    normal_cursor = _FakeAthenaCursor(description=[("customer_count",)], rows=[(7,)])
    raw_connection = _FakeAthenaRawConnection(lambda: normal_cursor)
    monkeypatch.setitem(sys.modules, "pyathena", _make_fake_pyathena(lambda **kwargs: raw_connection))

    connection = AthenaConnection(AthenaConfig(s3_staging_dir="s3://bucket/path", region_name="us-east-1"))
    cursor = connection.execute("SELECT COUNT(*) AS customer_count FROM customers")

    assert cursor.description == [("customer_count",)]
    assert cursor.fetchall() == [(7,)]


def test_athena_connection_close_delegates_to_driver(monkeypatch):
    raw_connection = _FakeAthenaRawConnection(lambda: _FakeAthenaCursor())
    monkeypatch.setitem(sys.modules, "pyathena", _make_fake_pyathena(lambda **kwargs: raw_connection))

    connection = AthenaConnection(AthenaConfig(s3_staging_dir="s3://bucket/path", region_name="us-east-1"))
    connection.close()

    assert raw_connection.closed is True


# ---------------------------------------------------------------------------
# BigQuery
# ---------------------------------------------------------------------------


class _FakeField:
    def __init__(self, name):
        self.name = name


class _FakeRow:
    def __init__(self, values):
        self._values = values

    def values(self):
        return self._values


class _FakeQueryResult:
    def __init__(self, columns, rows):
        self.schema = [_FakeField(name) for name in columns]
        self._rows = [_FakeRow(row) for row in rows]

    def __iter__(self):
        return iter(self._rows)


class _FakeJob:
    def __init__(self, result=None, total_bytes_processed=None):
        self._result = result
        self.total_bytes_processed = total_bytes_processed

    def result(self):
        return self._result


class _FakeQueryJobConfig:
    def __init__(self, dry_run=False, use_query_cache=True):
        self.dry_run = dry_run
        self.use_query_cache = use_query_cache


def _make_bigquery_module(client_factory):
    module = types.ModuleType("google.cloud.bigquery")
    module.Client = client_factory
    module.QueryJobConfig = _FakeQueryJobConfig
    return module


def _install_fake_bigquery(monkeypatch, bigquery_module):
    google_module = types.ModuleType("google")
    google_cloud_module = types.ModuleType("google.cloud")
    google_cloud_module.bigquery = bigquery_module
    google_module.cloud = google_cloud_module

    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.cloud", google_cloud_module)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bigquery_module)


@pytest.mark.skipif(
    _HAS_BIGQUERY, reason="google-cloud-bigquery is installed; this exercises the missing-dependency path"
)
def test_bigquery_connection_missing_dependency_raises_install_hint():
    with pytest.raises(EngineExecutionError) as exc_info:
        BigQueryConnection(BigQueryConfig(project="my-project"))
    assert "pip install" in str(exc_info.value)
    assert "bigquery" in str(exc_info.value)


def test_bigquery_connection_wraps_connect_failure(monkeypatch):
    def failing_client(project=None):
        raise RuntimeError("bad credentials")

    _install_fake_bigquery(monkeypatch, _make_bigquery_module(failing_client))

    with pytest.raises(EngineExecutionError, match="could not connect to BigQuery"):
        BigQueryConnection(BigQueryConfig(project="my-project"))


def test_bigquery_connection_intercepts_explain_prefix_as_a_dry_run(monkeypatch):
    """BigQuery has no EXPLAIN statement; the adapter must strip the literal
    'EXPLAIN ' prefix the agent loop sends and run a dry-run job instead,
    reporting total_bytes_processed as the plan text (PROGRESS.md Phase 4)."""
    recorded_calls = []

    class _Client:
        def __init__(self, project=None):
            self.project = project

        def query(self, sql, job_config=None, location=None):
            recorded_calls.append((sql, job_config, location))
            return _FakeJob(total_bytes_processed=123456)

    _install_fake_bigquery(monkeypatch, _make_bigquery_module(lambda project=None: _Client(project)))

    connection = BigQueryConnection(BigQueryConfig(project="my-project", location="US"))
    cursor = connection.execute("EXPLAIN SELECT 1 FROM customers")

    assert cursor.description == [("plan",)]
    assert cursor.fetchall() == [("dry run: would scan 123456 bytes (no rows returned)",)]

    sql, job_config, location = recorded_calls[0]
    assert sql == "SELECT 1 FROM customers"  # the EXPLAIN prefix must not reach BigQuery
    assert job_config.dry_run is True
    assert location == "US"


def test_bigquery_connection_wraps_dry_run_failure(monkeypatch):
    class _Client:
        def __init__(self, project=None):
            pass

        def query(self, sql, job_config=None, location=None):
            raise RuntimeError("invalid query")

    _install_fake_bigquery(monkeypatch, _make_bigquery_module(lambda project=None: _Client(project)))

    connection = BigQueryConnection(BigQueryConfig(project="my-project"))
    with pytest.raises(EngineExecutionError, match="BigQuery dry run failed"):
        connection.execute("EXPLAIN SELECT 1")


def test_bigquery_connection_runs_a_normal_query_unmodified(monkeypatch):
    recorded_calls = []

    class _Client:
        def __init__(self, project=None):
            self.project = project

        def query(self, sql, job_config=None, location=None):
            recorded_calls.append((sql, job_config, location))
            return _FakeJob(result=_FakeQueryResult(["customer_count"], [(7,)]))

    _install_fake_bigquery(monkeypatch, _make_bigquery_module(lambda project=None: _Client(project)))

    connection = BigQueryConnection(BigQueryConfig(project="my-project", location="US"))
    cursor = connection.execute("SELECT COUNT(*) AS customer_count FROM customers")

    assert cursor.description == [("customer_count",)]
    assert cursor.fetchall() == [(7,)]

    sql, job_config, location = recorded_calls[0]
    assert sql == "SELECT COUNT(*) AS customer_count FROM customers"
    assert job_config is None
    assert location == "US"


def test_bigquery_connection_wraps_query_failure(monkeypatch):
    class _Client:
        def __init__(self, project=None):
            pass

        def query(self, sql, job_config=None, location=None):
            raise RuntimeError("quota exceeded")

    _install_fake_bigquery(monkeypatch, _make_bigquery_module(lambda project=None: _Client(project)))

    connection = BigQueryConnection(BigQueryConfig(project="my-project"))
    with pytest.raises(EngineExecutionError, match="BigQuery query failed"):
        connection.execute("SELECT 1")


def test_bigquery_connection_close_delegates_to_driver(monkeypatch):
    closed = []

    class _Client:
        def __init__(self, project=None):
            pass

        def close(self):
            closed.append(True)

    _install_fake_bigquery(monkeypatch, _make_bigquery_module(lambda project=None: _Client(project)))

    connection = BigQueryConnection(BigQueryConfig(project="my-project"))
    connection.close()

    assert closed == [True]


# ---------------------------------------------------------------------------
# CLI wiring: `ask --dialect athena|bigquery`
# ---------------------------------------------------------------------------


class _FakeCloudCursor:
    def __init__(self, description, rows):
        self.description = description
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeCloudConnection:
    """Stands in for AthenaConnection/BigQueryConnection in CLI-level tests.

    Only the CLI's own plumbing (approval gating, cost-estimate skip,
    audit-trace recording) is under test here; the adapters' own internals
    are covered directly above.
    """

    def __init__(self, *args, **kwargs):
        self.executed = []

    def execute(self, sql):
        self.executed.append(sql)
        if sql.startswith("EXPLAIN"):
            return _FakeCloudCursor([("plan",)], [("fake plan",)])
        return _FakeCloudCursor([("customer_count",)], [(7,)])


def test_ask_dialect_athena_missing_required_options_is_a_usage_error(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["ask", "how many customers", "--warehouse-dir", str(warehouse_dir), "--dialect", "athena", "--yes"],
    )
    assert result.exit_code != 0
    assert "--athena-s3-staging-dir" in result.output
    assert "--athena-region" in result.output


def test_ask_dialect_bigquery_missing_required_options_is_a_usage_error(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["ask", "how many customers", "--warehouse-dir", str(warehouse_dir), "--dialect", "bigquery", "--yes"],
    )
    assert result.exit_code != 0
    assert "--bigquery-project" in result.output


@pytest.mark.skipif(_HAS_PYATHENA, reason="pyathena is installed; this exercises the missing-driver path")
def test_ask_dialect_athena_without_driver_installed_shows_install_hint_not_traceback(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "ask",
            "how many customers",
            "--warehouse-dir",
            str(warehouse_dir),
            "--dialect",
            "athena",
            "--athena-s3-staging-dir",
            "s3://bucket/path",
            "--athena-region",
            "us-east-1",
            "--yes",
        ],
    )
    assert result.exit_code == 1
    assert "pip install" in result.output
    assert "Traceback" not in result.output


@pytest.mark.skipif(_HAS_BIGQUERY, reason="google-cloud-bigquery is installed; this exercises the missing-driver path")
def test_ask_dialect_bigquery_without_driver_installed_shows_install_hint_not_traceback(warehouse_dir: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "ask",
            "how many customers",
            "--warehouse-dir",
            str(warehouse_dir),
            "--dialect",
            "bigquery",
            "--bigquery-project",
            "my-project",
            "--yes",
        ],
    )
    assert result.exit_code == 1
    assert "pip install" in result.output
    assert "Traceback" not in result.output


def test_ask_dialect_athena_ignores_yes_and_still_requires_approval(monkeypatch, warehouse_dir: Path):
    """README: "Athena and BigQuery always require interactive approval,
    since they're not the bundled demo source" -- --yes must be silently
    ignored, not just documented."""
    monkeypatch.setattr("text_to_sql_agent.cli.AthenaConnection", lambda config: _FakeCloudConnection())
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "ask",
            "how many customers",
            "--warehouse-dir",
            str(warehouse_dir),
            "--dialect",
            "athena",
            "--athena-s3-staging-dir",
            "s3://bucket/path",
            "--athena-region",
            "us-east-1",
            "--yes",
        ],
        input="n\n",
    )

    assert "proceed" in result.output.lower()
    assert "aborted" in result.output.lower()
    assert "customer_count" not in result.output
    assert result.exit_code == 0


def test_ask_dialect_athena_approved_executes_and_skips_the_scan_cost_guard(
    monkeypatch, warehouse_dir: Path, tmp_path: Path
):
    """README/PROGRESS: the scan-cost guard only has meaning for the local
    warehouse, so for non-duckdb dialects it must be skipped (bytes/cost
    recorded as None with an explanatory note) rather than showing a
    fabricated number."""
    monkeypatch.setattr("text_to_sql_agent.cli.AthenaConnection", lambda config: _FakeCloudConnection())
    audit_log = tmp_path / "trace.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "ask",
            "how many customers",
            "--warehouse-dir",
            str(warehouse_dir),
            "--dialect",
            "athena",
            "--athena-s3-staging-dir",
            "s3://bucket/path",
            "--athena-region",
            "us-east-1",
            "--audit-log",
            str(audit_log),
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert "scan-cost estimate unavailable" in result.output
    assert "customer_count" in result.output

    steps = AuditTrace.load(audit_log)
    cost_step = next(step for step in steps if step["step_type"] == "cost_estimate")
    assert cost_step["payload"]["bytes_scanned"] is None
    assert cost_step["payload"]["cost_usd"] is None
    assert "duckdb" in cost_step["payload"]["note"]
    assert steps[-1]["payload"]["outcome"] == "executed"


def test_ask_dialect_bigquery_approved_executes_and_skips_the_scan_cost_guard(
    monkeypatch, warehouse_dir: Path, tmp_path: Path
):
    monkeypatch.setattr("text_to_sql_agent.cli.BigQueryConnection", lambda config: _FakeCloudConnection())
    audit_log = tmp_path / "trace.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "ask",
            "how many customers",
            "--warehouse-dir",
            str(warehouse_dir),
            "--dialect",
            "bigquery",
            "--bigquery-project",
            "my-project",
            "--audit-log",
            str(audit_log),
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert "scan-cost estimate unavailable" in result.output
    assert "customer_count" in result.output

    steps = AuditTrace.load(audit_log)
    cost_step = next(step for step in steps if step["step_type"] == "cost_estimate")
    assert cost_step["payload"]["bytes_scanned"] is None
    assert cost_step["payload"]["cost_usd"] is None
    assert steps[-1]["payload"]["outcome"] == "executed"
