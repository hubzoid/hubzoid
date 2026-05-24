"""Unit tests for hubzoid.tools.grep_data.

Forces the Python backend (by stubbing shutil.which → None) so the same
behaviour is asserted on machines with and without ripgrep installed.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from agents.tool_context import ToolContext

from hubzoid.tools import grep_data as gd


@pytest.fixture(autouse=True)
def _force_python_backend(monkeypatch):
    """Pin tests to the pure-Python backend so output is deterministic."""
    monkeypatch.setattr(gd.shutil, "which", lambda _: None)


class _Ctx:
    def __init__(self, hub_dir: Path):
        self.hub_dir = hub_dir
        self.output_dir = hub_dir / "output" / "test-session"


def _invoke(tool, **kwargs) -> str:
    """Call a FunctionTool through the SDK's invocation path."""
    args = json.dumps(kwargs)
    ctx = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="test",
        tool_arguments=args,
    )
    return asyncio.run(tool.on_invoke_tool(ctx, args))


def _build_tool(hub_dir: Path):
    return gd.make(_Ctx(hub_dir))[0]


def _seed_corpus(hub: Path):
    rd = hub / "raw_data"
    (rd / "repo-a/src").mkdir(parents=True)
    (rd / "repo-a/src/login.py").write_text(
        "def authenticate_user(username, password):\n"
        "    return True\n"
        "\n"
        "class AuthenticateMiddleware:\n"
        "    pass\n"
    )
    (rd / "repo-a/tests").mkdir()
    (rd / "repo-a/tests/test_login.py").write_text(
        "def test_authenticate():\n"
        "    assert authenticate_user('a', 'b')\n"
    )
    (rd / "repo-b").mkdir()
    (rd / "repo-b/auth.go").write_text(
        "package auth\n\nfunc AuthenticateUser() error {\n    return nil\n}\n"
    )
    return rd


# --- Sanity / refusal paths ------------------------------------------------
def test_returns_friendly_message_when_raw_data_missing(tmp_path):
    out = _invoke(_build_tool(tmp_path), pattern="x")
    assert "raw_data" in out
    assert "not present" in out.lower()


def test_refuses_path_outside_hub(tmp_path):
    _seed_corpus(tmp_path)
    out = _invoke(_build_tool(tmp_path), pattern="x", path="../etc")
    assert "refused" in out or "outside" in out.lower()


def test_friendly_when_subpath_does_not_exist(tmp_path):
    _seed_corpus(tmp_path)
    out = _invoke(_build_tool(tmp_path), pattern="x", path="raw_data/nope")
    assert "not found" in out


def test_invalid_regex_returns_message(tmp_path):
    _seed_corpus(tmp_path)
    out = _invoke(_build_tool(tmp_path), pattern="[unclosed")
    assert "invalid regex" in out.lower()


def test_no_matches_returns_no_matches(tmp_path):
    _seed_corpus(tmp_path)
    out = _invoke(_build_tool(tmp_path), pattern="zzznevermatchzzz")
    assert "no matches" in out.lower()


# --- Output format ---------------------------------------------------------
def test_returns_path_lineno_content(tmp_path):
    _seed_corpus(tmp_path)
    out = _invoke(_build_tool(tmp_path), pattern="authenticate_user")
    # path is hub-relative, line is real, content is the matching line
    assert "raw_data/repo-a/src/login.py:1:def authenticate_user" in out
    assert "raw_data/repo-a/tests/test_login.py:2:" in out


def test_case_sensitive_default(tmp_path):
    _seed_corpus(tmp_path)
    out = _invoke(_build_tool(tmp_path), pattern="Authenticate")
    assert "AuthenticateMiddleware" in out
    assert "AuthenticateUser" in out
    # Lowercase variant must not appear (regex is case-sensitive).
    assert "def authenticate_user" not in out


def test_files_grouped_by_match_count_desc(tmp_path):
    _seed_corpus(tmp_path)
    out = _invoke(_build_tool(tmp_path), pattern="authenticate", path="raw_data/repo-a")
    # tests/test_login.py: 2 lowercase "authenticate" hits.
    # src/login.py:        1 lowercase "authenticate" hit (line 4 is capital A).
    # The 2-match file must appear before the 1-match file.
    test_idx = out.index("repo-a/tests/test_login.py")
    src_idx = out.index("repo-a/src/login.py")
    assert test_idx < src_idx


def test_scoped_path_excludes_other_repos(tmp_path):
    _seed_corpus(tmp_path)
    out = _invoke(_build_tool(tmp_path), pattern="authenticate", path="raw_data/repo-a")
    assert "repo-a" in out
    assert "repo-b" not in out


# --- Context lines ---------------------------------------------------------
def test_context_lines_included(tmp_path):
    _seed_corpus(tmp_path)
    out = _invoke(_build_tool(tmp_path), pattern="return True", context=1)
    # Should include the surrounding lines: the def above and the blank below.
    assert "def authenticate_user" in out
    assert "return True" in out


# --- Caps ------------------------------------------------------------------
def test_caps_total_matches(tmp_path):
    rd = tmp_path / "raw_data"
    rd.mkdir()
    for i in range(150):
        (rd / f"f{i}.txt").write_text("needle\n")
    out = _invoke(_build_tool(tmp_path), pattern="needle")
    # At most MAX_MATCHES content lines + footer.
    content_lines = [ln for ln in out.splitlines() if ":" in ln and ln.endswith("needle")]
    assert len(content_lines) <= gd.MAX_MATCHES
    assert "Refine" in out


def test_caps_matches_per_file(tmp_path):
    rd = tmp_path / "raw_data"
    rd.mkdir()
    (rd / "big.txt").write_text("\n".join(["needle"] * 100) + "\n")
    out = _invoke(_build_tool(tmp_path), pattern="needle")
    lines_for_big = [ln for ln in out.splitlines() if ln.startswith("raw_data/big.txt:")]
    assert len(lines_for_big) <= gd.MAX_PER_FILE
    assert "more matches than shown" in out.lower()


def test_skips_ignored_dirs(tmp_path):
    rd = tmp_path / "raw_data"
    (rd / "repo-a/node_modules/pkg").mkdir(parents=True)
    (rd / "repo-a/node_modules/pkg/index.js").write_text("var needle = 1;\n")
    (rd / "repo-a/src").mkdir(parents=True)
    (rd / "repo-a/src/real.js").write_text("var needle = 2;\n")
    out = _invoke(_build_tool(tmp_path), pattern="needle")
    assert "src/real.js" in out
    assert "node_modules" not in out


def test_skips_binary_files(tmp_path):
    rd = tmp_path / "raw_data"
    rd.mkdir()
    (rd / "bin.dat").write_bytes(b"\x00\x01\x02needle\x00\x01")
    (rd / "text.txt").write_text("needle here\n")
    out = _invoke(_build_tool(tmp_path), pattern="needle")
    assert "text.txt" in out
    assert "bin.dat" not in out


def test_skips_oversize_files(tmp_path, monkeypatch):
    monkeypatch.setattr(gd, "MAX_FILE_BYTES", 100)
    rd = tmp_path / "raw_data"
    rd.mkdir()
    (rd / "big.txt").write_text("needle\n" + "x" * 500)
    (rd / "small.txt").write_text("needle here\n")
    out = _invoke(_build_tool(tmp_path), pattern="needle")
    assert "small.txt" in out
    assert "big.txt" not in out
