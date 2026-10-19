"""Tests for #3: the curator `remember` tool + disk-live knowledge reads.

Covers:
  * tools/curator.py   — remember goes through hub changes (tests/test_hub_changes.py)
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
    return {t.name: t for t in curator_mod.make(_Ctx(hub_dir=hub))}["remember"]


def test_knowledge_loader_isolates_bad_file(tmp_path):
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "good.md").write_text("---\nname: good\n---\n\nfine\n")
    (kdir / "bad.md").write_text("---\ndescription: broken: yaml: here\n---\n\nx\n")
    from hubzoid.loaders import knowledge as kloader
    docs = {d.name: d for d in kloader.load_all(tmp_path)}
    assert "good" in docs                       # good doc survives the bad one


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
    access.store_for(tmp_path).grant("u", tmp_path.name, "manage_access", actor="t")
    with identity_scope(Identity.make("u", [], surface="owui")):
        out = json.loads(_invoke(_remember(tmp_path), topic="new fact", text="the sky is blue"))
    assert "learned/new-fact" not in _invoke(ktools["list_knowledge"])   # a proposal until a person applies it
    from hubzoid.hub_changes import service as hc

    hc.decide(tmp_path, out["hub_change"]["id"], actor="u", decision="approve")
    assert "learned/new-fact" in _invoke(ktools["list_knowledge"])
    assert _invoke(ktools["read_knowledge"], name="learned/new-fact").strip() == "the sky is blue"


def test_remember_description_requires_explicit_user_request(tmp_path):
    desc = _remember(tmp_path).description
    assert "ONLY when the person explicitly asks" in desc and "save_memory" in desc


# ---------------------------------------------------------------------------
# access gating
# ---------------------------------------------------------------------------
def test_curator_tool_is_gated(tmp_path):
    guarded = access.guard_tool(_remember(tmp_path), curator_mod.CURATOR_PERMISSION, tmp_path)
    # anonymous (CLI / scheduled) -> denied, nothing written
    assert "access denied" in _invoke(guarded, topic="t", text="c").lower()
    assert not (tmp_path / "knowledge" / "_learned").exists()
    # no grant (a group named like it grants nothing) -> denied
    with identity_scope(Identity.make("u", ["curator"], surface="owui")):
        assert "access denied" in _invoke(guarded, topic="t", text="c").lower()
    # granted, on a verified surface -> allowed
    access.store_for(tmp_path).grant("u", tmp_path.name, curator_mod.CURATOR_PERMISSION, actor="t")
    with identity_scope(Identity.make("u", [], surface="owui")):
        out = json.loads(_invoke(guarded, topic="t", text="c"))
    assert out["hub_change"]["status"] == "pending"   # a curator's note is reviewed first


def test_curator_denied_over_slack_surface(tmp_path):
    guarded = access.guard_tool(_remember(tmp_path), curator_mod.CURATOR_PERMISSION, tmp_path)
    with identity_scope(Identity.make("u", ["curator"], surface="slack")):
        assert "access denied" in _invoke(guarded, topic="t", text="c").lower()


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
    access.store_for(tmp_path).grant("u", tmp_path.name, curator_mod.CURATOR_PERMISSION, actor="t")
    with identity_scope(Identity.make("u", [], surface="owui")):
        assert tool.is_enabled(None, None) is True

    # Hub already defines `remember` -> hub wins, core tool is not added.
    @function_tool
    def remember(topic: str) -> str:
        """hub's own"""
        return "hub-remember"

    reg2 = {"remember": remember}
    _add_curator_tool(_Ctx(hub_dir=tmp_path), reg2, access)
    assert reg2["remember"] is remember


def test_hub_managers_may_use_remember_without_the_curator_grant(tmp_path):
    from hubzoid.factory import _add_curator_tool

    reg: dict = {}
    _add_curator_tool(_Ctx(hub_dir=tmp_path), reg, access)
    access.store_for(tmp_path).grant("boss", tmp_path.name, "manage_access", actor="t")
    with identity_scope(Identity.make("boss", [], surface="owui")):
        assert reg["remember"].is_enabled(None, None) is True
    with identity_scope(Identity.make("nobody", [], surface="owui")):
        assert reg["remember"].is_enabled(None, None) is False
