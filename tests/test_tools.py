"""Behavior tests for every pre-shipped tool.

Each test invokes a tool through the openai-agents SDK's `on_invoke_tool`
interface (the same path the LLM uses at runtime), so we exercise the
JSON-schema wrapper, not just the inner closure. Mocks `httpx.Client`
for the web tools so the tests stay offline.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from agents.tool_context import ToolContext

from hubzoid.loaders.knowledge import LoadedKnowledge
from hubzoid.loaders.skills import LoadedSkill
from hubzoid.tools import (
    current_time as ct_mod,
    files as files_mod,
    knowledge as knowledge_mod,
    render as render_mod,
    skills_tool as skills_mod,
    web_http as web_mod,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call(tool, **kwargs: Any) -> str:
    """Invoke a FunctionTool via the SDK's tool-invocation path."""
    args = json.dumps(kwargs)
    ctx = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="test",
        tool_arguments=args,
    )
    return asyncio.run(tool.on_invoke_tool(ctx, args))


def _by_name(tools: list, name: str):
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(f"tool {name!r} not in {[t.name for t in tools]}")


@dataclass
class _Ctx:
    """Minimal HubContext stand-in. Mirrors the fields tools actually read."""
    hub_dir: Path
    output_dir: Path
    session_id: str = "test-session"
    settings: Any = None
    skills: list = None
    knowledge: list = None

    def __post_init__(self):
        if self.skills is None:
            self.skills = []
        if self.knowledge is None:
            self.knowledge = []


@pytest.fixture
def ctx(tmp_path: Path) -> _Ctx:
    hub = tmp_path / "hub"
    hub.mkdir()
    out = hub / "output" / "sess"
    out.mkdir(parents=True)
    return _Ctx(hub_dir=hub, output_dir=out)


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


def test_read_file_reads_under_hub(ctx):
    (ctx.hub_dir / "hello.txt").write_text("hello world", encoding="utf-8")
    read_file = _by_name(files_mod.make(ctx), "read_file")
    assert _call(read_file, path="hello.txt") == "hello world"


def test_read_file_refuses_outside_hub(ctx, tmp_path):
    (tmp_path / "outside.txt").write_text("nope", encoding="utf-8")
    read_file = _by_name(files_mod.make(ctx), "read_file")
    result = _call(read_file, path=str(tmp_path / "outside.txt"))
    assert "outside the hub directory" in result


def test_read_file_missing(ctx):
    read_file = _by_name(files_mod.make(ctx), "read_file")
    assert "not found" in _call(read_file, path="nope.txt")


def test_read_file_truncates_large_and_writes_overflow(ctx):
    big = "x" * 50_000
    (ctx.hub_dir / "big.txt").write_text(big, encoding="utf-8")
    read_file = _by_name(files_mod.make(ctx), "read_file")
    out = _call(read_file, path="big.txt")
    assert "truncated at 25,000" in out
    assert "Full output saved to" in out
    assert "read-overflow-" in out
    # Truncated head + footer is well under the original size.
    assert len(out) < 30_000


def test_read_file_offset_and_limit(ctx):
    (ctx.hub_dir / "abc.txt").write_text("abcdefghij", encoding="utf-8")
    read_file = _by_name(files_mod.make(ctx), "read_file")
    assert _call(read_file, path="abc.txt", offset=3, limit=4) == "defg"


def test_list_files_caps_with_refine_hint(ctx):
    for i in range(120):
        (ctx.hub_dir / f"f{i:03d}.txt").write_text("x")
    list_files = _by_name(files_mod.make(ctx), "list_files")
    out = _call(list_files, glob="*.txt")
    assert "Showing 100 of 120" in out
    assert "Refine" in out


def test_list_files_globs(ctx):
    (ctx.hub_dir / "a.md").write_text("a")
    (ctx.hub_dir / "b.md").write_text("b")
    (ctx.hub_dir / "c.txt").write_text("c")
    list_files = _by_name(files_mod.make(ctx), "list_files")
    out = _call(list_files, glob="*.md")
    assert "a.md" in out and "b.md" in out and "c.txt" not in out


def test_list_files_empty(ctx):
    list_files = _by_name(files_mod.make(ctx), "list_files")
    assert _call(list_files, glob="nothing*") == ""


def test_write_artifact_writes_under_output_when_no_chat_in_scope(ctx):
    """No chat scope -> writes to the legacy session output dir."""
    write_artifact = _by_name(files_mod.make(ctx), "write_artifact")
    result = _call(write_artifact, filename="result.txt", content="ok")
    assert "Saved" in result  # new response format
    assert (ctx.output_dir / "result.txt").read_text() == "ok"


def test_write_artifact_refuses_invalid_filename(ctx):
    """`../../etc/passwd` sanitises to `passwd` (basename), but we test ../
    in isolation which leaves nothing."""
    write_artifact = _by_name(files_mod.make(ctx), "write_artifact")
    result = _call(write_artifact, filename="../..", content="x")
    assert "refused" in result.lower() or "empty filename" in result.lower()


def test_write_artifact_strips_directory_components(ctx):
    """Directory components in filename are stripped; only basename survives."""
    write_artifact = _by_name(files_mod.make(ctx), "write_artifact")
    _call(write_artifact, filename="nested/dir/file.md", content="hi")
    # Lands at the artifact root (no chat scope -> session output dir).
    assert (ctx.output_dir / "file.md").read_text() == "hi"
    # And NOT in any subdirectory.
    assert not (ctx.output_dir / "nested").exists()


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------


def _skill(name: str, body: str = "skill body", description: str = "desc") -> LoadedSkill:
    from hubzoid.loaders.skills import SkillSpec
    spec = SkillSpec(name=name, description=description)
    return LoadedSkill(spec=spec, body=body, source_path=Path(f"/tmp/skills/{name}/SKILL.md"))


def test_list_skills_empty(ctx):
    list_skills = _by_name(skills_mod.make(ctx), "list_skills")
    assert "no skills" in _call(list_skills).lower()


def test_list_skills_with_skills(ctx):
    ctx.skills = [_skill("alpha", description="first"), _skill("beta", description="second")]
    list_skills = _by_name(skills_mod.make(ctx), "list_skills")
    out = _call(list_skills)
    assert "alpha" in out and "beta" in out
    assert "first" in out and "second" in out


def test_load_skill_returns_body(ctx):
    ctx.skills = [_skill("alpha", body="step one\nstep two")]
    load_skill = _by_name(skills_mod.make(ctx), "load_skill")
    assert "step one" in _call(load_skill, name="alpha")


def test_load_skill_missing(ctx):
    ctx.skills = [_skill("alpha")]
    load_skill = _by_name(skills_mod.make(ctx), "load_skill")
    out = _call(load_skill, name="beta")
    assert "no skill" in out.lower()


# ---------------------------------------------------------------------------
# knowledge
# ---------------------------------------------------------------------------


def _kn(name: str, body: str = "kn body", description: str = "desc") -> LoadedKnowledge:
    return LoadedKnowledge(name=name, description=description, body=body)


def test_list_knowledge_empty(ctx):
    list_knowledge = _by_name(knowledge_mod.make(ctx), "list_knowledge")
    assert "no knowledge" in _call(list_knowledge).lower()


def test_list_knowledge_with_docs(ctx):
    ctx.knowledge = [_kn("intro", description="overview"), _kn("api", description="endpoints")]
    list_knowledge = _by_name(knowledge_mod.make(ctx), "list_knowledge")
    out = _call(list_knowledge)
    assert "intro" in out and "api" in out


def test_read_knowledge_returns_body(ctx):
    ctx.knowledge = [_kn("intro", body="hello there")]
    read_knowledge = _by_name(knowledge_mod.make(ctx), "read_knowledge")
    assert _call(read_knowledge, name="intro") == "hello there"


def test_read_knowledge_missing_lists_available(ctx):
    ctx.knowledge = [_kn("intro")]
    read_knowledge = _by_name(knowledge_mod.make(ctx), "read_knowledge")
    out = _call(read_knowledge, name="nope")
    assert "no document" in out
    assert "intro" in out  # menu of what IS available


# ---------------------------------------------------------------------------
# render_jinja
# ---------------------------------------------------------------------------


def test_render_jinja_basic(ctx):
    render_jinja = _by_name(render_mod.make(ctx), "render_jinja")
    out = _call(render_jinja, template="Hello {{ name }}!", context_json='{"name": "world"}')
    assert out == "Hello world!"


def test_render_jinja_empty_context(ctx):
    render_jinja = _by_name(render_mod.make(ctx), "render_jinja")
    out = _call(render_jinja, template="static", context_json="{}")
    assert out == "static"


def test_render_jinja_invalid_json(ctx):
    render_jinja = _by_name(render_mod.make(ctx), "render_jinja")
    out = _call(render_jinja, template="{{ name }}", context_json="not json")
    assert "[render_jinja" in out or "error" in out.lower()


# ---------------------------------------------------------------------------
# current_time
# ---------------------------------------------------------------------------


def test_current_time_utc_default(ctx):
    current_time = _by_name(ct_mod.make(ctx), "current_time")
    out = _call(current_time)
    # ISO 8601 with +00:00 for UTC, e.g. 2026-05-20T12:34:56+00:00
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$", out), out


def test_current_time_named_zone(ctx):
    current_time = _by_name(ct_mod.make(ctx), "current_time")
    out = _call(current_time, zone="Asia/Kolkata")
    assert out.endswith("+05:30"), out


def test_current_time_unknown_zone(ctx):
    current_time = _by_name(ct_mod.make(ctx), "current_time")
    out = _call(current_time, zone="Mars/Olympus_Mons")
    assert "unknown timezone" in out


# ---------------------------------------------------------------------------
# web_http: http_get + web_search (+ env-disable toggles)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


class _FakeClient:
    last_url: str | None = None

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, headers=None):
        type(self).last_url = url
        return type(self)._response_for(url)

    @classmethod
    def _response_for(cls, url):
        return _FakeResponse("OK")


@pytest.fixture
def fake_httpx(monkeypatch):
    """Replace httpx.Client with a settable fake. Returns the class for assertions."""
    class C(_FakeClient):
        last_url = None

    monkeypatch.setattr(web_mod.httpx, "Client", C)
    return C


def test_http_get_returns_body(ctx, fake_httpx, monkeypatch):
    monkeypatch.delenv("HTTP_ALLOWLIST", raising=False)
    monkeypatch.delenv("HUBZOID_DISABLE_HTTP_GET", raising=False)
    fake_httpx._response_for = classmethod(lambda cls, u: _FakeResponse("body bytes"))
    http_get = _by_name(web_mod.make(ctx), "http_get")
    out = _call(http_get, url="https://example.com")
    assert "HTTP 200" in out and "body bytes" in out
    assert fake_httpx.last_url == "https://example.com"


def test_http_get_blocks_non_http(ctx):
    http_get = _by_name(web_mod.make(ctx), "http_get")
    assert "only http/https" in _call(http_get, url="file:///etc/passwd")


def test_http_get_honors_allowlist(ctx, fake_httpx, monkeypatch):
    monkeypatch.setenv("HTTP_ALLOWLIST", "example.com")
    monkeypatch.delenv("HUBZOID_DISABLE_HTTP_GET", raising=False)
    http_get = _by_name(web_mod.make(ctx), "http_get")
    assert "refused" in _call(http_get, url="https://evil.com")
    fake_httpx._response_for = classmethod(lambda cls, u: _FakeResponse("ok"))
    assert "HTTP 200" in _call(http_get, url="https://example.com")


def test_http_get_disabled_via_env(ctx, monkeypatch):
    monkeypatch.setenv("HUBZOID_DISABLE_HTTP_GET", "true")
    monkeypatch.delenv("HUBZOID_DISABLE_WEB_SEARCH", raising=False)
    tool_names = {t.name for t in web_mod.make(ctx)}
    assert "http_get" not in tool_names
    assert "web_search" in tool_names  # the other one still ships


def test_web_search_parses_results(ctx, fake_httpx, monkeypatch):
    monkeypatch.delenv("HUBZOID_DISABLE_WEB_SEARCH", raising=False)
    html = """
    <a class="result__a" href="https://a.example.com">Title One</a>
    <a class="result__snippet">snippet one</a>
    <a class="result__a" href="https://b.example.com">Title Two</a>
    <a class="result__snippet">snippet two</a>
    """
    fake_httpx._response_for = classmethod(lambda cls, u: _FakeResponse(html))
    web_search = _by_name(web_mod.make(ctx), "web_search")
    out = _call(web_search, query="hubzoid", limit=5)
    assert "Title One" in out and "Title Two" in out
    assert "https://a.example.com" in out


def test_web_search_no_results(ctx, fake_httpx, monkeypatch):
    monkeypatch.delenv("HUBZOID_DISABLE_WEB_SEARCH", raising=False)
    fake_httpx._response_for = classmethod(lambda cls, u: _FakeResponse("<html></html>"))
    web_search = _by_name(web_mod.make(ctx), "web_search")
    out = _call(web_search, query="hubzoid")
    assert out == "(no results)"


def test_web_search_disabled_via_env(ctx, monkeypatch):
    monkeypatch.setenv("HUBZOID_DISABLE_WEB_SEARCH", "true")
    monkeypatch.delenv("HUBZOID_DISABLE_HTTP_GET", raising=False)
    tool_names = {t.name for t in web_mod.make(ctx)}
    assert "web_search" not in tool_names
    assert "http_get" in tool_names


def test_both_web_tools_disabled(ctx, monkeypatch):
    monkeypatch.setenv("HUBZOID_DISABLE_HTTP_GET", "true")
    monkeypatch.setenv("HUBZOID_DISABLE_WEB_SEARCH", "true")
    tools = web_mod.make(ctx)
    assert tools == []
