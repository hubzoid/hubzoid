"""Result records for one case and one suite run, plus their JSON shape.

The JSON file written after every run (`<hub>/.hubzoid/evals/<ts>.json`) is the
durable record: it is what `--compare` diffs, what `eval status` reads, and
what CI keeps as an artifact. It is written whether or not Langfuse is
configured — local files are the floor, Langfuse is an upgrade on top.

Keep `to_dict` / `from_dict` symmetric. A field that round-trips wrong shows
up as a phantom regression in `--compare`, which is worse than not recording
it at all.

Schema 2 (1.1) adds, per suite, `trigger` (cli, schedule, console, ci) and
`run_as`; per case, `tools` (each call with its arguments, outcome, duration,
an optional result preview and its turn), `turns` (multi-turn cases) and
`run_as`. `tool_calls` stays the list of tool names, for older readers.
Schema 1 files still load: `tools` is rebuilt from `tool_calls` with no
arguments, and `trigger` reads as None.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .assertions import Check

SCHEMA_VERSION = 2

TRIGGERS = ("cli", "schedule", "console", "ci")


@dataclass
class ToolCallRecord:
    """One tool call of a case, in order. `args` is already shortened and has
    secret-looking values redacted (see `calls.safe_args`); `raw_args` keeps
    the runtime's arguments in memory for `expect_tool_args` and is never
    written. `ok` and `duration_ms` are None when the runtime did not report a
    result; `preview` is the start of the result, when the runtime recorded one."""
    name: str
    args: dict | None = None
    ok: bool | None = None
    error: str | None = None
    duration_ms: int | None = None
    preview: str | None = None
    turn: int = 1
    raw_args: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict:
        return {"name": self.name, "args": self.args, "ok": self.ok, "error": self.error,
                "duration_ms": self.duration_ms, "preview": self.preview, "turn": self.turn}

    @classmethod
    def from_dict(cls, d: dict) -> "ToolCallRecord":
        args = d.get("args")
        duration = d.get("duration_ms")
        ok = d.get("ok")
        return cls(
            name=str(d.get("name") or "?"),
            args=args if isinstance(args, dict) else None,
            ok=None if ok is None else bool(ok),
            error=d.get("error"),
            duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
            preview=d.get("preview"),
            turn=int(d.get("turn") or 1),
        )


@dataclass
class TurnRecord:
    """One turn of a multi-turn case: what the person said, what the agent replied."""
    prompt: str
    response: str = ""

    def to_dict(self) -> dict:
        return {"prompt": self.prompt, "response": self.response}

    @classmethod
    def from_dict(cls, d: dict) -> "TurnRecord":
        return cls(prompt=str(d.get("prompt") or ""), response=str(d.get("response") or ""))


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
    tools: list[ToolCallRecord] = field(default_factory=list)
    turns: list[TurnRecord] | None = None     # None for a single-prompt case
    run_as: str | None = None     # the account the case ran as
    prompt: str | None = None     # recorded input; never read from a later definition

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
            "tools": [t.to_dict() for t in self.tools],
            "turns": [t.to_dict() for t in self.turns] if self.turns is not None else None,
            "run_as": self.run_as,
            "prompt": self.prompt,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CaseResult":
        names = [str(n) for n in (d.get("tool_calls") or [])]
        if isinstance(d.get("tools"), list):
            tools = [ToolCallRecord.from_dict(t) for t in d["tools"] if isinstance(t, dict)]
        else:   # schema 1: names only
            tools = [ToolCallRecord(name=n) for n in names]
        turns = d.get("turns")
        return cls(
            name=d.get("name", "?"),
            tags=list(d.get("tags") or []),
            checks=[Check(kind=c.get("kind", "?"), passed=bool(c.get("passed")),
                          detail=c.get("detail", ""))
                    for c in (d.get("checks") or [])],
            judge=JudgeResult.from_dict(d["judge"]) if d.get("judge") else None,
            response=d.get("response", ""),
            tool_calls=names,
            duration=float(d.get("duration") or 0.0),
            error=d.get("error"),
            tools=tools,
            turns=([TurnRecord.from_dict(t) for t in turns if isinstance(t, dict)]
                   if isinstance(turns, list) else None),
            run_as=d.get("run_as"),
            prompt=d.get("prompt"),
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
    trigger: str | None = "cli"   # cli | schedule | console | ci (None: schema 1 file)
    run_as: str | None = None     # the suite default account, if one was given

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
            "trigger": self.trigger,
            "run_as": self.run_as,
            "passed": self.passed,
            "failed": self.failed,
            "cases": [c.to_dict() for c in self.cases],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SuiteResult":
        if d.get("schema", 1) not in (1, 2):
            raise ValueError("Unsupported eval result schema")
        return cls(
            hub=d.get("hub", "?"),
            cases=[CaseResult.from_dict(c) for c in (d.get("cases") or [])],
            started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at", ""),
            model=d.get("model", ""),
            judge_model=d.get("judge_model"),
            judged=bool(d.get("judged", True)),
            trigger=d.get("trigger"),
            run_as=d.get("run_as"),
        )


def now_iso() -> str:
    """Local time WITH its UTC offset, e.g. 2026-08-20T20:45:06+05:30.

    The offset is not decoration. Langfuse's ingestion API validates
    timestamps against a strict ISO-8601 pattern that requires `Z` or an
    offset, and rejects every event carrying a naive one. It also makes the
    JSON record unambiguous when a run is read on a box in another zone.
    """
    return datetime.now().astimezone().replace(microsecond=0).isoformat()
