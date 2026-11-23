"""Translate validated SQL between the engines the agent can target."""

from __future__ import annotations

import sqlglot

# sqlglot has no "athena" dialect of its own; Athena speaks Trino/Presto SQL.
_DIALECT_ALIASES = {"athena": "trino"}

SUPPORTED_DIALECTS = ("duckdb", "athena", "bigquery")


def _resolve(dialect: str) -> str:
    return _DIALECT_ALIASES.get(dialect, dialect)


def translate_dialect(sql: str, source_dialect: str, target_dialect: str) -> str:
    """Re-render ``sql`` (already written for ``source_dialect``) for ``target_dialect``."""
    translated = sqlglot.transpile(
        sql, read=_resolve(source_dialect), write=_resolve(target_dialect)
    )
    return translated[0]
