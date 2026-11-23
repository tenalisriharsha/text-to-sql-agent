"""SQL parsing, validation, and dialect translation built on sqlglot."""

from text_to_sql_agent.sql.validator import ValidationResult, validate_query
from text_to_sql_agent.sql.dialect import translate_dialect

__all__ = ["ValidationResult", "validate_query", "translate_dialect"]
