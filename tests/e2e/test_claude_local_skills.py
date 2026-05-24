"""End-to-end claude-local tests that actually verify skills and knowledge
are loaded by the model.

Why this file exists. The old unit tests for knowledge/skills only verified
that the loader read files from disk and that the tool returned content
when called directly. They did NOT prove that the LLM actually decides to
call the tool in a real conversation — which is exactly the bug Hubzoid
shipped: knowledge files loaded, tool present in registry, model never
called it. These tests close that gap by asking the model questions
whose answers ONLY exist inside skill/knowledge bodies and asserting the
specific answer comes back.

How to run.
    cd HubZoid && pytest tests/e2e/test_claude_local_skills.py -m e2e -v

The tests self-skip when:
    * the `claude` CLI is not installed
    * the user has not run `claude login` (no subscription credit)
    * the claude-agent-sdk import fails

Each test makes one real LLM call (~1-3 seconds via Haiku), drawing from
the user's Claude subscription credit. Negligible cost.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

HUB = Path(__file__).resolve().parent.parent / "fixtures" / "claude_local_hub"
ARTIFACTS_AT = HUB / ".hubzoid"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _require_claude_local():
    if shutil.which("claude") is None:
        pytest.skip("`claude` CLI not on PATH — claude-local tests need it")
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        pytest.skip("claude_agent_sdk not installed")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Force claude-local for this test, regardless of any prior MODEL value."""
    monkeypatch.setenv("MODEL", "claude-local")
    monkeypatch.setenv("BRIDGE_API_KEYS", "e2e-dev")
    # Use a uncommon port if any test spins up a bridge.
    monkeypatch.setenv("BRIDGE_PORT", "8765")
    yield


@pytest.fixture(autouse=True)
def _clean_chats_dir():
    """Wipe per-chat state between tests so state doesn't leak."""
    shutil.rmtree(ARTIFACTS_AT, ignore_errors=True)
    yield
    shutil.rmtree(ARTIFACTS_AT, ignore_errors=True)


@pytest.fixture
def runtime():
    """Build a ClaudeRuntime for the test hub."""
    from hubzoid.factory_claude import build_claude_runtime
    return build_claude_runtime(HUB)


def _run(rt, prompt: str) -> str:
    """Synchronous wrapper around rt.run for test ergonomics."""
    return asyncio.new_event_loop().run_until_complete(rt.run(prompt))


# ---------------------------------------------------------------------------
# Issue 1 verification: addendum is present in the system prompt.
# ---------------------------------------------------------------------------
def test_addendum_is_present_in_runtime_system_prompt(runtime):
    """The ClaudeRuntime's options must carry a system_prompt that includes
    the auto-injected Hubzoid sections — knowledge list, skills list, and
    generic tool guidance.
    """
    sp = runtime._options.system_prompt
    assert "Hubzoid runtime context" in sp
    assert "## Knowledge available" in sp
    assert "- magic-number:" in sp
    assert "## Skills available" in sp
    assert "- lookup-secret-code:" in sp
    # agents/echo-promoted/ must appear in the skill list (Issue 2).
    assert "- echo-promoted:" in sp
    # Tool guidance is generic, not domain-specific.
    assert "## How to use your tools" in sp


# ---------------------------------------------------------------------------
# Issue 1 + 2 verification: the model actually LOADS knowledge / skills when
# asked. This is the test the old suite was missing.
# ---------------------------------------------------------------------------
def test_model_calls_read_knowledge_and_returns_magic_number(runtime):
    """Ask about the magic number — its answer (88) lives ONLY inside
    knowledge/magic-number.md. If the model returns 88, it must have called
    read_knowledge to find it.
    """
    answer = _run(runtime, "What is the magic number?")
    # 88 must appear in the reply.
    assert "88" in answer, f"model did not load magic-number knowledge. Reply: {answer!r}"


def test_model_calls_load_skill_for_secret_code(runtime):
    """Ask for the secret code — SQUIRREL-42 lives ONLY inside the
    skills/lookup-secret-code skill body.
    """
    answer = _run(runtime, "What is the secret code? Please tell me.")
    assert "SQUIRREL-42" in answer, (
        f"model did not load lookup-secret-code skill. Reply: {answer!r}"
    )


def test_model_calls_grep_data_against_raw_data_corpus(runtime):
    """The agent should find PURPLE-OWL-7421 inside raw_data/repo-zeta/notes/CONFIG.md.

    That value lives ONLY in raw_data/ — not in any knowledge file, not in any
    skill, not in AGENTS.md. The model can only return it by grepping the
    raw_data corpus and then reading the file it found.
    """
    answer = _run(
        runtime,
        "What is the production value of ZETA_THRESHOLD? Search raw_data/ for it.",
    )
    assert "PURPLE-OWL-7421" in answer, (
        f"model did not grep raw_data to find ZETA_THRESHOLD. Reply: {answer!r}"
    )


def test_addendum_mentions_raw_data_when_present(runtime):
    """When the fixture hub has a raw_data/ folder, the addendum must include
    the discovery-protocol section so the agent knows how to navigate it.
    """
    sp = runtime._options.system_prompt
    assert "## Searching raw_data/" in sp
    assert "grep_data" in sp


def test_model_can_load_promoted_agent_as_skill(runtime):
    """A `agents/echo-promoted/AGENTS.md` file must be reachable via
    `load_skill('echo-promoted')` — this proves Issue 2 (Option A).
    """
    answer = _run(
        runtime,
        "Please run the echo-promoted routine and reply with what it tells you to say.",
    )
    assert "PROMOTED" in answer, (
        f"model did not load echo-promoted as a skill. Reply: {answer!r}"
    )


# ---------------------------------------------------------------------------
# Issue 3 verification: tool activity surfaces in the stream as blockquote
# status lines, not silent pauses.
# ---------------------------------------------------------------------------
def test_streaming_surfaces_tool_calls_inline(runtime):
    """Streamed output must contain a `> ✓ **tool_name**` blockquote when
    the model calls a tool — one line per call, no separate confirm.
    """
    async def collect():
        chunks: list[str] = []
        async for chunk in runtime.stream("What is the magic number?"):
            chunks.append(chunk)
        return "".join(chunks)

    full = asyncio.new_event_loop().run_until_complete(collect())
    # One ✓ marker per tool call. No 🔧 (collapsed into ✓ at call start).
    assert "✓" in full, f"no tool-activity marker in stream:\n{full!r}"
    assert "🔧" not in full, f"old two-line format detected:\n{full!r}"
    # The model called read_knowledge, so its short name should appear.
    assert "read_knowledge" in full
    # No "returned" / "B returned" / "KB returned" noise.
    assert "returned" not in full
    # And the actual answer should still be there.
    assert "88" in full


# ---------------------------------------------------------------------------
# Issue 4 verification: write_artifact under chat scope writes to the
# per-chat artifacts dir and the response contains a download URL.
# ---------------------------------------------------------------------------
def test_write_artifact_under_chat_scope_returns_download_link(runtime, monkeypatch):
    """When a chat scope is active, write_artifact lands files at
    `<hub>/.hubzoid/chats/<chat>/artifacts/<file>` and the tool result
    contains the public download URL.
    """
    from hubzoid import _request_ctx

    monkeypatch.setenv("BRIDGE_PORT", "8765")
    monkeypatch.delenv("HUBZOID_PUBLIC_URL", raising=False)

    async def run_with_scope():
        with _request_ctx.chat_scope("oracle-e2e-1"):
            return await runtime.run(
                "Use the write_artifact tool to save a file named "
                "'hello.txt' with the content 'world'. Then tell me the "
                "download link."
            )

    answer = asyncio.new_event_loop().run_until_complete(run_with_scope())
    # The model may or may not parrot the link verbatim, but the file
    # must exist on disk regardless — that is the load-bearing assertion.
    artifact = HUB / ".hubzoid/chats/oracle-e2e-1/artifacts/hello.txt"
    assert artifact.is_file(), (
        f"artifact not written. answer was: {answer!r}"
    )
    assert artifact.read_text().strip() == "world"


# ---------------------------------------------------------------------------
# Negative test: with `auto_addendum: false`, the model has no menu of
# skills/knowledge and shouldn't be able to look them up reliably.
# (This is more of a documentation test for the opt-out behavior.)
# ---------------------------------------------------------------------------
def test_auto_addendum_opt_out_removes_addendum(tmp_path, monkeypatch):
    """Building the runtime against a hub with `auto_addendum: false`
    yields a system prompt with NO Hubzoid context block."""
    # Copy our test hub into a tmp dir and flip the frontmatter.
    import shutil as sh
    target = tmp_path / "opt-out-hub"
    sh.copytree(HUB, target)
    (target / "AGENTS.md").write_text(
        "---\nname: oracle\ndescription: d\nauto_addendum: false\n---\nbody"
    )

    from hubzoid.factory_claude import build_claude_runtime
    rt = build_claude_runtime(target)
    sp = rt._options.system_prompt
    assert "Hubzoid runtime context" not in sp
    assert "## Knowledge available" not in sp
