"""Partition- and column-pruning-aware bytes-scanned estimate.

A real query engine does not charge for every byte on disk: a ``WHERE``
filter on a partition column skips whole partitions, and a narrow
``SELECT`` list skips columns the query never reads within the partitions
it does scan. This module estimates exactly that, from the query's own
parsed structure, instead of the earlier placeholder that simply summed
every referenced table's on-disk size.

The estimate is deliberately conservative: whenever a predicate or column
reference can't be resolved with confidence (an ambiguous unqualified
column in a multi-table query, a dynamic predicate like
``CURRENT_DATE - INTERVAL``, a bare ``*``), it falls back to charging for
more bytes rather than fewer, so the scan-cost guard it feeds never
under-estimates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pyarrow.parquet as pq
import sqlglot
from sqlglot import exp

_RANGE_COMPARISONS = (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


@dataclass(frozen=True)
class TableLocation:
    """Where one table's data lives on disk.

    ``path`` is a single Parquet file for an unpartitioned table, or the
    root directory of hive-style ``<column>=<value>`` partition
    subdirectories when ``partition_column`` is set.
    """

    path: Path
    partition_column: str | None = None


@dataclass(frozen=True)
class TableScanEstimate:
    """The scan estimate for one table referenced by a query."""

    bytes_scanned: int
    all_columns: bool
    columns_scanned: tuple[str, ...] = field(default_factory=tuple)
    partitions_scanned: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ScanEstimate:
    """The full bytes-scanned estimate for a validated query."""

    total_bytes: int
    per_table: dict[str, TableScanEstimate] = field(default_factory=dict)


def estimate_bytes_scanned(
    sql: str,
    dialect: str,
    table_locations: dict[str, TableLocation],
) -> ScanEstimate:
    """Estimate bytes scanned by ``sql``, pruning partitions and columns.

    ``table_locations`` maps lowercase table name to :class:`TableLocation`.
    Tables referenced by the query but absent from ``table_locations`` are
    skipped (they aren't local data this estimate can size).
    """
    statement = sqlglot.parse_one(sql, read=dialect)
    tables = list(statement.find_all(exp.Table))
    alias_map = {table.alias_or_name.lower(): table.name.lower() for table in tables}
    referenced_tables = sorted({table.name.lower() for table in tables})
    single_table = referenced_tables[0] if len(referenced_tables) == 1 else None

    all_columns_tables = _tables_needing_all_columns(statement, alias_map, single_table)

    per_table: dict[str, TableScanEstimate] = {}
    total = 0
    for table_name in referenced_tables:
        location = table_locations.get(table_name)
        if location is None:
            continue

        all_columns = table_name in all_columns_tables
        columns = (
            None if all_columns else _referenced_columns(statement, table_name, alias_map, single_table)
        )

        if location.partition_column is not None:
            constraint = _partition_constraint(
                statement, table_name, location.partition_column, alias_map, single_table
            )
            partition_dirs = _matching_partitions(location.path, location.partition_column, constraint)
            files = [path for directory in partition_dirs for path in _parquet_files(directory)]
            partitions_scanned: tuple[str, ...] | None = tuple(
                directory.name for directory in partition_dirs
            )
        else:
            files = [location.path]
            partitions_scanned = None

        table_bytes = sum(_file_bytes(path, all_columns, columns) for path in files)
        total += table_bytes
        per_table[table_name] = TableScanEstimate(
            bytes_scanned=table_bytes,
            all_columns=all_columns,
            columns_scanned=tuple(sorted(columns)) if columns else (),
            partitions_scanned=partitions_scanned,
        )

    return ScanEstimate(total_bytes=total, per_table=per_table)


def _resolve_table(table_ref: str, alias_map: dict[str, str], single_table: str | None) -> str | None:
    if table_ref:
        return alias_map.get(table_ref.lower())
    return single_table


def _tables_needing_all_columns(
    statement: exp.Expression, alias_map: dict[str, str], single_table: str | None
) -> set[str]:
    needs_all: set[str] = set()
    for star in statement.find_all(exp.Star):
        if isinstance(star.parent, exp.Column):
            resolved = _resolve_table(star.parent.table, alias_map, single_table)
            if resolved:
                needs_all.add(resolved)
            else:
                needs_all.update(alias_map.values())
        else:
            needs_all.update(alias_map.values())

    for column in statement.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        if not column.table and single_table is None:
            needs_all.update(alias_map.values())

    return needs_all


def _referenced_columns(
    statement: exp.Expression, table_name: str, alias_map: dict[str, str], single_table: str | None
) -> set[str]:
    columns: set[str] = set()
    for column in statement.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        if _resolve_table(column.table, alias_map, single_table) == table_name:
            columns.add(column.name)
    return columns


@dataclass
class _PartitionConstraint:
    exact_values: set[str] | None = None
    min_value: str | None = None
    max_value: str | None = None

    def allows(self, value: str) -> bool:
        if self.exact_values is not None and value not in self.exact_values:
            return False
        if self.min_value is not None and value < self.min_value:
            return False
        if self.max_value is not None and value > self.max_value:
            return False
        return True


def _partition_constraint(
    statement: exp.Expression,
    table_name: str,
    partition_column: str,
    alias_map: dict[str, str],
    single_table: str | None,
) -> _PartitionConstraint:
    constraint = _PartitionConstraint()
    where = statement.args.get("where")
    if where is None:
        return constraint
    for conjunct in _flatten_and(where.this):
        _apply_conjunct(conjunct, table_name, partition_column, alias_map, single_table, constraint)
    return constraint


def _flatten_and(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _flatten_and(node.left) + _flatten_and(node.right)
    return [node]


def _apply_conjunct(
    node: exp.Expression,
    table_name: str,
    partition_column: str,
    alias_map: dict[str, str],
    single_table: str | None,
    constraint: _PartitionConstraint,
) -> None:
    if isinstance(node, exp.In):
        column = node.this
        if not _is_partition_column(column, table_name, partition_column, alias_map, single_table):
            return
        values = {_literal_value(expression) for expression in node.expressions}
        if None in values:
            return
        constraint.exact_values = (
            values if constraint.exact_values is None else constraint.exact_values & values  # type: ignore[arg-type]
        )
        return

    if isinstance(node, _RANGE_COMPARISONS):
        column, literal, flipped = _column_and_literal(node)
        if column is None or literal is None:
            return
        if not _is_partition_column(column, table_name, partition_column, alias_map, single_table):
            return
        value = _literal_value(literal)
        if value is None:
            return

        operator = type(node)
        if flipped:
            operator = {
                exp.EQ: exp.EQ,
                exp.GT: exp.LT,
                exp.GTE: exp.LTE,
                exp.LT: exp.GT,
                exp.LTE: exp.GTE,
            }[operator]

        if operator is exp.EQ:
            constraint.exact_values = (
                {value} if constraint.exact_values is None else constraint.exact_values & {value}
            )
        elif operator in (exp.GT, exp.GTE):
            if constraint.min_value is None or value > constraint.min_value:
                constraint.min_value = value
        elif operator in (exp.LT, exp.LTE):
            if constraint.max_value is None or value < constraint.max_value:
                constraint.max_value = value


def _column_and_literal(
    node: exp.Expression,
) -> tuple[exp.Expression | None, exp.Expression | None, bool]:
    left, right = node.this, node.expression
    if _is_column_like(left) and not _is_column_like(right):
        return left, right, False
    if _is_column_like(right) and not _is_column_like(left):
        return right, left, True
    return None, None, False


def _is_column_like(expression: exp.Expression) -> bool:
    return isinstance(_unwrap_cast(expression), exp.Column)


def _unwrap_cast(expression: exp.Expression) -> exp.Expression:
    while isinstance(expression, exp.Cast):
        expression = expression.this
    return expression


def _is_partition_column(
    expression: exp.Expression,
    table_name: str,
    partition_column: str,
    alias_map: dict[str, str],
    single_table: str | None,
) -> bool:
    column = _unwrap_cast(expression)
    if not isinstance(column, exp.Column):
        return False
    if column.name.lower() != partition_column.lower():
        return False
    return _resolve_table(column.table, alias_map, single_table) == table_name


def _literal_value(expression: exp.Expression) -> str | None:
    literal = _unwrap_cast(expression)
    if isinstance(literal, exp.Literal):
        return str(literal.this)
    return None


def _matching_partitions(root: Path, partition_column: str, constraint: _PartitionConstraint) -> list[Path]:
    prefix = f"{partition_column}="
    matched = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and entry.name.startswith(prefix):
            value = entry.name[len(prefix) :]
            if constraint.allows(value):
                matched.append(entry)
    return matched


def _parquet_files(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.parquet"))


def _file_bytes(path: Path, all_columns: bool, columns: set[str] | None) -> int:
    if all_columns or not columns:
        return path.stat().st_size

    parquet_file = pq.ParquetFile(path)
    metadata = parquet_file.metadata
    schema_names = set(metadata.schema.names)
    if not columns <= schema_names:
        # A referenced column (e.g. a partition column read only from the
        # directory name) isn't in the file's physical schema -- fall back
        # to the full file size rather than guess.
        return path.stat().st_size

    sizes = dict.fromkeys(columns, 0)
    for row_group_index in range(metadata.num_row_groups):
        row_group = metadata.row_group(row_group_index)
        for column_index in range(row_group.num_columns):
            column = row_group.column(column_index)
            if column.path_in_schema in sizes:
                sizes[column.path_in_schema] += column.total_compressed_size
    return sum(sizes.values())
