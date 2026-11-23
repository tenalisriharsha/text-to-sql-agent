"""Schema catalog: table and column metadata used for retrieval and validation.

The catalog is the single source of truth the agent consults before drafting
a query. Each table and column carries a human-written description plus a
handful of real sample values, which Phase 2's embedding search will index to
find the tables and columns relevant to a question. For now the catalog is
built by introspecting a live DuckDB connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import duckdb


@dataclass(frozen=True)
class ColumnSchema:
    """One column's metadata: type, description, and a few real values."""

    name: str
    data_type: str
    description: str = ""
    sample_values: tuple[str, ...] = ()

    def to_text(self) -> str:
        parts = [self.name, self.data_type]
        if self.description:
            parts.append(self.description)
        if self.sample_values:
            parts.append("examples: " + ", ".join(self.sample_values))
        return " | ".join(parts)


@dataclass(frozen=True)
class TableSchema:
    """One table's metadata: its columns and, if partitioned, which columns."""

    name: str
    description: str = ""
    columns: tuple[ColumnSchema, ...] = ()
    partition_columns: tuple[str, ...] = ()

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def get_column(self, name: str) -> ColumnSchema | None:
        for column in self.columns:
            if column.name == name:
                return column
        return None

    def to_text(self) -> str:
        header = self.name
        if self.description:
            header += f": {self.description}"
        column_lines = "\n".join(f"  - {c.to_text()}" for c in self.columns)
        return f"{header}\n{column_lines}"


@dataclass(frozen=True)
class SchemaCatalog:
    """The full set of tables an agent is allowed to know about."""

    tables: dict[str, TableSchema] = field(default_factory=dict)

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(self.tables.keys())

    def get(self, table_name: str) -> TableSchema | None:
        return self.tables.get(table_name)

    def to_text(self) -> str:
        return "\n\n".join(table.to_text() for table in self.tables.values())

    @classmethod
    def from_duckdb(
        cls,
        connection: duckdb.DuckDBPyConnection,
        table_descriptions: dict[str, str] | None = None,
        column_descriptions: dict[str, dict[str, str]] | None = None,
        partition_columns: dict[str, tuple[str, ...]] | None = None,
        sample_value_limit: int = 5,
    ) -> SchemaCatalog:
        """Introspect every base table visible on ``connection``.

        Descriptions are supplied by the caller (DuckDB has no column-comment
        storage reachable from Python), keyed by table name and, for columns,
        by ``(table name) -> {column name: description}``.
        """
        table_descriptions = table_descriptions or {}
        column_descriptions = column_descriptions or {}
        partition_columns = partition_columns or {}

        table_rows = connection.execute(
            "select table_name from information_schema.tables"
            " where table_schema = 'main' order by table_name"
        ).fetchall()

        tables: dict[str, TableSchema] = {}
        for (table_name,) in table_rows:
            column_rows = connection.execute(
                "select column_name, data_type from information_schema.columns"
                " where table_schema = 'main' and table_name = ?"
                " order by ordinal_position",
                [table_name],
            ).fetchall()

            table_column_descriptions = column_descriptions.get(table_name, {})
            columns = []
            for column_name, data_type in column_rows:
                sample_values = _sample_values(
                    connection, table_name, column_name, sample_value_limit
                )
                columns.append(
                    ColumnSchema(
                        name=column_name,
                        data_type=data_type,
                        description=table_column_descriptions.get(column_name, ""),
                        sample_values=sample_values,
                    )
                )

            tables[table_name] = TableSchema(
                name=table_name,
                description=table_descriptions.get(table_name, ""),
                columns=tuple(columns),
                partition_columns=partition_columns.get(table_name, ()),
            )

        return cls(tables=tables)


def _sample_values(
    connection: duckdb.DuckDBPyConnection,
    table_name: str,
    column_name: str,
    limit: int,
) -> tuple[str, ...]:
    if limit <= 0:
        return ()
    quoted_table = f'"{table_name}"'
    quoted_column = f'"{column_name}"'
    rows = connection.execute(
        f"select distinct {quoted_column} from {quoted_table}"
        f" where {quoted_column} is not null limit {limit}"
    ).fetchall()
    return tuple(str(row[0]) for row in rows)
