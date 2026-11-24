"""Tests for SchemaRetriever: TF-IDF ranking and narrowing of the schema catalog.

PROGRESS.md and the README both promise: every table is ranked (never
dropped silently), narrowing by top-k actually narrows, and a question that
shares no vocabulary with the catalog falls back to the full catalog
instead of narrowing to nothing.
"""

from __future__ import annotations

from text_to_sql_agent.retrieval import SchemaRetriever


def test_rank_tables_returns_every_table_in_the_catalog(catalog):
    retriever = SchemaRetriever(catalog)
    matches = retriever.rank_tables("how many customers")
    assert {match.table for match in matches} == set(catalog.table_names)


def test_rank_tables_sorted_highest_score_first(catalog):
    retriever = SchemaRetriever(catalog)
    matches = retriever.rank_tables("events by type")
    scores = [match.score for match in matches]
    assert scores == sorted(scores, reverse=True)


def test_rank_tables_favors_the_table_whose_vocabulary_matches(catalog):
    retriever = SchemaRetriever(catalog)
    matches = retriever.rank_tables("count events grouped by event type")
    assert matches[0].table == "events"


def test_rank_tables_favors_customers_for_a_country_question(catalog):
    retriever = SchemaRetriever(catalog)
    matches = retriever.rank_tables("customers grouped by country")
    assert matches[0].table == "customers"


def test_rank_columns_returns_every_column_of_the_table(catalog):
    retriever = SchemaRetriever(catalog)
    table = catalog.tables["orders"]
    scores = retriever.rank_columns("order status", "orders")
    assert {name for name, _score in scores} == set(table.column_names)


def test_rank_columns_favors_the_matching_column(catalog):
    retriever = SchemaRetriever(catalog)
    scores = retriever.rank_columns("order lifecycle status state", "orders")
    assert scores[0][0] == "status"


def test_retrieve_with_no_top_k_keeps_the_full_catalog(catalog):
    retriever = SchemaRetriever(catalog)
    narrowed = retriever.retrieve("how many customers")
    assert set(narrowed.table_names) == set(catalog.table_names)


def test_retrieve_top_k_tables_narrows_to_n_tables(catalog):
    retriever = SchemaRetriever(catalog)
    narrowed = retriever.retrieve("how many customers", top_k_tables=1)
    assert len(narrowed.table_names) == 1
    assert "customers" in narrowed.table_names


def test_retrieve_top_k_columns_narrows_columns_within_kept_tables(catalog):
    retriever = SchemaRetriever(catalog)
    narrowed = retriever.retrieve(
        "order status", top_k_tables=1, top_k_columns_per_table=2
    )
    orders_table = narrowed.tables["orders"]
    assert len(orders_table.columns) == 2
    assert set(orders_table.column_names).issubset(set(catalog.tables["orders"].column_names))


def test_retrieve_top_k_columns_larger_than_table_keeps_all_columns(catalog):
    retriever = SchemaRetriever(catalog)
    narrowed = retriever.retrieve(
        "how many customers", top_k_tables=1, top_k_columns_per_table=1000
    )
    assert len(narrowed.tables["customers"].columns) == len(catalog.tables["customers"].columns)


def test_retrieve_falls_back_to_the_full_catalog_when_nothing_matches(catalog):
    retriever = SchemaRetriever(catalog)
    narrowed = retriever.retrieve("xyzzyblorp quuxfrobnicate wibbleflange", top_k_tables=1)
    assert set(narrowed.table_names) == set(catalog.table_names)


def test_retrieve_does_not_fall_back_when_at_least_one_table_scores_nonzero(catalog):
    retriever = SchemaRetriever(catalog)
    narrowed = retriever.retrieve("customers", top_k_tables=1)
    assert narrowed.table_names == ("customers",)


def test_retrieve_preserves_table_metadata_when_narrowing_columns(catalog):
    retriever = SchemaRetriever(catalog)
    narrowed = retriever.retrieve("event type", top_k_tables=1, top_k_columns_per_table=1)
    events_table = narrowed.tables["events"]
    assert events_table.partition_columns == catalog.tables["events"].partition_columns
    assert events_table.description == catalog.tables["events"].description


def test_retrieve_narrowed_catalog_to_text_only_mentions_kept_tables(catalog):
    retriever = SchemaRetriever(catalog)
    narrowed = retriever.retrieve("how many customers", top_k_tables=1)
    text = narrowed.to_text()
    assert "customers" in text
    for other_table in set(catalog.table_names) - {"customers"}:
        assert other_table not in text
