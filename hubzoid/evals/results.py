"""Result records for one case and one suite run, plus their JSON shape.

The JSON file written after every run (`<hub>/.hubzoid/evals/<ts>.json`) is the
durable record: it is what `--compare` diffs, what `eval status` reads, and
what CI keeps as an artifact. It is written whether or not Langfuse is
configured — local files are the floor, Langfuse is an upgrade on top.

Keep `to_dict` / `from_dict` symmetric. A field that round-trips wrong shows
up as a phantom regression in `--compare`, which is worse than not recording
it at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .assertions import Check

SCHEMA_VERSION = 1


@dataclass
class JudgeResult:
    score: int                    # 1-10
    threshold: int
    reasoning: str = ""
    model: str = ""
    error: str | None = None      # judge itself failed (not the case failing)

    @property
    def passed(self) -> bool:
        return self.error is None and self.score >= self.threshold

    def to_dict(self) -> dict:
        return {
            "score": self.score, "threshold": self.threshold,
            "reasoning": self.reasoning, "model": self.model, "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "JudgeResult":
        return cls(
            score=int(d.get("score", 0)), threshold=int(d.get("threshold", 0)),
            reasoning=d.get("reasoning", ""), model=d.get("model", ""),
            error=d.get("error"),
        )


@dataclass
class CaseResult:
    name: str
    tags: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    judge: JudgeResult | None = None
    response: str = ""
    tool_calls: list[str] = field(default_factory=list)
    duration: float = 0.0
    error: str | None = None      # the run blew up / timed out

    @property
    def free_passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)

    @property
    def passed(self) -> bool:
        if not self.free_passed:
            return False
        if self.judge is None:
            return True
        return self.judge.passed

    @property
    def reason(self) -> str:
        """One line for the terminal table. '' when the case passed."""
        if self.error:
            return self.error
        for c in self.checks:
            if not c.passed:
                return c.detail or c.kind
        if self.judge is not None and not self.judge.passed:
            if self.judge.error:
                return f"judge failed: {self.judge.error}"
            return f"judge {self.judge.score}/10 < {self.judge.threshold}"
        return ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tags": list(self.tags),
            "passed": self.passed,
            "reason": self.reason,
            "duration": round(self.duration, 3),
            "error": self.error,
            "checks": [c.to_dict() for c in self.checks],
            "judge": self.judge.to_dict() if self.judge else None,
            "tool_calls": list(self.tool_calls),
            "response": self.response,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CaseResult":
        return cls(
            name=d.get("name", "?"),
            tags=list(d.get("tags") or []),
            checks=[Check(kind=c.get("kind", "?"), passed=bool(c.get("passed")),
                          detail=c.get("detail", ""))
                    for c in (d.get("checks") or [])],
            judge=JudgeResult.from_dict(d["judge"]) if d.get("judge") else None,
            response=d.get("response", ""),
            tool_calls=list(d.get("tool_calls") or []),
            duration=float(d.get("duration") or 0.0),
            error=d.get("error"),
        )


@dataclass
class SuiteResult:
    hub: str
    cases: list[CaseResult] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    model: str = ""
    judge_model: str | None = None
    judged: bool = True           # was the judge tier enabled for this run

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cases if not c.passed)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "hub": self.hub,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "model": self.model,
            "judge_model": self.judge_model,
            "judged": self.judged,
            "passed": self.passed,
            "failed": self.failed,
            "cases": [c.to_dict() for c in self.cases],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SuiteResult":
        return cls(
            hub=d.get("hub", "?"),
            cases=[CaseResult.from_dict(c) for c in (d.get("cases") or [])],
            started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at", ""),
            model=d.get("model", ""),
            judge_model=d.get("judge_model"),
            judged=bool(d.get("judged", True)),
        )


def now_iso() -> str:
    """Local time WITH its UTC offset, e.g. 2026-08-20T20:45:06+05:30.

    The offset is not decoration. Langfuse's ingestion API validates
    timestamps against a strict ISO-8601 pattern that requires `Z` or an
    offset, and rejects every event carrying a naive one. It also makes the
    JSON record unambiguous when a run is read on a box in another zone.
    """
    return datetime.now().astimezone().replace(microsecond=0).isoformat()
