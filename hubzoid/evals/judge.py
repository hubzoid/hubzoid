"""The model tier — grade a response against the hub's own instructions.

A case is judged if and only if it has a `## Criteria` section. The judge sees
three things:

  1. the hub's `AGENTS.md` — what this agent was told to be,
  2. the case's `## Criteria` — what this particular answer must do,
  3. the answer itself, with display chrome already stripped.

**Why there is no separate rules file.** The golden rules are already written
down in `AGENTS.md` (the Hubzoid Test Hub's "Behaviour rules", the IRS hub's
voice rules). Restating them in an `evals/RULES.md` would create a second
source of truth that drifts, and a stale rules file makes an eval pass for the
wrong reason — worse than having no eval.

**What the judge deliberately does NOT see:** the runtime addendum that
`factory._compose_instructions` appends. That is Hubzoid's own plumbing
guidance (how to call tools, how uploads work), not the hub's behavioural
spec. Including it would add a kilobyte of boilerplate to every judge call and
invite the judge to grade the framework instead of the hub.

**The judge is not an agent.** It is a plain, single-turn model call with no
tools and no hub persona. Building a second `Runtime` would hand the judge the
hub's own system prompt and toolset, so it would answer the question rather
than grade it.

**Pin the judge model in anything you track over time.** It defaults to the
hub's own model, which needs no configuration and is right for getting
started. But a model tends to rate its own output generously, and if the hub's
model changes the ruler moves with it — a score from March stops meaning what
a score from August means. Set `HUBZOID_EVAL_JUDGE_MODEL` and the ruler holds
still.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from .results import JudgeResult

log = logging.getLogger("hubzoid.evals")

JUDGE_MODEL_ENV = "HUBZOID_EVAL_JUDGE_MODEL"

# Guard against a pathological AGENTS.md dominating every judge call. Hubs this
# large are rare; when one shows up the tail is dropped rather than the head,
# because behavioural rules are conventionally stated early.
_MAX_SPEC_CHARS = 20_000

# The graded answer is capped too — a runaway response should cost a bounded
# judge call, not an unbounded one.
_MAX_RESPONSE_CHARS = 20_000

_SYSTEM = """\
You are grading a single response produced by an AI agent. You are not the \
agent and you must not answer its question.

You will be given the agent's own instructions, the question it was asked, the \
criteria this particular answer must satisfy, and the answer it gave.

Score the answer from 1 to 10:
  1-3  fails the criteria, or contradicts the agent's instructions
  4-6  partially satisfies the criteria, or complies only loosely
  7-8  satisfies the criteria and follows the instructions
  9-10 satisfies the criteria fully and follows the instructions precisely

Grade only what the criteria and instructions actually require. Do not deduct \
for style, length, or formatting that neither document asks for. Judge the \
substance of the answer, not its confidence.

Reply with JSON only, no prose and no code fence:
{"score": <1-10>, "reasoning": "<one or two sentences>"}\
"""


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[...truncated for judging]"


def hub_spec(hub_dir: Path) -> str:
    """The hub's `AGENTS.md` body — the rules the judge grades against.

    A hub with no readable AGENTS.md still judges, on the case criteria alone:
    a missing spec should weaken the judge, not break the suite.
    """
    try:
        from ..loaders import agents as agents_loader
        return _clip(agents_loader.load_main(hub_dir).instructions, _MAX_SPEC_CHARS)
    except Exception as exc:  # noqa: BLE001
        log.warning("judging without a hub spec (%s): %s", hub_dir.name, exc)
        return ""


def build_prompt(spec: str, case, response: str) -> str:
    """The judge's user message. Delimited sections, so a prompt-injection
    attempt inside the *response* reads as data rather than instruction."""
    parts = []
    if spec.strip():
        parts.append(f"<agent_instructions>\n{spec.strip()}\n</agent_instructions>")
    parts.append(f"<question>\n{case.prompt.strip()}\n</question>")
    parts.append(f"<criteria>\n{(case.criteria or '').strip()}\n</criteria>")
    parts.append(f"<answer>\n{_clip(response, _MAX_RESPONSE_CHARS).strip()}\n</answer>")
    parts.append('Grade the answer. Reply with JSON only: {"score": <1-10>, "reasoning": "..."}')
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Parsing the verdict
# --------------------------------------------------------------------------
_JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)
_SCORE_RE = re.compile(r'"?score"?\s*[:=]\s*(\d{1,2})')


def parse_verdict(text: str) -> tuple[int | None, str]:
    """Extract (score, reasoning) from the judge's reply.

    Lenient by design: a judge that wraps its JSON in a code fence or adds a
    sentence of preamble is still a usable verdict, and re-running the case to
    punish formatting would cost real money. Returns (None, raw) when no score
    can be found at all — the caller records that as a judge error, which is
    reported distinctly from the case failing.
    """
    if not text:
        return None, ""
    for match in _JSON_RE.finditer(text):
        try:
            data = json.loads(match.group(0))
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and "score" in data:
            try:
                score = int(data["score"])
            except (TypeError, ValueError):
                continue
            return max(1, min(10, score)), str(data.get("reasoning", "")).strip()

    fallback = _SCORE_RE.search(text)
    if fallback:
        return max(1, min(10, int(fallback.group(1)))), text.strip()[:400]
    return None, text.strip()[:400]


# --------------------------------------------------------------------------
# The model call
# --------------------------------------------------------------------------
def resolve_model(hub_dir: Path, override: str | None = None) -> str:
    """Judge model: `--judge-model` > HUBZOID_EVAL_JUDGE_MODEL > the hub's own."""
    explicit = (override or os.environ.get(JUDGE_MODEL_ENV) or "").strip()
    if explicit:
        return explicit
    from .. import runtime as runtime_lib
    from .. import settings as settingslib
    return runtime_lib._resolve_model_id(hub_dir, settingslib.load(hub_dir))


async def _ask_claude_local(model_id: str, prompt: str) -> str:
    """Single-turn Claude call with no tools, via the bundled `claude` login."""
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

    from ..factory_claude import _parse_model_pin

    opts: dict = {
        "system_prompt": _SYSTEM,
        "allowed_tools": [],       # a judge has no business calling anything
        "max_turns": 1,
    }
    pin = _parse_model_pin(model_id)
    if pin:
        opts["model"] = pin

    chunks: list[str] = []
    final = ""
    async for message in query(prompt=prompt, options=ClaudeAgentOptions(**opts)):
        if isinstance(message, AssistantMessage):
            for block in getattr(message, "content", []) or []:
                text = getattr(block, "text", None)
                if text:
                    chunks.append(text)
        elif isinstance(message, ResultMessage):
            final = getattr(message, "result", None) or ""
    return "".join(chunks) or final


async def _ask_litellm(model_id: str, prompt: str) -> str:
    """Single-turn call for any OpenAI/LiteLLM-routed model."""
    import litellm

    resp = await litellm.acompletion(
        model=model_id,
        messages=[{"role": "system", "content": _SYSTEM},
                  {"role": "user", "content": prompt}],
        temperature=0,          # a ruler should not wobble between runs
    )
    return (resp.choices[0].message.content or "").strip()


async def ask_model(model_id: str, prompt: str) -> str:
    """Route to the right backend for a bare, tool-less model call."""
    if model_id.lower().startswith("claude-local"):
        return await _ask_claude_local(model_id, prompt)
    return await _ask_litellm(model_id, prompt)


def make_judge(hub_dir: Path, *, model: str | None = None, ask=ask_model):
    """Build the `judge_fn(case, response) -> JudgeResult` the runner injects.

    `ask` is swappable so the tests can exercise prompt assembly, verdict
    parsing and error handling without a model or a network.
    """
    model_id = resolve_model(hub_dir, model)
    spec = hub_spec(hub_dir)

    async def judge_fn(case, response: str) -> JudgeResult:
        prompt = build_prompt(spec, case, response)
        try:
            reply = await ask(model_id, prompt)
        except Exception as exc:  # noqa: BLE001 — a broken judge is not a failed hub
            log.warning("judge call failed for %s: %s", case.name, exc)
            return JudgeResult(score=0, threshold=case.threshold, model=model_id,
                               error=f"{type(exc).__name__}: {exc}")

        score, reasoning = parse_verdict(reply)
        if score is None:
            return JudgeResult(score=0, threshold=case.threshold, model=model_id,
                               error="could not parse a score from the judge reply",
                               reasoning=reasoning)
        return JudgeResult(score=score, threshold=case.threshold,
                           reasoning=reasoning, model=model_id)

    return judge_fn


def describe(hub_dir: Path, model: str | None = None) -> str:
    """One line for the CLI header: which model is holding the ruler."""
    model_id = resolve_model(hub_dir, model)
    pinned = bool((model or os.environ.get(JUDGE_MODEL_ENV) or "").strip())
    return model_id + ("" if pinned else " (hub default — pin it for stable scores)")
