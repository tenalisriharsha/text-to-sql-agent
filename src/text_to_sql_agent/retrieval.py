"""Local retrieval over the schema catalog, scoped to one question.

Builds a TF-IDF vector for every table (and, within a table, every column)
from its description and sample values, then ranks tables/columns against
a question by cosine similarity. This is a local, dependency-free stand-in
for an embedding search: no network call or API key is involved, matching
the project's no-API-key requirement, while still giving the agent loop a
narrowed, question-specific schema context instead of the whole catalog.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from text_to_sql_agent.schema_catalog import ColumnSchema, SchemaCatalog, TableSchema

_TOKEN_RE = re.compile(r"[a-z0-9]+")

Vector = dict[str, float]


@dataclass(frozen=True)
class TableMatch:
    """One table's relevance score for a question, highest first."""

    table: str
    score: float


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _table_document(table: TableSchema) -> list[str]:
    tokens = _tokenize(table.name) + _tokenize(table.description)
    for column in table.columns:
        tokens.extend(_column_document(column))
    return tokens


def _column_document(column: ColumnSchema) -> list[str]:
    tokens = _tokenize(column.name) + _tokenize(column.description)
    for value in column.sample_values:
        tokens.extend(_tokenize(value))
    return tokens


def _idf(documents: list[list[str]]) -> dict[str, float]:
    """Smoothed inverse document frequency: common terms score near zero."""
    n = len(documents)
    if n == 0:
        return {}
    doc_frequency: Counter[str] = Counter()
    for tokens in documents:
        doc_frequency.update(set(tokens))
    return {term: math.log((1 + n) / (1 + freq)) + 1.0 for term, freq in doc_frequency.items()}


def _vector(tokens: list[str], idf: dict[str, float]) -> Vector:
    counts = Counter(tokens)
    return {term: count * idf[term] for term, count in counts.items() if term in idf}


def _cosine(a: Vector, b: Vector) -> float:
    if not a or not b:
        return 0.0
    common = a.keys() & b.keys()
    if not common:
        return 0.0
    numerator = sum(a[term] * b[term] for term in common)
    norm_a = math.sqrt(sum(value * value for value in a.values()))
    norm_b = math.sqrt(sum(value * value for value in b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return numerator / (norm_a * norm_b)


class SchemaRetriever:
    """Ranks and narrows a :class:`SchemaCatalog` against a question.

    Each table's document is its own name, description, and the name,
    description, and sample values of every one of its columns; each
    column's document is just its own text. IDF is computed separately
    at the table level (documents = tables) and, per table, at the
    column level (documents = that table's columns), so a term that is
    common across every table in the catalog but distinguishes columns
    within one table still carries weight there.
    """

    def __init__(self, catalog: SchemaCatalog) -> None:
        self._catalog = catalog
        table_names = catalog.table_names
        table_docs = [_table_document(catalog.tables[name]) for name in table_names]
        table_idf = _idf(table_docs)
        self._table_idf = table_idf
        self._table_vectors: dict[str, Vector] = {
            name: _vector(doc, table_idf) for name, doc in zip(table_names, table_docs)
        }

        self._column_idf: dict[str, dict[str, float]] = {}
        self._column_vectors: dict[str, dict[str, Vector]] = {}
        for name in table_names:
            table = catalog.tables[name]
            column_docs = {column.name: _column_document(column) for column in table.columns}
            idf = _idf(list(column_docs.values()))
            self._column_idf[name] = idf
            self._column_vectors[name] = {
                column_name: _vector(doc, idf) for column_name, doc in column_docs.items()
            }

    def rank_tables(self, question: str) -> list[TableMatch]:
        """Every table in the catalog, ranked highest-score first."""
        query_vector = _vector(_tokenize(question), self._table_idf)
        matches = [
            TableMatch(table=name, score=_cosine(query_vector, vector))
            for name, vector in self._table_vectors.items()
        ]
        matches.sort(key=lambda match: match.score, reverse=True)
        return matches

    def rank_columns(self, question: str, table_name: str) -> list[tuple[str, float]]:
        """Every column of ``table_name``, ranked highest-score first."""
        idf = self._column_idf[table_name]
        query_vector = _vector(_tokenize(question), idf)
        scores = [
            (column_name, _cosine(query_vector, vector))
            for column_name, vector in self._column_vectors[table_name].items()
        ]
        scores.sort(key=lambda item: item[1], reverse=True)
        return scores

    def retrieve(
        self,
        question: str,
        top_k_tables: int | None = None,
        top_k_columns_per_table: int | None = None,
    ) -> SchemaCatalog:
        """A :class:`SchemaCatalog` narrowed to the question's top-k matches.

        Falls back to the full, unnarrowed catalog if every table scores
        zero (the question shares no vocabulary with the catalog at all):
        narrowing to nothing would make the question impossible to answer,
        and a zero score carries no information about which tables matter.
        """
        ranked = self.rank_tables(question)
        if not ranked or all(match.score <= 0.0 for match in ranked):
            return self._catalog

        if top_k_tables is not None:
            ranked = ranked[:top_k_tables]

        tables: dict[str, TableSchema] = {}
        for match in ranked:
            table = self._catalog.tables[match.table]
            if top_k_columns_per_table is None or top_k_columns_per_table >= len(table.columns):
                tables[match.table] = table
                continue
            kept = {name for name, _score in self.rank_columns(question, match.table)[:top_k_columns_per_table]}
            tables[match.table] = TableSchema(
                name=table.name,
                description=table.description,
                columns=tuple(column for column in table.columns if column.name in kept),
                partition_columns=table.partition_columns,
            )
        return SchemaCatalog(tables=tables)
