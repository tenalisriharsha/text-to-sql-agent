"""Tests for pricing models and the budget/agent config knobs documented in the README."""

from __future__ import annotations

import pytest

from text_to_sql_agent.config import (
    ATHENA_PRICING,
    BIGQUERY_PRICING,
    AgentConfig,
    BudgetConfig,
    PricingModel,
    pricing_model_for,
)


def test_athena_pricing_matches_documented_rate():
    assert ATHENA_PRICING.price_per_tib_usd == 5.0


def test_bigquery_pricing_matches_documented_rate():
    assert BIGQUERY_PRICING.price_per_tib_usd == 6.25


def test_estimate_cost_usd_scales_linearly_with_bytes_scanned():
    model = PricingModel(name="test", price_per_tib_usd=1024.0, min_bytes_billed=0)
    one_tib = 1024**4
    assert model.estimate_cost_usd(one_tib) == pytest.approx(1024.0)
    assert model.estimate_cost_usd(one_tib // 2) == pytest.approx(512.0)


def test_estimate_cost_usd_rounds_up_to_minimum_billed_bytes():
    model = PricingModel(name="test", price_per_tib_usd=1024.0, min_bytes_billed=1024**3)
    assert model.estimate_cost_usd(1) == pytest.approx(1.0)


def test_estimate_cost_usd_rejects_negative_bytes():
    model = PricingModel(name="test", price_per_tib_usd=1.0, min_bytes_billed=0)
    with pytest.raises(ValueError):
        model.estimate_cost_usd(-1)


def test_pricing_model_for_known_names():
    assert pricing_model_for("athena") is ATHENA_PRICING
    assert pricing_model_for("bigquery") is BIGQUERY_PRICING


def test_pricing_model_for_unknown_name_lists_known_models():
    with pytest.raises(ValueError, match="athena"):
        pricing_model_for("snowflake")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes_scanned": 0},
        {"max_bytes_scanned": -1},
        {"max_cost_usd": 0},
        {"max_cost_usd": -1},
        {"confirm_above_usd": -0.01},
    ],
)
def test_budget_config_rejects_invalid_values(kwargs):
    with pytest.raises(ValueError):
        BudgetConfig(**kwargs)


def test_budget_config_defaults_are_internally_consistent():
    budget = BudgetConfig()
    assert budget.confirm_above_usd < budget.max_cost_usd
    assert budget.estimate_cost_usd(0) >= 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_correction_attempts": -1},
        {"default_row_limit": 0},
        {"default_row_limit": -5},
        {"retrieval_top_k_tables": 0},
        {"retrieval_top_k_tables": -1},
        {"retrieval_top_k_columns_per_table": 0},
        {"retrieval_top_k_columns_per_table": -1},
    ],
)
def test_agent_config_rejects_invalid_values(kwargs):
    with pytest.raises(ValueError):
        AgentConfig(**kwargs)


def test_agent_config_defaults_match_documented_values():
    config = AgentConfig()
    assert config.max_correction_attempts == 3
    assert config.default_row_limit == 1000
    assert config.require_human_approval is True
    assert isinstance(config.budget, BudgetConfig)
    # README: "defaults: top 2 tables, every column kept"
    assert config.retrieval_top_k_tables == 2
    assert config.retrieval_top_k_columns_per_table is None


def test_agent_config_allows_none_retrieval_top_k_to_mean_keep_all():
    config = AgentConfig(retrieval_top_k_tables=None, retrieval_top_k_columns_per_table=None)
    assert config.retrieval_top_k_tables is None
    assert config.retrieval_top_k_columns_per_table is None


def test_agent_config_accepts_positive_retrieval_top_k_values():
    config = AgentConfig(retrieval_top_k_tables=1, retrieval_top_k_columns_per_table=3)
    assert config.retrieval_top_k_tables == 1
    assert config.retrieval_top_k_columns_per_table == 3
