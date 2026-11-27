"""The draft / validate / EXPLAIN / self-correct loop, independent of the CLI.

This is the bounded retry loop described in the architecture: a candidate
is drafted, checked against the SQL safety contract, and then checked
against the real target engine's ``EXPLAIN`` so a query that passes the
parser-level validator but is semantically wrong (an unknown column, a
type mismatch) is still caught before it ever reaches a human approval
prompt or executes. Either kind of failure becomes a ``CorrectionContext``
fed back into the provider on the next attempt, up to a configured bound.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

from text_to_sql_agent.audit import AuditTrace
from text_to_sql_agent.config import AgentConfig
from text_to_sql_agent.engines.base import EngineExecutionError
from text_to_sql_agent.llm.base import CorrectionContext, LLMProvider
from text_to_sql_agent.llm.scripted import ClarificationNeededError, UnsupportedQuestionError
from text_to_sql_agent.sql.validator import ValidationResult, validate_query


@dataclass(frozen=True)
class AgentResult:
    """The outcome of running the loop once for a single question.

    ``explain_plan`` is only set when ``validation.is_valid`` is True: it is
    the plan text from the attempt that finally passed both the validator
    and the engine's ``EXPLAIN``. ``draft_error`` is set instead of
    ``validation`` when the provider could not draft anything at all (for
    example, ``ScriptedProvider`` has no rule matching the question).
    ``needs_clarification`` is set instead when the provider recognized the
    question but judged it to have no single correct interpretation --
    this is a deliberate refusal to guess, not a failure to understand.
    """

    sql: str | None
    validation: ValidationResult | None
    explain_plan: str | None
    attempts: int
    draft_error: str | None = None
    needs_clarification: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.draft_error is None and self.validation is not None and self.validation.is_valid


def run_agent_loop(
    provider: LLMProvider,
    question: str,
    schema_context: str,
    table_allowlist: tuple[str, ...],
    dialect: str,
    connection: duckdb.DuckDBPyConnection,
    config: AgentConfig,
    trace: AuditTrace,
) -> AgentResult:
    """Draft, validate, and EXPLAIN-check ``question`` against ``connection``.

    ``connection`` only needs to support ``.execute(sql)`` returning an
    object with ``.fetchall()`` -- DuckDB's native connection satisfies
    this directly; the Athena and BigQuery adapters in ``engines/`` wrap
    their own drivers to satisfy it too, so this loop works unmodified
    against any of the three (see ``engines.base.Connection``).

    ``table_allowlist`` is the full catalog's table names, independent of
    whatever subset of the catalog ``schema_context`` was narrowed to by
    retrieval: narrowing what the provider sees must never narrow what the
    validator allows a query to reference.
    """
    correction: CorrectionContext | None = None
    attempts = config.max_correction_attempts + 1
    sql: str | None = None
    validation: ValidationResult | None = None

    for attempt in range(1, attempts + 1):
        try:
            sql = provider.draft_sql(question, schema_context, dialect, correction)
        except ClarificationNeededError as exc:
            trace.record(
                "needs_clarification", attempt=attempt, reason=exc.reason, options=list(exc.options)
            )
            return AgentResult(
                sql=None,
                validation=None,
                explain_plan=None,
                attempts=attempt,
                needs_clarification=exc.reason,
            )
        except UnsupportedQuestionError as exc:
            trace.record("draft_failed", attempt=attempt, error=str(exc))
            return AgentResult(
                sql=None, validation=None, explain_plan=None, attempts=attempt, draft_error=str(exc)
            )

        trace.record("draft", attempt=attempt, sql=sql)
        validation = validate_query(
            sql,
            table_allowlist=table_allowlist,
            dialect=dialect,
            default_row_limit=config.default_row_limit,
        )
        trace.record(
            "validation",
            attempt=attempt,
            is_valid=validation.is_valid,
            errors=list(validation.errors),
        )

        if not validation.is_valid:
            correction = CorrectionContext(previous_sql=sql, error_message="; ".join(validation.errors))
            continue

        assert validation.normalized_sql is not None
        try:
            plan_rows = connection.execute(f"EXPLAIN {validation.normalized_sql}").fetchall()
        except (duckdb.Error, EngineExecutionError) as exc:
            engine_error = str(exc)
            trace.record("engine_error", attempt=attempt, error=engine_error)
            validation = ValidationResult.failure(f"engine rejected query: {engine_error}")
            correction = CorrectionContext(previous_sql=sql, error_message=engine_error)
            continue

        plan_text = "\n".join(str(row[-1]) for row in plan_rows)
        return AgentResult(sql=sql, validation=validation, explain_plan=plan_text, attempts=attempt)

    return AgentResult(sql=sql, validation=validation, explain_plan=None, attempts=attempts)
