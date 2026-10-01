"""Eval case declaration — `<hub>/evals/*.md`.

One markdown file per case, exactly like `skills/`, `knowledge/` and
`schedule/`. The filename stem is the case id; everything in frontmatter is
optional; the body carries the prompt and (optionally) the judging criteria.

    ---
    schedule: "0 6 * * 1"          # 5-field cron; absent = manual / CI only
    tags: [canary]
    expect_tools: [read_knowledge] # these tools MUST be called
    forbid_tools: [http_get]       # these tools MUST NOT be called
    contains: ["14 days"]          # substrings the reply must have
    not_contains: ["as an AI"]     # substrings the reply must not have
    timeout: 120                   # hard bound, seconds; exceeded = fail
    threshold: 7                   # judge pass mark out of 10
    enabled: true
    ---
    ## Prompt
    What is the refund window for a cancelled program?

    ## Criteria
    States 14 days. Cites the policy knowledge file.

Two deliberate design choices, both aimed at "minimum a hub author must
learn":

  * **The judge has no switch.** A case is judged if and only if it has a
    `## Criteria` section. Writing criteria *is* turning the judge on, so
    there is no `judge:` flag to understand or forget.
  * **There is no rules file.** The golden rules the judge grades against are
    the hub's own effective system prompt (`AGENTS.md` + addendum) — the spec
    is already written down, and restating it in a second file would give you
    two sources of truth that drift. Case-specific rules go in `## Criteria`.

A body with no `##` headings at all is taken as the prompt, so the smallest
possible case file is a single line of text.

Optional additions (1.1):

    run_as: someone@example.com    # run as this account (workflow identity
                                   # rules: a usable account, grants nothing)
    expect_tool_args:              # a call of the tool with these arguments
      read_knowledge: {name: refund-policy}

A multi-turn case uses `## Turn 1`, `## Turn 2`, ... (any heading starting
with "Turn") instead of `## Prompt`. The turns run as one conversation;
`contains` / `not_contains` and the judge look at the final reply.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import frontmatter
from .._fs import resolve_bucket
from ..scheduling import CronExpr, parse_cron

log = logging.getLogger("hubzoid.evals")

# Hard bound on one case's agent run. Generous enough for a multi-tool answer,
# short enough that a hung backend fails the suite instead of stalling CI.
DEFAULT_TIMEOUT = 120

# Judge pass mark, out of 10. 7 is "clearly acceptable" rather than "perfect":
# a rubric that demands 9+ turns every stylistic wobble into a red build.
DEFAULT_THRESHOLD = 7

# Section headings recognised in the body. Matched case-insensitively at any
# heading depth so `## Prompt`, `# prompt` and `### PROMPT` all work.
_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$", re.MULTILINE)

_PROMPT_KEYS = {"prompt", "input", "ask"}
# "Turn 1", "Turn 2: follow-up", "turn one". A whole word, so a heading such
# as "Turnaround" in an existing case is not mistaken for a turn.
_TURN_RE = re.compile(r"^turn\b")
_CRITERIA_KEYS = {"criteria", "rubric", "expect", "expected"}

# Frontmatter keys we understand. Anything else is a typo worth reporting —
# a silently-ignored `expected_tools:` (plural slip) would make a case look
# green while asserting nothing at all.
_KNOWN_KEYS = {
    "schedule", "tags", "expect_tools", "forbid_tools", "contains",
    "not_contains", "timeout", "threshold", "enabled",
    "run_as", "expect_tool_args",
}


class EvalCaseError(ValueError):
    """A case file that cannot be parsed. Message always names the file."""


@dataclass
class EvalCase:
    name: str                                  # filename stem = the case id
    prompt: str                                # the first turn's text for multi-turn
    criteria: str | None = None                # present => judged
    tags: list[str] = field(default_factory=list)
    expect_tools: list[str] = field(default_factory=list)
    forbid_tools: list[str] = field(default_factory=list)
    contains: list[str] = field(default_factory=list)
    not_contains: list[str] = field(default_factory=list)
    timeout: int = DEFAULT_TIMEOUT
    threshold: int = DEFAULT_THRESHOLD
    schedule: str | None = None                # raw cron string, if scheduled
    cron: CronExpr | None = None
    enabled: bool = True
    source_path: Path | None = None
    turns: list[str] = field(default_factory=list)       # empty: single prompt
    run_as: str | None = None                  # account email, validated syntax
    expect_tool_args: dict = field(default_factory=dict)  # tool -> {arg: expected}

    @property
    def is_multi_turn(self) -> bool:
        return bool(self.turns)

    @property
    def prompts(self) -> list[str]:
        """What the person says, in order: the turns, or the one prompt."""
        return list(self.turns) if self.turns else [self.prompt]

    @property
    def is_judged(self) -> bool:
        """Criteria present => the judge runs. There is no separate flag."""
        return bool((self.criteria or "").strip())

    @property
    def is_scheduled(self) -> bool:
        return self.cron is not None and self.enabled


def _as_list(value: Any, *, key: str, where: str) -> list[str]:
    """Accept a bare string or a list of strings; reject anything else.

    `contains: "14 days"` is what people write the first time. Treating it as
    a one-element list is friendlier than an error, and unambiguous.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if not isinstance(item, (str, int, float)):
                raise EvalCaseError(f"{where}: {key} entries must be strings")
            text = str(item)
            if text.strip():
                out.append(text)
        return out
    raise EvalCaseError(f"{where}: {key} must be a string or a list of strings")


def _as_int(value: Any, *, key: str, where: str, lo: int, hi: int, default: int) -> int:
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise EvalCaseError(f"{where}: {key} must be a whole number") from exc
    if not lo <= n <= hi:
        raise EvalCaseError(f"{where}: {key} must be between {lo} and {hi}, got {n}")
    return n


def _split_body(body: str, *, where: str = "case") -> tuple[str, str | None, list[str]]:
    """Return (prompt, criteria, turns) from a case body.

    With no headings the whole body is the prompt — the one-line case. With
    headings, text before the first recognised heading is ignored (it reads as
    a note to the human), and only `Prompt` / `Criteria` / `Turn ...` sections
    are used. For a multi-turn case `prompt` is the first turn's text.
    """
    headings = list(_HEADING_RE.finditer(body))
    if not headings:
        return body.strip(), None, []

    sections: dict[str, str] = {}
    turns: list[tuple[str, str]] = []
    for i, match in enumerate(headings):
        raw_title = match.group(1).strip()
        title = raw_title.lower().rstrip(":")
        start = match.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        text = body[start:end].strip()
        if title in _PROMPT_KEYS:
            sections.setdefault("prompt", text)
        elif title in _CRITERIA_KEYS:
            sections.setdefault("criteria", text)
        elif _TURN_RE.match(title):
            turns.append((raw_title, text))

    if turns:
        if "prompt" in sections:
            raise EvalCaseError(
                f"{where}: has both a '## Prompt' section and '## Turn' sections. "
                "Use '## Prompt' for one question, or '## Turn 1', '## Turn 2', ... "
                "for a conversation, not both.")
        empty = [title for title, text in turns if not text]
        if empty:
            raise EvalCaseError(f"{where}: '{empty[0]}' has no text")
        texts = [text for _, text in turns]
        return texts[0], sections.get("criteria"), texts

    if "prompt" not in sections:
        if "criteria" in sections:
            # A `## Criteria` section but no prompt. Falling back to "the whole
            # body is the prompt" here would quietly send the *rubric* to the
            # agent as the question, and the case would look like it ran. Make
            # the author fix it.
            return "", sections["criteria"], []
        # No recognised section at all — the author used markdown headings
        # inside a heading-free case. Take the whole body as the prompt rather
        # than failing over a formatting preference.
        return body.strip(), None, []
    return sections["prompt"], sections.get("criteria"), []


def _run_as(value: Any, *, where: str) -> str | None:
    """`run_as:` is an account email, checked for syntax here and resolved
    against the hub's accounts when the case runs (workflow identity rules)."""
    if value is None:
        return None
    from ..workflows.identity import validate_run_as

    try:
        return validate_run_as(value)
    except ValueError as exc:
        raise EvalCaseError(f"{where}: {exc}") from exc


def _expect_tool_args(value: Any, *, where: str) -> dict:
    """`expect_tool_args:` maps a tool name to the arguments one of its calls
    must have: {read_knowledge: {name: refund-policy}}."""
    if value is None:
        return {}
    shape = ("expect_tool_args must map a tool name to its expected arguments, "
             "for example {read_knowledge: {name: refund-policy}}")
    if not isinstance(value, dict):
        raise EvalCaseError(f"{where}: {shape}")
    out: dict[str, dict] = {}
    for tool, expected in value.items():
        if not isinstance(tool, str) or not tool.strip():
            raise EvalCaseError(f"{where}: {shape}")
        if not isinstance(expected, dict) or not expected:
            raise EvalCaseError(f"{where}: expect_tool_args.{tool}: {shape}")
        for arg in expected:
            if not isinstance(arg, str) or not arg.strip():
                raise EvalCaseError(f"{where}: expect_tool_args.{tool}: argument names must be text")
        out[tool.strip()] = dict(expected)
    return out


def parse(path: Path) -> EvalCase:
    """Parse one `<hub>/evals/<name>.md` into an EvalCase.

    Raises EvalCaseError (with the filename) on anything unusable. Callers
    decide whether one bad file fails the suite or is skipped with a warning.
    """
    where = path.name
    try:
        meta, body = frontmatter.read(path)
    except (OSError, ValueError) as exc:
        raise EvalCaseError(f"{where}: {exc}") from exc

    unknown = sorted(set(meta) - _KNOWN_KEYS)
    if unknown:
        raise EvalCaseError(
            f"{where}: unknown frontmatter key(s): {', '.join(unknown)}. "
            f"Known keys: {', '.join(sorted(_KNOWN_KEYS))}"
        )

    prompt, criteria, turns = _split_body(body, where=where)
    if not prompt.strip():
        raise EvalCaseError(f"{where}: no prompt — add a '## Prompt' section (or plain body text)")

    schedule_raw = meta.get("schedule")
    cron: CronExpr | None = None
    if schedule_raw is not None:
        try:
            cron = parse_cron(str(schedule_raw))
        except ValueError as exc:
            raise EvalCaseError(f"{where}: bad schedule — {exc}") from exc

    return EvalCase(
        name=path.stem,
        prompt=prompt,
        criteria=criteria,
        tags=_as_list(meta.get("tags"), key="tags", where=where),
        expect_tools=_as_list(meta.get("expect_tools"), key="expect_tools", where=where),
        forbid_tools=_as_list(meta.get("forbid_tools"), key="forbid_tools", where=where),
        contains=_as_list(meta.get("contains"), key="contains", where=where),
        not_contains=_as_list(meta.get("not_contains"), key="not_contains", where=where),
        timeout=_as_int(meta.get("timeout"), key="timeout", where=where,
                        lo=1, hi=3600, default=DEFAULT_TIMEOUT),
        threshold=_as_int(meta.get("threshold"), key="threshold", where=where,
                          lo=1, hi=10, default=DEFAULT_THRESHOLD),
        schedule=str(schedule_raw).strip() if schedule_raw is not None else None,
        cron=cron,
        enabled=bool(meta.get("enabled", True)),
        source_path=path,
        turns=turns,
        run_as=_run_as(meta.get("run_as"), where=where),
        expect_tool_args=_expect_tool_args(meta.get("expect_tool_args"), where=where),
    )


def discover(hub_dir: Path, *, strict: bool = True) -> list[EvalCase]:
    """Load every case in `<hub>/evals/`, sorted by name.

    `strict` (the default, used by the CLI) raises on the first unparseable
    file: a suite that silently drops a case reports a pass it never ran. The
    scheduler passes strict=False so one broken file cannot stop the other
    cases from firing — it logs and moves on.
    """
    root = resolve_bucket(hub_dir, "evals")
    if root is None:
        return []

    cases: list[EvalCase] = []
    for path in sorted(root.glob("*.md"), key=lambda p: p.name.lower()):
        if path.name.startswith(("_", ".")):
            continue                    # notes / drafts, not cases
        try:
            cases.append(parse(path))
        except EvalCaseError:
            if strict:
                raise
            log.warning("skipping unparseable eval case %s", path.name, exc_info=True)
    return cases


def select(
    cases: list[EvalCase],
    *,
    tag: str | None = None,
    case: str | None = None,
    include_disabled: bool = False,
) -> list[EvalCase]:
    """Filter a case list for `--tag` / `--case` (fnmatch glob on the name)."""
    from fnmatch import fnmatch

    out = list(cases)
    if not include_disabled:
        out = [c for c in out if c.enabled]
    if tag:
        out = [c for c in out if tag in c.tags]
    if case:
        out = [c for c in out if fnmatch(c.name, case)]
    return out
