"""Validate a candidate query before it is ever sent to a real engine.

The rules are deliberately strict: exactly one statement, read-only
(``SELECT``/set operations, nothing else), every referenced table on an
explicit allowlist, and a ``LIMIT`` that cannot exceed a configured ceiling.
Anything that fails is reported back to the caller so the agent's
self-correction loop can revise and retry rather than ever reaching an
engine with an invalid or unsafe query.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

READ_ONLY_STATEMENT_TYPES = (exp.Select, exp.Union, exp.Except, exp.Intersect)

FORBIDDEN_NODE_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Merge,
    exp.Command,
    exp.Attach,
    exp.Detach,
)


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of validating one candidate query."""

    is_valid: bool
    errors: tuple[str, ...] = field(default_factory=tuple)
    normalized_sql: str | None = None
    referenced_tables: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def failure(cls, *errors: str) -> ValidationResult:
        return cls(is_valid=False, errors=tuple(errors))


def validate_query(
    sql: str,
    table_allowlist: Sequence[str],
    dialect: str = "duckdb",
    default_row_limit: int = 1000,
) -> ValidationResult:
    """Parse, check, and normalize ``sql``.

    On success, ``normalized_sql`` always has a ``LIMIT`` of at most
    ``default_row_limit`` and is safe to hand to the target engine's
    ``EXPLAIN``. ``table_allowlist`` is matched case-insensitively against
    bare table names (schema/catalog qualifiers are ignored).
    """
    try:
        statements = [
            statement
            for statement in sqlglot.parse(sql, read=dialect)
            if statement is not None
        ]
    except sqlglot.errors.SqlglotError as exc:
        return ValidationResult.failure(f"could not parse SQL: {exc}")

    if len(statements) == 0:
        return ValidationResult.failure("no SQL statement found")
    if len(statements) > 1:
        return ValidationResult.failure(
            f"exactly one SQL statement is allowed, found {len(statements)}"
        )

    statement = statements[0]

    if not isinstance(statement, READ_ONLY_STATEMENT_TYPES):
        return ValidationResult.failure(
            "only SELECT queries (including UNION/EXCEPT/INTERSECT) are allowed, "
            f"got {type(statement).__name__}"
        )

    forbidden = [node for node in statement.walk() if isinstance(node[0], FORBIDDEN_NODE_TYPES)]
    if forbidden:
        kinds = sorted({type(node[0]).__name__ for node in forbidden})
        return ValidationResult.failure(f"disallowed SQL construct(s): {', '.join(kinds)}")

    referenced_tables = tuple(sorted({table.name for table in statement.find_all(exp.Table)}))
    allowed = {name.lower() for name in table_allowlist}
    disallowed = sorted(
        {table for table in referenced_tables if table.lower() not in allowed}
    )
    if disallowed:
        return ValidationResult.failure(
            f"table(s) not on the allowlist: {', '.join(disallowed)}"
        )

    limited_statement = _enforce_limit(statement, default_row_limit)
    normalized_sql = limited_statement.sql(dialect=dialect)

    return ValidationResult(
        is_valid=True,
        normalized_sql=normalized_sql,
        referenced_tables=referenced_tables,
    )


def _enforce_limit(statement: exp.Expression, default_row_limit: int) -> exp.Expression:
    existing_limit = statement.args.get("limit")
    if existing_limit is None:
        return statement.limit(default_row_limit, copy=True)

    existing_value = existing_limit.expression
    if isinstance(existing_value, exp.Literal) and existing_value.is_number:
        if int(existing_value.this) <= default_row_limit:
            return statement
    return statement.limit(default_row_limit, copy=True)
