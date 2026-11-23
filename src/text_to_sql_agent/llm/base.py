"""The provider contract the agent loop drafts queries against.

Any provider — a scripted deterministic policy, a local model, or a hosted
API — implements this interface, so the agent loop never depends on which
one is wired in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class CorrectionContext:
    """A previous attempt's SQL and the error it produced.

    Passed back into ``draft_sql`` on a retry so the provider can revise
    instead of drafting from scratch.
    """

    previous_sql: str
    error_message: str


class LLMProvider(ABC):
    """Drafts a SQL query for a natural-language question against a schema."""

    @abstractmethod
    def draft_sql(
        self,
        question: str,
        schema_context: str,
        dialect: str = "duckdb",
        correction: CorrectionContext | None = None,
    ) -> str:
        """Return candidate SQL text (not yet validated) for ``question``.

        ``schema_context`` is the catalog text (or retrieved subset of it)
        the provider should ground its answer in. When ``correction`` is
        set, this is a retry: the provider should fix ``previous_sql`` in
        light of ``error_message`` rather than starting over.
        """
        raise NotImplementedError
