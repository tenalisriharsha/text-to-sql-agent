"""Configuration: pricing models, scan-cost budgets, and agent-loop limits."""

from __future__ import annotations

from dataclasses import dataclass, field

BYTES_PER_TIB = 1024**4


@dataclass(frozen=True)
class PricingModel:
    """A warehouse's on-demand bytes-scanned pricing.

    ``price_per_tib_usd`` is the list price per tebibyte scanned, and
    ``min_bytes_billed`` is the minimum a single query is billed for, even if
    it scans less (both engines round small queries up to a minimum).
    """

    name: str
    price_per_tib_usd: float
    min_bytes_billed: int

    def estimate_cost_usd(self, bytes_scanned: int) -> float:
        if bytes_scanned < 0:
            raise ValueError("bytes_scanned must be non-negative")
        billed_bytes = max(bytes_scanned, self.min_bytes_billed)
        return (billed_bytes / BYTES_PER_TIB) * self.price_per_tib_usd


# Public list prices as of general availability; callers may construct their
# own PricingModel for a negotiated rate or a different engine.
ATHENA_PRICING = PricingModel(
    name="athena", price_per_tib_usd=5.0, min_bytes_billed=10 * 1024 * 1024
)
BIGQUERY_PRICING = PricingModel(
    name="bigquery", price_per_tib_usd=6.25, min_bytes_billed=10 * 1024 * 1024
)

PRICING_MODELS: dict[str, PricingModel] = {
    "athena": ATHENA_PRICING,
    "bigquery": BIGQUERY_PRICING,
}


def pricing_model_for(name: str) -> PricingModel:
    try:
        return PRICING_MODELS[name]
    except KeyError as exc:
        known = ", ".join(sorted(PRICING_MODELS))
        raise ValueError(f"unknown pricing model '{name}'; known models: {known}") from exc


@dataclass(frozen=True)
class BudgetConfig:
    """Scan-cost guardrails for a single question.

    ``max_bytes_scanned`` and ``max_cost_usd`` are hard ceilings: an estimate
    above either is refused outright. ``confirm_above_usd`` is a softer
    threshold below the hard ceiling: an estimate above it still runs, but
    only after explicit human confirmation.
    """

    pricing_model: PricingModel = ATHENA_PRICING
    max_bytes_scanned: int = 100 * 1024**3  # 100 GiB
    max_cost_usd: float = 5.0
    confirm_above_usd: float = 0.50

    def __post_init__(self) -> None:
        if self.max_bytes_scanned <= 0:
            raise ValueError("max_bytes_scanned must be positive")
        if self.max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be positive")
        if self.confirm_above_usd < 0:
            raise ValueError("confirm_above_usd must be non-negative")

    def estimate_cost_usd(self, bytes_scanned: int) -> float:
        return self.pricing_model.estimate_cost_usd(bytes_scanned)


@dataclass(frozen=True)
class AgentConfig:
    """Limits governing a single ask-the-agent run."""

    max_correction_attempts: int = 3
    default_row_limit: int = 1000
    table_allowlist: tuple[str, ...] = field(default_factory=tuple)
    require_human_approval: bool = True
    budget: BudgetConfig = field(default_factory=BudgetConfig)

    def __post_init__(self) -> None:
        if self.max_correction_attempts < 0:
            raise ValueError("max_correction_attempts must be non-negative")
        if self.default_row_limit <= 0:
            raise ValueError("default_row_limit must be positive")
