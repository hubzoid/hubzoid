"""Claude-local backend must tell the model who the real end user is.

`claude -p` authenticates via the box's local subscription account, so with no
other identity signal the model assumes it *is* that account holder and answers
"who am I" with the Claude login instead of the Open WebUI user. Hubzoid already
resolves the true caller into `current_identity()`; the runtime must surface it
in the USER turn (not the cached system prompt).

These tests pin:
  1. `_identity_preamble()` renders the resolved caller (and stays quiet/anon
     when no login reached the bridge).
  2. `ClaudeRuntime.stream()` actually prepends that preamble to the text that
     reaches `query()`.
  3. It rides the user turn, never the system prompt (so prompt caching holds).
"""
from __future__ import annotations

from hubzoid.access import Identity, identity_scope
from hubzoid.factory_claude import ClaudeRuntime, _identity_preamble


# ---------------------------------------------------------------------------
# 1. The pure helper.
# ---------------------------------------------------------------------------
def test_preamble_names_the_resolved_owui_user():
    ident = Identity.make(user="jane@sadhguru.org", groups=["testers"], surface="owui")
    with identity_scope(ident):
        text = _identity_preamble()
    assert "jane@sadhguru.org" in text
    assert "owui" in text
    assert "testers" in text
    # It must steer the model OFF the local account identity.
    assert "who am i" in text.lower()


def test_preamble_is_anonymous_off_request():
    # No identity bound (CLI / scheduled) -> anonymous, no invented identity.
    text = _identity_preamble()
    assert "automated run" in text.lower()
    assert "@" not in text  # no email fabricated


# ---------------------------------------------------------------------------
# 2 + 3. stream() prepends it to the user turn, not the system prompt.
# ---------------------------------------------------------------------------
class _FakeOptions:
    """Minimal stand-in for ClaudeAgentOptions: only what stream() touches."""

    def __init__(self):
        self.env = None
        self.system_prompt = "SYSTEM PROMPT (cached prefix)"


def test_stream_prepends_identity_to_the_user_turn(monkeypatch):
    """stream() must feed query() a user turn carrying the resolved caller, and
    leave the cached system prompt untouched. Driven with anyio.run() to match
    the repo's async-test convention (pytest-asyncio is not installed)."""
    import anyio

    seen: dict = {}

    async def _fake_query(*, prompt, options):
        seen["prompt"] = prompt
        seen["system_prompt"] = getattr(options, "system_prompt", None)
        return
        yield  # make this an async generator

    # Patch the SDK entry point stream() imports at call time.
    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query, raising=False)

    # hub_dir=None keeps stream() on the plain-string path (no vision wrapping),
    # so the prompt reaching query() is the raw prepended string.
    runtime = ClaudeRuntime(name="t", options=_FakeOptions(), hub_dir=None)
    ident = Identity.make(user="jane@sadhguru.org", groups=["testers"], surface="owui")

    async def _run():
        with identity_scope(ident):
            async for _ in runtime.stream("Who am I?"):
                pass

    anyio.run(_run)

    # The user turn carries the identity AND the original question, in order.
    assert "jane@sadhguru.org" in seen["prompt"]
    assert seen["prompt"].rstrip().endswith("Who am I?")
    # The cached system prompt is untouched — identity never leaks into it.
    assert "jane@sadhguru.org" not in seen["system_prompt"]
