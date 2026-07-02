# Model-triggered agent delegation for `agents/`

- **Date:** 2026-07-02
- **Status:** Design approved, pending spec review
- **Area:** `hubzoid/loaders/agents.py`, `hubzoid/factory.py`, `hubzoid/factory_claude.py`, new `hubzoid/handover.py`, `hubzoid/system_addendum.py`

## Problem

Today every sub-agent under `<hub>/agents/<name>/` is unconditionally
*promoted to a skill* (`loaders/agents.py:promote_to_skills`): its body is
loaded inline by the main agent via `load_skill(<name>)`, and its `model:` and
`tools:` frontmatter fields are **discarded**. So there is no way to say "this
sub-task should run on a *different* model than the hub's main model."

We want: **when a sub-agent declares a `model:` that differs from the hub's
model, the main agent can delegate a sub-task to that sub-agent running on its
own model, get the result back, and continue.** When no different model is
declared, behavior is unchanged (promote to skill).

## Why not the old handoff logic

`agents/` used to be wired as OpenAI Agents SDK **handoffs** (control transfers
to the sub-agent). That was removed because "handoff state didn't survive
Hubzoid's stateless HTTP bridge across turns" (`factory.py` docstring).

Verified root cause (`server.py:_flatten_messages` + `runtime.stream`): every
turn the **entire** conversation is flattened into one `[user]/[assistant]`
prompt string, the agent is rebuilt from scratch, and `Runner.run(agent,
prompt)` is called. There is **no `Session`, no `to_input_list()`, no
`last_agent`** persisted. A handoff transfers control to agent B and the SDK
tracks "we are now B" in `result.last_agent`, which the caller must persist and
restore each turn. Hubzoid discards it, so sticky cross-turn handoff cannot
work without adding a session store.

### The chosen model: delegation, not handover

The main agent **stays in control the whole time**. It *asks* a sub-agent (a
tool call), the sub-agent runs on its own model in an isolated context and
returns its final message as the tool result, and the main agent reviews it and
continues (relays it, combines it with other subagents, asks a follow-up).

This is deliberately **not** true handover (where the specialist's output *is*
the user-facing reply). Delegation was chosen because:

1. **Thin Hubzoid layer.** Delegation is pure wiring on top of each SDK's native
   primitive. Hubzoid adds **no** new streaming, interception, or session code.
   True handover would require mid-stream abort-and-re-drive logic in both
   runtimes plus suppression of the main agent's trailing output.
2. **Backend parity.** Both backends behave identically. The Claude Agent SDK
   has **no** control-transfer primitive at all — subagents (`Task`) always run
   and return their result to the parent. Delegation is the only semantics
   expressible on both backends, and `claude-local` is Hubzoid's default
   backend.
3. **It is the native primitive** both SDKs already provide (OpenAI
   `Agent.as_tool`; Claude `AgentDefinition` + `Task`).

Trade-off accepted: delegation costs an extra main-model turn (main decides →
subagent runs → main writes the reply) and the main model mediates the
subagent's answer. Mitigated with a system-addendum instruction to relay a
delegated subagent's output faithfully.

## Goals

- A sub-agent with a `model:` that differs from the hub's model becomes a
  **delegate**: a tool the main agent can call, which runs the sub-agent on its
  declared model within the current turn and returns its output.
- Works on **both** backends (OpenAI Agents SDK + LiteLLM; Claude Agent SDK),
  using each backend's native subagent primitive.
- **Same-engine only.** A sub-agent's `model:` must resolve to the same engine
  as the hub (both `claude-local*`, or both LiteLLM). Cross-engine is out of
  scope for v1.
- **Backward compatible.** Existing sub-agents (no `model:`, or `model:` equal
  to the hub model) keep behaving exactly as today (promoted to skills).
- Revive the currently-discarded `tools:` whitelist for delegate agents.

## Non-goals

- True handover / control transfer (specialist answers the user directly).
- Cross-engine delegation (e.g. `gpt-4o` sub-agent inside a `claude-local` hub).
- Sticky cross-turn routing / session persistence.
- A config toggle between delegation and handover modes.
- Streaming the sub-agent's internal tokens/tool-calls to the user.

## Verified facts (installed environment)

- `openai-agents` **0.17.3**: `Agent.as_tool(tool_name, tool_description,
  custom_output_extractor, ...)` present. Internally runs
  `Runner.run(starting_agent=self, ...)` — nested, self-contained run; parent
  keeps control. Each `Agent` has its own `model=`, which may be a
  `LitellmModel`.
- `claude-agent-sdk` **0.2.87**: `ClaudeAgentOptions.agents` field present;
  `AgentDefinition(description, prompt, tools, disallowedTools, model, skills,
  memory, mcpServers, initialPrompt, maxTurns, background, effort,
  permissionMode)`. `model` is Claude-only (`"sonnet" | "opus" | "haiku" |
  "inherit"` or a full Claude id); non-Anthropic models are not supported.
- **Claude tool gate:** `tools=[]` emits `--tools ""` to the CLI, which zeroes
  the base tool set (Bash/Read/Edit/Write/**Task**/…). `agents=` is sent over
  the init handshake separately, so defining subagents alone does **not** let
  the model dispatch them — the subagent-spawn tool must be in the base set. We
  can add **only** that one tool (`tools=["Task"]`) to re-enable dispatch while
  keeping Bash/Read/Edit/etc. off. (Exact tool name — `Task` vs `Agent` — and
  subagent tool-scoping under this gate must be confirmed by a live spike; see
  Implementation step 0.)

## Architecture

### 1. Disposition classification — new module `hubzoid/handover.py`

One shared predicate, called by both factories, so behavior never drifts.

```
hub_model = runtime._resolve_model_id(hub_dir, settings)   # "claude-local" | "openai/gpt-4o" | ...

def classify(sub_spec, hub_model) -> "skill" | "delegate":
    if not sub_spec.model:                          return "skill"   # unchanged
    if engine(sub_spec.model) != engine(hub_model): return "skill"   # cross-engine -> warn
    if norm(sub_spec.model) == norm(hub_model):     return "skill"   # same brain
    return "delegate"
```

- `engine(model)`: reuse the existing predicate — `model.lower().startswith(
  "claude-local")` → `"claude"`, else `"litellm"`.
- `norm(model)`: for the claude engine, collapse bare `claude-local` →
  `claude-local/sonnet` (the `_CLAUDE_LOCAL_DEFAULT`) and compare the tier pin;
  for litellm, compare the full model string. This is the backward-compat
  guarantee: a `model: claude-local` sub-agent in a `claude-local` (=sonnet) hub
  normalizes equal → stays a skill.

The module also owns:
- `resolve_tier(claude_model)` — map `claude-local/opus` → `"opus"` for
  `AgentDefinition.model` (reuse `factory_claude._parse_model_pin`).
- Tool scoping (see §4).

`loaders/agents.py` keeps `load_subagents` and a trimmed `promote_to_skills`
that handles only the sub-agents classified as `"skill"`. It stops discarding
`model:`/`tools:` — those pass through on `AgentSpec` for the delegate path.
The `AgentSpec` schema already carries `model` and `tools`; no schema change
needed.

### 2. HubContext + factory shared helper

`_load_skills_and_promoted_agents(hub_dir)` (shared by both factories) is
extended / split so each factory receives two lists:

- `skills` — real skills + skill-classified sub-agents (existing behavior).
- `delegates` — `LoadedAgent`s classified as `"delegate"`.

`HubContext` gains a `delegates: list` field (default empty) so
`system_addendum` can surface them (§5).

### 3a. OpenAI backend (`factory.py`)

For each delegate:

```python
sub = Agent(
    name=spec.name,
    instructions=spec.instructions,          # sub-agent body; no main addendum
    model=modellib.build(spec.model),        # its own LitellmModel
    tools=scoped_tools(spec, registry),      # §4
)
delegate_tool = sub.as_tool(
    tool_name=f"handover_{safe(spec.name)}",
    tool_description=spec.description,        # frontmatter description = "when to use"
)
```

`delegate_tool`s are appended to the main agent's `tools=` list alongside the
existing registry. `modellib.build` raises `MissingProviderKey` if the
provider key is absent — caught at build time and downgraded to skill + warning
(§6), so the hub still boots.

### 3b. Claude backend (`factory_claude.py`)

For each delegate (guaranteed claude engine by classification), build an
`AgentDefinition`:

```python
agents[spec.name] = AgentDefinition(
    description=spec.description,
    prompt=spec.instructions,
    model=handover.resolve_tier(spec.model),         # "opus" | "haiku" | full id
    tools=scoped_tool_names(spec, registry),         # ["mcp__hubzoid__<t>", ...]
)
```

Pass `agents=agents` in `ClaudeAgentOptions`, and when `agents` is non-empty,
change `tools=[]` → `tools=[SUBAGENT_TOOL_NAME]` and add `SUBAGENT_TOOL_NAME` to
`allowed_tools`. `SUBAGENT_TOOL_NAME` is `"Task"` (or `"Agent"` — confirmed by
the spike). When there are no delegates, `tools=[]` is unchanged — existing
hubs keep the exact current tool gate.

### 4. Tool scoping (revive `tools:`)

A delegate's `tools:` frontmatter whitelist becomes meaningful again:

- If `tools:` present → the sub-agent is scoped to exactly those tool names,
  intersected with the hub registry (restricted-gated already via
  `access.apply`). Unknown names are dropped with a warning.
- If `tools:` omitted → inherit the hub's non-restricted registry **minus the
  delegate tools themselves** (recursion guard: a delegate cannot call another
  delegate in v1).
- On the Claude backend, tool names are the `mcp__hubzoid__<name>` forms.

Skill-classified sub-agents still ignore `tools:` (they run on the main agent,
which owns the whole registry) — unchanged.

### 5. Observability & discovery

- A delegate call surfaces as the standard one-line `SHOW_TOOLS` tool-call
  marker (`handover_<name>(...)`), same as any tool. The sub-agent's internal
  tokens/tool-calls stay a black box (both SDKs' default). Its final answer
  returns as the tool result and the main agent weaves it into the reply.
- `system_addendum` gains a **"Delegate agents available"** section listing each
  delegate (`- handover_<name>: <description> (model: <model>)`) plus a one-line
  instruction to relay a delegate's output faithfully rather than rewrite it.
  Fed from `HubContext.delegates`.

### 6. Graceful degradation

At build time, a sub-agent that *would* be a delegate but can't be wired falls
back to a skill with a `log.warning`, so the hub always boots:

- cross-engine target,
- missing provider key (`MissingProviderKey` from `modellib.build`),
- (Claude) unknown/empty tier.

## Data flow (one turn)

1. Bridge flattens history → `prompt`; runtime rebuilds the agent (with delegate
   tools/definitions wired) and calls `Runner.run` / `query`.
2. Main agent decides to delegate → calls `handover_<name>` (OpenAI) / `Task`
   with `subagent_type=<name>` (Claude).
3. SDK runs the sub-agent on its own model, isolated context = the tool-call
   brief the main agent passed (not full history).
4. Sub-agent's final message returns as the tool result. Main agent continues,
   reviews, writes the user-facing reply.
5. Next turn: fresh rebuild; the main agent re-decides. No cross-turn state.

## Backward compatibility

- No `model:` → skill (unchanged).
- `model:` equal to hub model (incl. bare `claude-local` in a `claude-local`
  hub) → skill (unchanged). This protects the many sub-agents that carry the
  `builder` template's default `model: claude-local`.
- Cross-engine `model:` → skill + warn.
- Hubs with zero delegates: Claude backend keeps `tools=[]` verbatim; OpenAI
  backend tool list unchanged.

## Testing

- `handover.classify` unit tests: matrix of {no model, same model, same-engine
  different tier, cross-engine} × {claude-local hub, litellm hub}.
- `norm`/`resolve_tier` unit tests (bare `claude-local` ↔ `claude-local/sonnet`).
- OpenAI factory: a hub with a delegate builds a `handover_<name>` FunctionTool
  whose sub-agent carries the declared `LitellmModel`; `tools:` scoping honored;
  recursion guard (no delegate-of-delegate).
- Claude factory: delegates present → `options.agents` populated and `tools`
  includes exactly the subagent-spawn tool (+ nothing else); delegates absent →
  `tools == []`. Update `test_factory_claude_tool_gating.py` accordingly.
- Graceful fallback: missing key / cross-engine → skill + warning, hub builds.
- E2E (Claude, real `claude` login): a `claude-local` hub with a
  `claude-local/opus` delegate actually dispatches and returns.

## Implementation steps

0. **Spike (live, on the bundled `claude` CLI):** confirm (a) the subagent-spawn
   tool name (`Task` vs `Agent`) accepted by `--tools`, and (b) that a subagent
   under `--tools "<spawn>"` can still reach `mcp__hubzoid__*` and is correctly
   scoped by `AgentDefinition.tools`. Record findings; they fix the two unknowns
   above.
1. `hubzoid/handover.py`: `engine`, `norm`, `resolve_tier`, `classify`,
   `scoped_tools` + unit tests.
2. `loaders/agents.py`: stop discarding `model`/`tools`; split skill vs delegate.
3. `factory.py`: build delegate `Agent`s + `as_tool`, append to main tools;
   populate `HubContext.delegates`; graceful fallback.
4. `factory_claude.py`: build `AgentDefinition`s; enable subagent-spawn tool when
   delegates exist; graceful fallback. Update tool-gating test.
5. `system_addendum.py`: "Delegate agents available" section + relay instruction.
6. Docs: `docs/authoring-a-hub.md` note that a differing `model:` turns a
   sub-agent into a delegate; update the `builder` sub-agent guidance.

## Open questions / future work

- True handover mode behind a flag, if a pure-router use case emerges.
- Cross-engine delegation (would need a nested LiteLLM run inside the Claude
  backend, or vice versa).
- Streaming a delegate's activity to the user (OpenAI `as_tool(on_stream=...)`;
  Claude subagent transcript).
- Delegate-of-delegate (nested delegation) — currently guarded off.
