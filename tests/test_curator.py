"""Tests for #3: the curator `remember` tool + disk-live knowledge reads.

Covers:
  * tools/curator.py   — remember() create / full-replace / slugify / refusals
                         / provenance / path-safety
  * tools/knowledge.py — read_knowledge / list_knowledge re-scan disk each call
  * access gating      — the curator tool is denied to anonymous / non-group
                         callers and allowed to the `curator` group
  * factory wiring     — _add_curator_tool guards it and lets a hub tool win
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from agents import function_tool
from agents.tool_context import ToolContext

from hubzoid import access
from hubzoid.access import Identity, identity_scope
from hubzoid.tools import curator as curator_mod
from hubzoid.tools import knowledge as knowledge_mod


def _invoke(tool, **kwargs) -> str:
    args = json.dumps(kwargs)
    ctx = ToolContext(context=None, tool_name=tool.name,
                      tool_call_id="test", tool_arguments=args)
    return asyncio.run(tool.on_invoke_tool(ctx, args))


@dataclass
class _Ctx:
    hub_dir: Path
    knowledge: list = field(default_factory=list)


def _remember(hub: Path):
    [tool] = curator_mod.make(_Ctx(hub_dir=hub))
    return tool


# ---------------------------------------------------------------------------
# remember(): create / replace / slug / provenance
# ---------------------------------------------------------------------------
def test_remember_creates_learned_doc(tmp_path):
    out = _invoke(_remember(tmp_path), topic="Billing edge cases",
                  content="Refunds over 30 days need manager approval.")
    assert "Saved" in out and "learned/billing-edge-cases" in out
    doc = (tmp_path / "knowledge" / "_learned" / "billing-edge-cases.md").read_text()
    assert "name: learned/billing-edge-cases" in doc
    assert "Refunds over 30 days" in doc
    assert "learned_by:" in doc and "updated:" in doc


def test_remember_description_requires_explicit_user_request(tmp_path):
    """The tool description must instruct the model to call `remember` ONLY on an
    explicit user request, not proactively/autonomously — this is what keeps the
    hub from curating silently in the background."""
    # normalize whitespace: the description preserves the docstring's hard
    # line-wraps, so phrases can straddle a newline.
    desc = " ".join(_remember(tmp_path).description.lower().split())
    assert "only when the user" in desc
    assert "do not call it on your own initiative" in desc


def test_remember_full_replaces_not_appends(tmp_path):
    rem = _remember(tmp_path)
    _invoke(rem, topic="link", content="X and Y are connected.")
    out = _invoke(rem, topic="link", content="X and Y are only PARTIALLY connected.")
    assert "Updated" in out
    doc = (tmp_path / "knowledge" / "_learned" / "link.md").read_text()
    assert "PARTIALLY connected" in doc
    assert "are connected." not in doc  # the old claim is gone, not appended


def test_remember_slugifies_and_is_path_safe(tmp_path):
    _invoke(_remember(tmp_path), topic="  Weird/../Topic  ##Name!! ",
            content="ok")
    files = list((tmp_path / "knowledge" / "_learned").glob("*.md"))
    assert len(files) == 1
    # no traversal, no slashes — a single safe stem inside _learned/
    assert files[0].parent.name == "_learned"
    assert "/" not in files[0].stem and ".." not in files[0].stem


def test_remember_topic_with_colon_stays_valid_yaml(tmp_path):
    """A topic with a colon must not emit broken YAML that blanks the scan."""
    ctx = _Ctx(hub_dir=tmp_path, knowledge=[])
    ktools = {t.name: t for t in knowledge_mod.make(ctx)}
    out = _invoke(_remember(tmp_path), topic="billing: edge cases",
                  content="Refunds: over 30 days need approval.")
    assert "Saved" in out
    # the doc parses cleanly AND is visible via the disk-live read (not lost)
    listed = _invoke(ktools["list_knowledge"])
    assert "learned/billing-edge-cases" in listed
    body = _invoke(ktools["read_knowledge"], name="learned/billing-edge-cases")
    assert "Refunds: over 30 days" in body


def test_remember_username_with_special_chars_stays_valid(tmp_path):
    from hubzoid.access import Identity, identity_scope
    ctx = _Ctx(hub_dir=tmp_path, knowledge=[])
    ktools = {t.name: t for t in knowledge_mod.make(ctx)}
    with identity_scope(Identity.make("weird: name #x", ["curator"], surface="owui")):
        _invoke(_remember(tmp_path), topic="t", content="c")
    # scan still works — no YAML poisoning from the username
    assert "learned/t" in _invoke(ktools["list_knowledge"])


def test_remember_keeps_backup_on_replace(tmp_path):
    rem = _remember(tmp_path)
    _invoke(rem, topic="t", content="first version")
    _invoke(rem, topic="t", content="second version")
    bak = tmp_path / "knowledge" / "_learned" / "t.md.bak"
    assert bak.is_file() and "first version" in bak.read_text()


def test_remember_leaves_no_temp_file(tmp_path):
    _invoke(_remember(tmp_path), topic="t", content="c")
    ldir = tmp_path / "knowledge" / "_learned"
    leftovers = [p.name for p in ldir.iterdir() if p.suffix == ".tmp" or ".tmp" in p.name]
    assert leftovers == []
    # only the .md doc is loadable knowledge (not the .bak/.tmp)
    from hubzoid.loaders import knowledge as kloader
    assert {d.name for d in kloader.load_all(tmp_path)} == {"learned/t"}


def test_knowledge_loader_isolates_bad_file(tmp_path):
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "good.md").write_text("---\nname: good\n---\n\nfine\n")
    (kdir / "bad.md").write_text("---\ndescription: broken: yaml: here\n---\n\nx\n")
    from hubzoid.loaders import knowledge as kloader
    docs = {d.name: d for d in kloader.load_all(tmp_path)}
    assert "good" in docs                       # good doc survives the bad one


def test_remember_provenance_uses_identity(tmp_path):
    with identity_scope(Identity.make("priya@x.org", ["curator"], surface="owui")):
        _invoke(_remember(tmp_path), topic="t", content="c")
    doc = (tmp_path / "knowledge" / "_learned" / "t.md").read_text()
    assert "learned_by: priya@x.org" in doc


def test_remember_refuses_bad_input(tmp_path):
    rem = _remember(tmp_path)
    assert "refused" in _invoke(rem, topic="", content="x").lower()
    assert "refused" in _invoke(rem, topic="!!!", content="x").lower()   # slug empty
    assert "refused" in _invoke(rem, topic="t", content="   ").lower()
    big = "a" * (curator_mod._MAX_CONTENT_BYTES + 1)
    assert "refused" in _invoke(rem, topic="t", content=big).lower()


# ---------------------------------------------------------------------------
# read_knowledge / list_knowledge are disk-live
# ---------------------------------------------------------------------------
def test_read_knowledge_is_disk_live(tmp_path):
    ctx = _Ctx(hub_dir=tmp_path, knowledge=[])   # snapshot is EMPTY
    tools = {t.name: t for t in knowledge_mod.make(ctx)}
    # A file written after build is still found (no rebuild).
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "fresh.md").write_text("---\nname: fresh\n---\n\nlive body\n")
    assert "fresh" in _invoke(tools["list_knowledge"])
    assert _invoke(tools["read_knowledge"], name="fresh").strip() == "live body"


def test_remember_then_read_knowledge_sees_it(tmp_path):
    ctx = _Ctx(hub_dir=tmp_path, knowledge=[])
    ktools = {t.name: t for t in knowledge_mod.make(ctx)}
    _invoke(_remember(tmp_path), topic="new fact", content="the sky is blue")
    assert "learned/new-fact" in _invoke(ktools["list_knowledge"])
    assert _invoke(ktools["read_knowledge"], name="learned/new-fact").strip() == "the sky is blue"


# ---------------------------------------------------------------------------
# access gating
# ---------------------------------------------------------------------------
def test_curator_tool_is_gated(tmp_path):
    guarded = access.guard_tool(_remember(tmp_path), curator_mod.CURATOR_PERMISSION, tmp_path)
    # anonymous (CLI / scheduled) -> denied, nothing written
    assert "access denied" in _invoke(guarded, topic="t", content="c").lower()
    assert not (tmp_path / "knowledge" / "_learned").exists()
    # wrong group -> denied
    with identity_scope(Identity.make("u", ["sales"], surface="owui")):
        assert "access denied" in _invoke(guarded, topic="t", content="c").lower()
    # curator group on a verified surface -> allowed
    with identity_scope(Identity.make("u", ["curator"], surface="owui")):
        assert "Saved" in _invoke(guarded, topic="t", content="c")


def test_curator_denied_over_slack_surface(tmp_path):
    guarded = access.guard_tool(_remember(tmp_path), curator_mod.CURATOR_PERMISSION, tmp_path)
    with identity_scope(Identity.make("u", ["curator"], surface="slack")):
        assert "access denied" in _invoke(guarded, topic="t", content="c").lower()


# ---------------------------------------------------------------------------
# factory wiring
# ---------------------------------------------------------------------------
def test_add_curator_tool_guards_and_hub_wins(tmp_path):
    from hubzoid.factory import _add_curator_tool

    # Empty registry: curator is added, guarded (is_enabled false for anon).
    reg: dict = {}
    _add_curator_tool(_Ctx(hub_dir=tmp_path), reg, access)
    assert "remember" in reg
    tool = reg["remember"]
    assert tool.is_enabled(None, None) is False   # anon can't see it
    with identity_scope(Identity.make("u", ["curator"], surface="owui")):
        assert tool.is_enabled(None, None) is True

    # Hub already defines `remember` -> hub wins, core tool is not added.
    @function_tool
    def remember(topic: str) -> str:
        """hub's own"""
        return "hub-remember"

    reg2 = {"remember": remember}
    _add_curator_tool(_Ctx(hub_dir=tmp_path), reg2, access)
    assert reg2["remember"] is remember
