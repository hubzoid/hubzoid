# Model-triggered agent delegation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a sub-agent under `<hub>/agents/<name>/` declares a `model:` that differs from the hub's model (same engine), the main agent can delegate a sub-task to it running on that model, get the result, and continue — on both the OpenAI Agents SDK and Claude Agent SDK backends.

**Architecture:** A sub-agent is classified at load time as a **skill** (today's inline promotion — unchanged default) or a **delegate**. A delegate is wired as a native within-turn subagent the main agent *calls* and whose result returns to it (delegation, not control-transfer): `Agent.as_tool` on the OpenAI backend; a native `AgentDefinition` + the `Agent` spawn tool on the Claude backend. A shared `hubzoid/handover.py` owns classification so both factories agree.

**Tech Stack:** Python 3.12, `openai-agents` 0.17.3 (`agents`), `claude-agent-sdk` 0.2.87, LiteLLM via `agents.extensions.models.litellm_model`, pytest.

## Global Constraints

- Interpreter for ALL commands: `/Users/shreyarao/Desktop/WaveAssist/waveAssistEnv/bin/python3` (system python lacks deps). Referenced below as `$PY`.
- No new dependencies — both SDKs are already installed.
- **Same-engine only:** a delegate's `model:` must resolve to the same engine as the hub (`engine(m)` = `"claude"` if `m.lower().startswith("claude-local")` else `"litellm"`). Cross-engine → falls back to skill + warning.
- **Delegation, not handover:** the main agent keeps control; the subagent runs and returns its final message as the tool result.
- **Backward compatible:** no `model:`, `model:` equal to the hub model, or cross-engine → sub-agent stays a skill (exactly today's behavior). Hubs with zero delegates must produce byte-identical agent/runtime wiring to today (esp. Claude `tools == []`).
- Claude subagent-spawn tool name is **`Agent`** (confirmed live on the bundled `claude` CLI v2.1.198; renamed from `Task` at v2.1.63).
- Run one task at a time; commit at the end of each task; keep the full suite green (`$PY -m pytest -q`, excluding `-m e2e`).

---

## File Structure

- **Create** `hubzoid/handover.py` — pure functions: engine detection, model normalization, tier resolution, skill/delegate classification, delegate tool-name + tool-scoping. No I/O, no SDK imports.
- **Modify** `hubzoid/loaders/agents.py` — factor out `to_skill`; add `split_subagents`; stop discarding `model`/`tools` for delegates (skills still ignore `tools`).
- **Modify** `hubzoid/factory.py` — `HubContext.delegates`; `_load_skills_and_delegates`; build `as_tool` delegates with graceful key fallback; keep `_load_skills_and_promoted_agents` as a thin wrapper.
- **Modify** `hubzoid/factory_claude.py` — build `AgentDefinition`s; enable ONLY the `Agent` spawn tool when delegates exist; `HubContext.delegates`.
- **Modify** `hubzoid/system_addendum.py` — "Delegate agents available" section + faithful-relay instruction.
- **Create** `tests/test_handover.py`, `tests/fixtures/delegate_claude_hub/…`, `tests/e2e/test_delegate_e2e.py`.
- **Modify** `tests/test_loaders.py`, `tests/test_factory.py`, `tests/test_factory_claude_tool_gating.py`, `tests/test_system_addendum.py`.
- **Modify** `docs/authoring-a-hub.md`, `demo-hub/agents/builder/AGENTS.md` (guidance only).

---

## Task 1: `hubzoid/handover.py` — classification core

**Files:**
- Create: `hubzoid/handover.py`
- Test: `tests/test_handover.py`

**Interfaces:**
- Produces:
  - `engine(model_id: str | None) -> str` → `"claude"` | `"litellm"`
  - `resolve_tier(model_id: str | None) -> str` → claude tier, e.g. `"opus"`, bare `claude-local` → `"sonnet"`
  - `norm(model_id: str | None) -> str` → canonical identity string for equality within an engine
  - `classify(sub_model: str | None, hub_model: str | None) -> str` → `"skill"` | `"delegate"`
  - `tool_name(agent_name: str) -> str` → `"handover_<slug>"`
  - `scoped_tool_names(whitelist: list[str], available: list[str]) -> list[str]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_handover.py`:

```python
from __future__ import annotations

import logging

import pytest

from hubzoid import handover


class TestEngine:
    @pytest.mark.parametrize("model,expected", [
        ("claude-local", "claude"),
        ("claude-local/opus", "claude"),
        ("CLAUDE-LOCAL/haiku", "claude"),
        ("openai/gpt-4o", "litellm"),
        ("openrouter/anthropic/claude-3-opus", "litellm"),
        ("", "litellm"),
        (None, "litellm"),
    ])
    def test_engine(self, model, expected):
        assert handover.engine(model) == expected


class TestResolveTier:
    @pytest.mark.parametrize("model,tier", [
        ("claude-local", "sonnet"),
        ("claude-local/sonnet", "sonnet"),
        ("claude-local/opus", "opus"),
        ("claude-local/haiku", "haiku"),
        ("claude-local/claude-opus-4-7", "claude-opus-4-7"),
        ("claude-local   ", "sonnet"),
    ])
    def test_resolve_tier(self, model, tier):
        assert handover.resolve_tier(model) == tier


class TestClassify:
    def test_no_model_is_skill(self):
        assert handover.classify(None, "claude-local") == "skill"
        assert handover.classify("", "openai/gpt-4o") == "skill"

    def test_same_claude_tier_is_skill(self):
        # bare claude-local == claude-local/sonnet
        assert handover.classify("claude-local", "claude-local") == "skill"
        assert handover.classify("claude-local/sonnet", "claude-local") == "skill"

    def test_different_claude_tier_is_delegate(self):
        assert handover.classify("claude-local/opus", "claude-local") == "delegate"
        assert handover.classify("claude-local/haiku", "claude-local/opus") == "delegate"

    def test_cross_engine_is_skill(self):
        # gpt sub inside a claude hub, and vice versa
        assert handover.classify("openai/gpt-4o", "claude-local") == "skill"
        assert handover.classify("claude-local/opus", "openai/gpt-4o") == "skill"

    def test_same_litellm_model_is_skill(self):
        assert handover.classify("openai/gpt-4o", "openai/gpt-4o") == "skill"

    def test_different_litellm_model_is_delegate(self):
        assert handover.classify(
            "openrouter/anthropic/claude-3-opus",
            "openrouter/anthropic/claude-haiku-4.5",
        ) == "delegate"

    def test_none_hub_model_is_skill(self):
        # hub_model unknown -> never delegate (old behavior)
        assert handover.classify("claude-local/opus", None) == "skill"


class TestToolName:
    def test_slug(self):
        assert handover.tool_name("Deep Researcher") == "handover_deep_researcher"
        assert handover.tool_name("opus-helper") == "handover_opus_helper"


class TestScopedToolNames:
    def test_whitelist_intersects_available(self, caplog):
        with caplog.at_level(logging.WARNING, logger="hubzoid.handover"):
            out = handover.scoped_tool_names(
                ["read_file", "does_not_exist"], ["read_file", "write_artifact"]
            )
        assert out == ["read_file"]
        assert any("unknown" in r.message for r in caplog.records)

    def test_empty_whitelist_returns_all_available(self):
        out = handover.scoped_tool_names([], ["read_file", "write_artifact"])
        assert out == ["read_file", "write_artifact"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `$PY -m pytest tests/test_handover.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hubzoid.handover'`.

- [ ] **Step 3: Write minimal implementation**

Create `hubzoid/handover.py`:

```python
"""Classify agents/ sub-agents into skills vs model-delegates.

Pure functions, no I/O, no SDK imports. Both factories call these so the
skill-vs-delegate decision never drifts between backends.

A sub-agent becomes a *delegate* (runs on its own model, called by the main
agent as a within-turn subagent) only when its `model:` resolves to the SAME
engine as the hub AND differs from the hub's model. Otherwise it stays a
skill (loaded inline by the main agent) — the backward-compatible default.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("hubzoid.handover")

_CLAUDE_PREFIX = "claude-local"
_CLAUDE_DEFAULT_TIER = "sonnet"  # mirror factory_claude._CLAUDE_LOCAL_DEFAULT


def engine(model_id: str | None) -> str:
    """Which runtime engine a model id belongs to."""
    return "claude" if (model_id or "").strip().lower().startswith(_CLAUDE_PREFIX) \
        else "litellm"


def resolve_tier(model_id: str | None) -> str:
    """The Claude tier pin for a claude-local id. Bare claude-local -> sonnet."""
    s = (model_id or "").strip()
    if "/" not in s:
        return _CLAUDE_DEFAULT_TIER
    suffix = s.split("/", 1)[1].strip()
    return suffix or _CLAUDE_DEFAULT_TIER


def norm(model_id: str | None) -> str:
    """Canonical identity used for equality comparison within one engine."""
    if engine(model_id) == "claude":
        return f"claude::{resolve_tier(model_id)}"
    return f"litellm::{(model_id or '').strip()}"


def classify(sub_model: str | None, hub_model: str | None) -> str:
    """Return 'delegate' or 'skill' for a sub-agent given the hub's model."""
    if not (sub_model or "").strip():
        return "skill"
    if not (hub_model or "").strip():
        return "skill"
    if engine(sub_model) != engine(hub_model):
        return "skill"
    if norm(sub_model) == norm(hub_model):
        return "skill"
    return "delegate"


def tool_name(agent_name: str) -> str:
    """The tool name the main agent uses to call a delegate."""
    slug = re.sub(r"[^a-z0-9]+", "_", (agent_name or "").strip().lower()).strip("_")
    return f"handover_{slug or 'agent'}"


def scoped_tool_names(whitelist: list[str], available: list[str]) -> list[str]:
    """Tool names a delegate may use.

    whitelist present -> intersection with `available` (unknown names dropped
    with a warning). whitelist empty -> all `available` (the recursion guard is
    automatic: delegate tools are never in `available`).
    """
    if not whitelist:
        return list(available)
    avail = set(available)
    allowed = [n for n in whitelist if n in avail]
    unknown = [n for n in whitelist if n not in avail]
    if unknown:
        log.warning("delegate tool whitelist has unknown tools (dropped): %s", unknown)
    return allowed
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `$PY -m pytest tests/test_handover.py -q`
Expected: PASS (all parametrized cases).

- [ ] **Step 5: Commit**

```bash
git add hubzoid/handover.py tests/test_handover.py
git commit -m "feat: handover classification core (skill vs model-delegate)"
```

---

## Task 2: `loaders/agents.py` — split subagents into skills vs delegates

**Files:**
- Modify: `hubzoid/loaders/agents.py`
- Test: `tests/test_loaders.py`

**Interfaces:**
- Consumes: `handover.classify` (Task 1), existing `load_subagents`, `LoadedAgent`, `skills.LoadedSkill`/`SkillSpec`.
- Produces:
  - `to_skill(loaded: LoadedAgent) -> LoadedSkill` (warns if `loaded.spec.tools` set)
  - `promote_to_skills(hub_dir) -> list[LoadedSkill]` (unchanged behavior: every sub-agent → skill)
  - `split_subagents(hub_dir, hub_model: str | None) -> tuple[list[LoadedAgent], list[LoadedAgent]]` → `(skill_agents, delegate_agents)`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_loaders.py`:

```python
def test_split_subagents_no_model_is_skill():
    # minimal_hub's `echo` sub-agent has no model -> skill bucket, empty delegates.
    from hubzoid.loaders import agents as agents_loader
    skills, delegates = agents_loader.split_subagents(MINIMAL, "claude-local")
    assert [a.spec.name for a in skills] == ["echo"]
    assert delegates == []


def test_split_subagents_different_tier_is_delegate(tmp_path):
    from hubzoid.loaders import agents as agents_loader
    (tmp_path / "AGENTS.md").write_text("---\nname: m\ndescription: d\n---\nbody")
    sub = tmp_path / "agents" / "opusguy"
    sub.mkdir(parents=True)
    (sub / "AGENTS.md").write_text(
        "---\nname: opusguy\ndescription: hard stuff\nmodel: claude-local/opus\n"
        "tools: [read_file]\n---\nYou are the opus specialist."
    )
    skills, delegates = agents_loader.split_subagents(tmp_path, "claude-local")
    assert skills == []
    assert [a.spec.name for a in delegates] == ["opusguy"]
    assert delegates[0].spec.model == "claude-local/opus"
    assert delegates[0].spec.tools == ["read_file"]


def test_split_subagents_none_hub_model_all_skills(tmp_path):
    from hubzoid.loaders import agents as agents_loader
    (tmp_path / "AGENTS.md").write_text("---\nname: m\ndescription: d\n---\nbody")
    sub = tmp_path / "agents" / "opusguy"
    sub.mkdir(parents=True)
    (sub / "AGENTS.md").write_text(
        "---\nname: opusguy\ndescription: d\nmodel: claude-local/opus\n---\nbody"
    )
    skills, delegates = agents_loader.split_subagents(tmp_path, None)
    assert [a.spec.name for a in skills] == ["opusguy"]
    assert delegates == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `$PY -m pytest tests/test_loaders.py -q -k split_subagents`
Expected: FAIL — `AttributeError: module ... has no attribute 'split_subagents'`.

- [ ] **Step 3: Write minimal implementation**

In `hubzoid/loaders/agents.py`, replace the body of `promote_to_skills` with a call to a new factored `to_skill`, and add `split_subagents`. Concretely, replace the current `promote_to_skills` function with:

```python
def to_skill(loaded: "LoadedAgent"):
    """Convert a sub-agent into a LoadedSkill (loaded inline by the main agent).

    A `tools:` whitelist is meaningless for a skill (the main agent owns the
    whole registry), so it is dropped with a warning — mirrors the historical
    behavior for promoted agents.
    """
    import logging

    from .skills import LoadedSkill, SkillSpec

    log = logging.getLogger("hubzoid.loaders.agents")
    if loaded.spec.tools:
        log.warning(
            "%s: tools whitelist %r is ignored — this agent is loaded as a "
            "skill; the main agent owns all tools.",
            loaded.source_path, loaded.spec.tools,
        )
    spec = SkillSpec(name=loaded.spec.name, description=loaded.spec.description)
    return LoadedSkill(spec=spec, body=loaded.instructions,
                       source_path=loaded.source_path)


def promote_to_skills(hub_dir: Path):
    """Load every <hub>/agents/<name> as a LoadedSkill (all sub-agents).

    Kept for callers that want the flat all-skills view. Delegate-aware
    loading goes through `split_subagents`.
    """
    return [to_skill(loaded) for loaded in load_subagents(hub_dir)]


def split_subagents(hub_dir: Path, hub_model: str | None):
    """Partition sub-agents into (skill_agents, delegate_agents) LoadedAgents.

    A sub-agent whose `model:` differs from `hub_model` on the same engine is a
    delegate; everything else is a skill. `hub_model=None` -> all skills.
    """
    from .. import handover

    skill_agents: list[LoadedAgent] = []
    delegate_agents: list[LoadedAgent] = []
    for loaded in load_subagents(hub_dir):
        if handover.classify(loaded.spec.model, hub_model) == "delegate":
            delegate_agents.append(loaded)
        else:
            skill_agents.append(loaded)
    return skill_agents, delegate_agents
```

(Keep the module docstring; only the `promote_to_skills` function is rewritten and two functions are added.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `$PY -m pytest tests/test_loaders.py tests/test_handover.py -q`
Expected: PASS (including the pre-existing `test_load_subagents`).

- [ ] **Step 5: Commit**

```bash
git add hubzoid/loaders/agents.py tests/test_loaders.py
git commit -m "feat: split_subagents classifies delegates vs skills"
```

---

## Task 3: OpenAI backend — wire delegates via `as_tool`

**Files:**
- Modify: `hubzoid/factory.py`
- Test: `tests/test_factory.py`

**Interfaces:**
- Consumes: `handover.tool_name`, `handover.scoped_tool_names`, `handover.classify`; `split_subagents` (Task 2); `modellib.build`, `modellib.MissingProviderKey`.
- Produces:
  - `HubContext.delegates: list` (list of `LoadedAgent`)
  - `_load_skills_and_delegates(hub_dir, hub_model) -> tuple[list, list]`
  - `_load_skills_and_promoted_agents(hub_dir) -> list` (wrapper = skills bucket with `hub_model=None`)
  - Main `Agent` gains one FunctionTool named `handover_<slug>` per wired delegate.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_factory.py`:

```python
def _write_hub_with_delegate(tmp_path, sub_model):
    (tmp_path / "AGENTS.md").write_text("---\nname: m\ndescription: d\n---\nbody")
    sub = tmp_path / "agents" / "opusguy"
    sub.mkdir(parents=True)
    (sub / "AGENTS.md").write_text(
        f"---\nname: opusguy\ndescription: hard questions\nmodel: {sub_model}\n"
        f"tools: [read_file]\n---\nYou are the specialist."
    )
    return tmp_path


def test_delegate_becomes_handover_tool(tmp_path, monkeypatch):
    # hub on haiku, delegate on a DIFFERENT same-engine model -> a handover tool.
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    hub = _write_hub_with_delegate(tmp_path, "openrouter/anthropic/claude-3-opus")

    from hubzoid.factory import build_agent
    agent = build_agent(hub)
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert "handover_opusguy" in tool_names
    # It must NOT also be promoted to a skill.
    assert not agent.handoffs


def test_delegate_missing_key_falls_back_to_skill(tmp_path, monkeypatch, caplog):
    # delegate model needs OPENAI_API_KEY which is absent -> skill fallback.
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    hub = _write_hub_with_delegate(tmp_path, "openai/gpt-4o")  # cross-provider, same engine

    import logging
    from hubzoid.factory import build_agent
    with caplog.at_level(logging.WARNING, logger="hubzoid"):
        agent = build_agent(hub)
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert "handover_opusguy" not in tool_names
    assert any("opusguy" in r.message for r in caplog.records)


def test_no_delegates_leaves_tool_list_unchanged(monkeypatch):
    # Regression: minimal_hub (echo has no model) has NO handover tools.
    from hubzoid.factory import build_agent
    agent = build_agent(MINIMAL)
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert not any(n.startswith("handover_") for n in tool_names)
```

Also update the two existing tests that call the renamed helper — change `_load_skills_and_promoted_agents(MINIMAL)` and `_load_skills_and_promoted_agents(tmp_path)` calls to keep working (the wrapper below preserves them, so **no edit is required** if the wrapper is added; verify in Step 4).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PY -m pytest tests/test_factory.py -q -k "delegate or unchanged"`
Expected: FAIL — `handover_opusguy` not found (delegates not wired yet).

- [ ] **Step 3: Write the implementation**

In `hubzoid/factory.py`:

(a) Add the `delegates` field to `HubContext`:

```python
@dataclass
class HubContext:
    hub_dir: Path
    output_dir: Path
    session_id: str
    settings: "settingslib.Settings"
    skills: list = field(default_factory=list)
    knowledge: list = field(default_factory=list)
    delegates: list = field(default_factory=list)
```

(b) Replace `_load_skills_and_promoted_agents` with a delegate-aware core plus a back-compat wrapper:

```python
def _load_skills_and_delegates(hub_dir: Path, hub_model: str | None):
    """Return (skills, delegate_agents).

    skills = real skills/ + skill-classified sub-agents (real wins on name
    collision, with a warning). delegate_agents = LoadedAgent objects whose
    model differs from the hub on the same engine.
    """
    real = skills_loader.load_all(hub_dir)
    skill_agents, delegate_agents = agents_loader.split_subagents(hub_dir, hub_model)
    by_name: dict[str, object] = {s.spec.name: s for s in real}
    for loaded in skill_agents:
        s = agents_loader.to_skill(loaded)
        if s.spec.name in by_name:
            log.warning(
                "skill name collision: %r exists in both skills/ and agents/. "
                "skills/ wins (%s).",
                s.spec.name, by_name[s.spec.name].source_path,
            )
            continue
        by_name[s.spec.name] = s
    return list(by_name.values()), delegate_agents


def _load_skills_and_promoted_agents(hub_dir: Path) -> list:
    """Back-compat: skills view with no delegate detection (all agents -> skills)."""
    return _load_skills_and_delegates(hub_dir, None)[0]
```

(c) Add a delegate-building helper (placed after `_compose_instructions`):

```python
def _prepare_delegates(delegate_agents: list):
    """Build each delegate's model up front. Returns (kept, fallbacks).

    kept = [(LoadedAgent, LitellmModel)]; fallbacks = LoadedAgents whose model
    could not be built (e.g. missing provider key) — caller demotes them to
    skills so the hub still boots.
    """
    kept, fallbacks = [], []
    for loaded in delegate_agents:
        try:
            m = modellib.build(loaded.spec.model)
        except modellib.MissingProviderKey as exc:
            log.warning("delegate %r cannot run (%s); loading it as a skill instead.",
                        loaded.spec.name, exc)
            fallbacks.append(loaded)
        else:
            kept.append((loaded, m))
    return kept, fallbacks


def _build_delegate_tools(kept: list, registry: dict):
    """Wrap each kept delegate as an Agent.as_tool FunctionTool."""
    from . import handover

    tools = []
    reg_names = list(registry.keys())
    for loaded, model in kept:
        scoped = handover.scoped_tool_names(loaded.spec.tools, reg_names)
        sub = Agent(
            name=loaded.spec.name,
            instructions=loaded.instructions,
            model=model,
            tools=[registry[n] for n in scoped],
        )
        tools.append(sub.as_tool(
            tool_name=handover.tool_name(loaded.spec.name),
            tool_description=loaded.spec.description,
        ))
    return tools
```

(d) Rewire `build_agent`. Load the main spec and hub model FIRST, split skills/delegates with that model, prepare delegates (demoting fallbacks to skills), then build the registry and delegate tools. Replace the section from `skills = _load_skills_and_promoted_agents(hub_dir)` down to the `main = Agent(...)` construction with:

```python
    settings = settingslib.load(hub_dir)
    session_id = memlib.make_session_id()
    output_dir = memlib.session_output_dir(hub_dir, session_id)

    main_spec = agents_loader.load_main(hub_dir)
    main_model_id = settings.model or main_spec.spec.model
    if not main_model_id:
        raise RuntimeError(
            "no model configured. Set MODEL in <hub>/.env or `model:` in AGENTS.md frontmatter."
        )

    skills, delegate_agents = _load_skills_and_delegates(hub_dir, main_model_id)
    kept, fallbacks = _prepare_delegates(delegate_agents)
    for loaded in fallbacks:
        skills.append(agents_loader.to_skill(loaded))
    knowledge = knowledge_loader.load_all(hub_dir)
    log.info(
        "hub %s: %d skill(s), %d delegate(s), %d knowledge doc(s)",
        hub_dir.name, len(skills), len(kept), len(knowledge),
    )

    ctx = HubContext(
        hub_dir=hub_dir,
        output_dir=output_dir,
        session_id=session_id,
        settings=settings,
        skills=skills,
        knowledge=knowledge,
        delegates=[loaded for loaded, _ in kept],
    )

    builtin: dict[str, FunctionTool] = make_builtin_tools(ctx)
    local: dict[str, FunctionTool] = tools_local_loader.load_all(hub_dir)
    overlap = set(builtin) & set(local)
    if overlap:
        log.info("hub-local tools override built-ins: %s", sorted(overlap))
    registry: dict[str, FunctionTool] = {**builtin, **local, **(extra_tools or {})}

    from . import access
    registry = access.apply(hub_dir, registry)

    mcp_servers = mcp_loader.load_all(hub_dir)

    delegate_tools = _build_delegate_tools(kept, registry)

    main_model = modellib.build(main_model_id)
    instructions = _compose_instructions(main_spec.instructions, ctx, backend="openai-agents")

    extra: dict = {}
    if settings.reasoning_effort:
        from agents import ModelSettings
        from openai.types.shared import Reasoning
        extra["model_settings"] = ModelSettings(
            reasoning=Reasoning(effort=settings.reasoning_effort)
        )

    main = Agent(
        name=main_spec.spec.name,
        instructions=instructions,
        model=main_model,
        tools=list(registry.values()) + delegate_tools,
        mcp_servers=mcp_servers,
        **extra,
    )
    return main
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `$PY -m pytest tests/test_factory.py -q`
Expected: PASS — new delegate tests pass AND all pre-existing factory tests (including `test_agents_folder_promoted_to_skills`, `test_real_skill_wins_over_promoted_agent_on_name_conflict` via the wrapper) still pass.

- [ ] **Step 5: Commit**

```bash
git add hubzoid/factory.py tests/test_factory.py
git commit -m "feat: OpenAI backend wires model-delegates via as_tool"
```

---

## Task 4: Claude backend — wire delegates via native `AgentDefinition`

**Files:**
- Modify: `hubzoid/factory_claude.py`
- Test: `tests/test_factory_claude_tool_gating.py`

**Interfaces:**
- Consumes: `_load_skills_and_delegates` (Task 3), `handover.resolve_tier`, `handover.scoped_tool_names`.
- Produces: `ClaudeAgentOptions.agents` populated with one `AgentDefinition` per delegate; `tools == [SUBAGENT_SPAWN_TOOL]` and `SUBAGENT_SPAWN_TOOL` in `allowed_tools` **only when delegates exist**; `HubContext.delegates` set.
- Module constant: `SUBAGENT_SPAWN_TOOL = "Agent"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/fixtures/delegate_claude_hub/AGENTS.md`:

```
---
name: router
description: Routes hard questions to an Opus specialist.
---

You are Router. For hard analytical questions, delegate to the opus-helper
subagent via its handover tool and relay its answer faithfully.
```

Create `tests/fixtures/delegate_claude_hub/agents/opus-helper/AGENTS.md`:

```
---
name: opus-helper
description: Deep analytical specialist. Use for hard reasoning questions.
model: claude-local/opus
tools: [read_knowledge]
---

You are the Opus specialist. Answer the delegated question thoroughly.
```

Append to `tests/test_factory_claude_tool_gating.py`:

```python
DELEGATE_HUB = FIXTURES / "delegate_claude_hub"


def test_no_delegates_keeps_tools_empty():
    """Regression: a hub with no delegates keeps the tools=[] gate exactly."""
    runtime = _build()  # MINIMAL, echo has no model
    assert runtime._options.tools == []
    assert not runtime._options.agents


def test_delegate_hub_enables_only_the_agent_spawn_tool(monkeypatch):
    monkeypatch.setenv("MODEL", "claude-local")  # hub = sonnet default
    from hubzoid.factory_claude import build_claude_runtime, SUBAGENT_SPAWN_TOOL
    runtime = build_claude_runtime(DELEGATE_HUB)
    opts = runtime._options
    # Only the spawn tool is enabled — Bash/Read/etc. stay off.
    assert opts.tools == [SUBAGENT_SPAWN_TOOL]
    assert SUBAGENT_SPAWN_TOOL in opts.allowed_tools
    # The delegate is registered as a native subagent on its own tier.
    assert "opus-helper" in opts.agents
    assert opts.agents["opus-helper"].model == "opus"
    # Its tool scope is the hubzoid MCP form of its whitelist.
    assert opts.agents["opus-helper"].tools == ["mcp__hubzoid__read_knowledge"]
    # hubzoid MCP tools still present in allowed_tools.
    assert any(t.startswith("mcp__hubzoid__") for t in opts.allowed_tools)


def test_delegate_hub_still_blocks_dangerous_builtins(monkeypatch):
    """Enabling Agent must NOT re-admit Bash/Read/Edit/Write etc."""
    monkeypatch.setenv("MODEL", "claude-local")
    from hubzoid.factory_claude import build_claude_runtime
    runtime = build_claude_runtime(DELEGATE_HUB)
    forbidden = {"Bash", "Read", "Edit", "Write", "WebFetch", "Grep", "Glob"}
    assert not (forbidden & set(runtime._options.tools))
    assert not (forbidden & set(runtime._options.allowed_tools))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PY -m pytest tests/test_factory_claude_tool_gating.py -q -k delegate`
Expected: FAIL — `ImportError: cannot import name 'SUBAGENT_SPAWN_TOOL'` / `opts.agents` empty.

- [ ] **Step 3: Write the implementation**

In `hubzoid/factory_claude.py`:

(a) Add the module constant near the top (after `_MCP_NAMESPACE`):

```python
# The Claude CLI's subagent-spawn tool. Renamed Task -> Agent at CLI v2.1.63;
# the bundled CLI is 2.x. Enabling ONLY this (not the claude_code preset) lets
# the model dispatch our AgentDefinition delegates while Bash/Read/etc. stay off.
SUBAGENT_SPAWN_TOOL = "Agent"
```

(b) In `build_claude_runtime`, resolve the hub model, split skills/delegates, and set `HubContext.delegates`. Replace the `skills = _load_skills_and_promoted_agents(hub_dir)` line and the `ctx = HubContext(...)` block with:

```python
    main_spec = agents_loader.load_main(hub_dir)
    hub_model = settings.model or main_spec.spec.model or "claude-local"
    skills, delegate_agents = _load_skills_and_delegates(hub_dir, hub_model)
    knowledge = knowledge_loader.load_all(hub_dir)
    log.info(
        "hub %s (claude-local): %d skill(s), %d delegate(s), %d knowledge doc(s)",
        hub_dir.name, len(skills), len(delegate_agents), len(knowledge),
    )

    ctx = HubContext(
        hub_dir=hub_dir,
        output_dir=output_dir,
        session_id=session_id,
        settings=settings,
        skills=skills,
        knowledge=knowledge,
        delegates=delegate_agents,
    )
```

Update the import at the top of the file:

```python
from .factory import (
    HubContext,
    _compose_instructions,
    _load_skills_and_delegates,
)
```

(remove the old `_load_skills_and_promoted_agents` import if it is now unused).

Delete the now-duplicate `main_spec = agents_loader.load_main(hub_dir)` line further down (it is loaded above now); keep `main_name` / `main_instructions` referencing the earlier `main_spec`.

(c) Build the `AgentDefinition` map and enable the spawn tool. After `allowed = _allowed_tool_names(registry, mcp_specs=external_mcp)` and before `from claude_agent_sdk import ClaudeAgentOptions`, add:

```python
    from . import handover
    from claude_agent_sdk import AgentDefinition

    reg_names = list(registry.keys())
    agent_defs: dict[str, Any] = {}
    for loaded in delegate_agents:
        scoped = handover.scoped_tool_names(loaded.spec.tools, reg_names)
        agent_defs[loaded.spec.name] = AgentDefinition(
            description=loaded.spec.description,
            prompt=loaded.instructions,
            model=handover.resolve_tier(loaded.spec.model),
            tools=[f"mcp__{_MCP_NAMESPACE}__{n}" for n in scoped],
        )

    base_tools: list[str] = []
    if agent_defs:
        base_tools = [SUBAGENT_SPAWN_TOOL]
        allowed = [*allowed, SUBAGENT_SPAWN_TOOL]
```

(d) In the `opts_kwargs` dict, change `tools=[]` to `tools=base_tools`, and add `agents=agent_defs` when non-empty:

```python
    opts_kwargs: dict[str, Any] = dict(
        system_prompt=main_instructions,
        tools=base_tools,
        allowed_tools=allowed,
        mcp_servers=mcp_servers,
        setting_sources=[],
        include_partial_messages=True,
    )
    if agent_defs:
        opts_kwargs["agents"] = agent_defs
```

Add `opts_kwargs.pop("agents", None)` to the `except TypeError:` fallback block (older SDK without `agents`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `$PY -m pytest tests/test_factory_claude_tool_gating.py -q`
Expected: PASS — new delegate tests pass AND the three original gating tests still pass (MINIMAL has no delegate → `tools == []`, no `Agent` leak).

- [ ] **Step 5: Commit**

```bash
git add hubzoid/factory_claude.py tests/test_factory_claude_tool_gating.py tests/fixtures/delegate_claude_hub
git commit -m "feat: Claude backend wires model-delegates via native AgentDefinition"
```

---

## Task 5: system addendum — surface delegates + relay instruction

**Files:**
- Modify: `hubzoid/system_addendum.py`
- Test: `tests/test_system_addendum.py`

**Interfaces:**
- Consumes: `HubContext.delegates` (Task 3/4), `handover.tool_name`.
- Produces: a `## Delegate agents available` section in the addendum when `ctx.delegates` is non-empty; absent otherwise.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_system_addendum.py` (follow the file's existing import/ctx-construction pattern; build a `HubContext` with a one-item `delegates` list):

```python
def test_delegates_section_present_when_delegates_exist(tmp_path):
    from hubzoid import system_addendum
    from hubzoid.factory import HubContext
    from hubzoid.loaders.agents import AgentSpec, LoadedAgent
    import hubzoid.settings as settingslib

    (tmp_path / "AGENTS.md").write_text("---\nname: m\ndescription: d\n---\nbody")
    spec = AgentSpec(name="opus-helper", description="Deep specialist",
                     model="claude-local/opus")
    loaded = LoadedAgent(spec=spec, instructions="body",
                         source_path=tmp_path / "agents/opus-helper/AGENTS.md")
    ctx = HubContext(
        hub_dir=tmp_path, output_dir=tmp_path, session_id="s",
        settings=settingslib.load(tmp_path),
        skills=[], knowledge=[], delegates=[loaded],
    )
    out = system_addendum.build(ctx, backend="claude-local")
    assert "## Delegate agents available" in out
    assert "handover_opus_helper" in out
    assert "Deep specialist" in out


def test_delegates_section_absent_when_none(tmp_path):
    from hubzoid import system_addendum
    from hubzoid.factory import HubContext
    import hubzoid.settings as settingslib

    (tmp_path / "AGENTS.md").write_text("---\nname: m\ndescription: d\n---\nbody")
    ctx = HubContext(
        hub_dir=tmp_path, output_dir=tmp_path, session_id="s",
        settings=settingslib.load(tmp_path), skills=[], knowledge=[], delegates=[],
    )
    out = system_addendum.build(ctx, backend="openai-agents")
    assert "## Delegate agents available" not in out
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `$PY -m pytest tests/test_system_addendum.py -q -k delegate`
Expected: FAIL — no `## Delegate agents available` section.

- [ ] **Step 3: Write the implementation**

In `hubzoid/system_addendum.py`, add a section builder and call it from `build`. Add after the `_skills_section` call in `build`:

```python
    delegates_section = _delegates_section(ctx)
    if delegates_section:
        parts.append(delegates_section)
```

And define:

```python
def _delegates_section(ctx: "HubContext") -> str:
    delegates = getattr(ctx, "delegates", None) or []
    if not delegates:
        return ""
    from . import handover

    lines = [
        "## Delegate agents available",
        "",
        "Each runs on its own model as a subagent. Call its tool with a clear "
        "brief; it works in isolation and returns a result. Relay its answer "
        "faithfully — do not rewrite or shorten it — then continue:",
        "",
    ]
    for d in delegates:
        model = d.spec.model or "(hub model)"
        lines.append(f"- {handover.tool_name(d.spec.name)}: {d.spec.description} "
                     f"(model: {model})")
    lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `$PY -m pytest tests/test_system_addendum.py -q`
Expected: PASS — new tests pass AND existing addendum tests still pass (no delegates → section absent, so existing assertions on other sections are unaffected).

- [ ] **Step 5: Commit**

```bash
git add hubzoid/system_addendum.py tests/test_system_addendum.py
git commit -m "feat: addendum surfaces delegate agents with relay guidance"
```

---

## Task 6: e2e — delegate actually dispatches on the Claude backend

**Files:**
- Create: `tests/e2e/test_delegate_e2e.py`
- Reuse: `tests/fixtures/delegate_claude_hub/` (Task 4)

**Interfaces:**
- Consumes: `build_claude_runtime` (Task 4), the delegate fixture hub.

- [ ] **Step 1: Write the e2e test**

Create `tests/e2e/test_delegate_e2e.py`:

```python
"""End-to-end: a claude-local hub delegates to a different-tier subagent.

Proves the whole delegation path with a real `claude` CLI: the main agent
(sonnet) dispatches the `opus-helper` subagent (opus tier) via the Agent
spawn tool, the subagent runs and returns, and the main agent relays it.

Self-skips when `claude` CLI is absent or claude-agent-sdk is missing.
Run: cd HubZoid && pytest tests/e2e/test_delegate_e2e.py -m e2e -v
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

HUB = Path(__file__).resolve().parent.parent / "fixtures" / "delegate_claude_hub"


@pytest.fixture(autouse=True)
def _require_claude_local():
    if shutil.which("claude") is None:
        pytest.skip("`claude` CLI not on PATH — claude-local e2e needs it")
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        pytest.skip("claude_agent_sdk not installed")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MODEL", "claude-local")
    monkeypatch.setenv("BRIDGE_API_KEYS", "e2e-dev")
    yield


def test_delegate_dispatches_and_returns():
    from hubzoid.factory_claude import build_claude_runtime

    rt = build_claude_runtime(HUB)

    async def collect() -> str:
        chunks: list[str] = []
        async for c in rt.stream(
            "Delegate this hard question to opus-helper: In one word, what is "
            "the opposite of 'up'? Relay its answer."
        ):
            chunks.append(c)
        return "".join(chunks)

    out = asyncio.new_event_loop().run_until_complete(collect())
    # The subagent dispatch surfaces as a tool-activity marker for the Agent tool.
    assert "✓" in out, f"no tool-activity marker (no dispatch?):\n{out!r}"
    # And the answer makes it back through the main agent.
    assert "down" in out.lower(), f"delegate answer not relayed:\n{out!r}"
```

- [ ] **Step 2: Run the e2e test**

Run: `$PY -m pytest tests/e2e/test_delegate_e2e.py -m e2e -v`
Expected: PASS if `claude login` is active; otherwise SKIPPED (documented). If it dispatches but the marker differs, inspect the raw `out` and adjust the assertion to the actual `tool_events` marker.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_delegate_e2e.py
git commit -m "test: e2e delegate dispatch on the Claude backend"
```

---

## Task 7: docs + full regression sweep

**Files:**
- Modify: `docs/authoring-a-hub.md`
- Modify: `demo-hub/agents/builder/AGENTS.md`

- [ ] **Step 1: Document the delegation trigger**

In `docs/authoring-a-hub.md`, add a short subsection under the `agents/` material:

```markdown
### Sub-agent models (delegation)

A sub-agent in `agents/<name>/` is loaded inline by the main agent (as a
skill) by default. If its frontmatter declares a `model:` that **differs**
from the hub's model *on the same engine*, it instead becomes a **delegate**:
the main agent calls it as a subagent running on that model, gets its answer,
and continues.

- claude-local hub + `model: claude-local/opus` sub-agent → delegate on Opus.
- OpenAI/LiteLLM hub + a different LiteLLM `model:` → delegate on that model.
- Same model as the hub, or a different *engine* (e.g. `gpt-4o` inside a
  claude-local hub), or no `model:` → stays an inline skill.

A delegate's `tools:` whitelist scopes which hub tools it may use. If the
delegate's model needs a provider key that is missing, it falls back to an
inline skill so the hub still boots.
```

- [ ] **Step 2: Update the builder guidance**

In `demo-hub/agents/builder/AGENTS.md`, in the "Do not fabricate a model name" note, append one sentence:

```
Note that setting `model:` to a *different* model than the hub turns a
sub-agent into a delegate that runs on that model; leave it off (or equal to
the hub model) for a plain inline skill.
```

- [ ] **Step 3: Full regression sweep (nothing current breaks)**

Run the whole non-e2e suite:

Run: `$PY -m pytest -q -m "not e2e and not e2e_llm and not e2e_ui"`
Expected: PASS with zero failures. In particular confirm still-green:
- `tests/test_loaders.py::test_load_subagents`
- `tests/test_factory.py::test_build_agent_minimal_hub`, `::test_agents_folder_promoted_to_skills`, `::test_real_skill_wins_over_promoted_agent_on_name_conflict`, `::test_promoted_agent_tools_whitelist_ignored_with_warning`
- `tests/test_factory_claude_tool_gating.py` (all three original tests)
- `tests/test_system_addendum.py` (all pre-existing)

If anything fails, fix before committing.

- [ ] **Step 4: Commit**

```bash
git add docs/authoring-a-hub.md demo-hub/agents/builder/AGENTS.md
git commit -m "docs: document sub-agent model delegation"
```

---

## Self-Review (completed by plan author)

**Spec coverage:** trigger rule (Task 1/2), same-engine-only (Task 1), OpenAI as_tool (Task 3), Claude AgentDefinition + narrowed gate (Task 4), revived `tools:` (Task 1 `scoped_tool_names` + Tasks 3/4), graceful fallback (Task 3 `_prepare_delegates`; Claude has no key path), observability/addendum (Task 5), backward compat regressions (Tasks 3/4/7), e2e (Task 6), docs (Task 7). All spec sections mapped.

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `split_subagents` returns `(skill_agents, delegate_agents)` used identically in Tasks 3/4; `_load_skills_and_delegates` returns `(skills, delegate_agents)`; `handover.scoped_tool_names(whitelist, available)` used in both factories; `SUBAGENT_SPAWN_TOOL` defined in Task 4 and imported in its test; `handover.tool_name` used in Task 3 (tool creation) and Task 5 (addendum) identically.
