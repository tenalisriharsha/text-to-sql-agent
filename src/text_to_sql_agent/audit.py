"""An append-only audit trail of every step the agent takes on one question.

Every retrieval, draft, validation verdict, engine error, revision, cost
estimate, and approval decision is recorded here, in order, so a run can be
reconstructed and reviewed after the fact without re-running anything.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AuditStep:
    """One recorded event in a trace, in the order it happened."""

    step_type: str
    timestamp: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditTrace:
    """Records steps in memory and, if given a path, appends them as JSON Lines."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.steps: list[AuditStep] = []
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, step_type: str, **payload: Any) -> AuditStep:
        step = AuditStep(
            step_type=step_type,
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload=payload,
        )
        self.steps.append(step)
        if self.path is not None:
            with self.path.open("a") as handle:
                handle.write(json.dumps(step.to_dict(), ensure_ascii=False) + "\n")
        return step

    def to_list(self) -> list[dict[str, Any]]:
        return [step.to_dict() for step in self.steps]

    @staticmethod
    def load(path: Path | str) -> list[dict[str, Any]]:
        with Path(path).open() as handle:
            return [json.loads(line) for line in handle if line.strip()]
