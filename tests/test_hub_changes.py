"""Hub changes (hubzoid.hub_changes) and personal memory: exact edits on a
checked base, review by a hub manager only, one commit with trailers,
conflicts that write nothing, crash recovery, undo as a new reviewed change,
remember through the same path, and each person's own notes."""
from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from hubzoid.access import Identity, identity_scope
from hubzoid.hub_changes import service as hc
from tests.chat_helpers import make_hub

OWNER, ANA, BEN = "owner@example.org", "ana@example.org", "ben@example.org"


def _git(hub, *args):
    return subprocess.run(["git", "-C", str(hub), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, monkeypatch)
    (hub / "knowledge").mkdir(exist_ok=True)
    (hub / "knowledge" / "pricing.md").write_text("# Pricing\n\nOrders over 5,000 need one check.\n")
    from hubzoid import access, migrations
    from hubzoid.db import operational_engine

    migrations.upgrade(operational_engine(hub), "operational")
    gs = access.store_for(hub)
    gs.grant(OWNER, hub.name, "manage_access", actor="t")
    gs.grant(ANA, hub.name, "curator", actor="t")
    for who in (OWNER, ANA, BEN):
        gs.grant(who, hub.name, "use_hub", actor="t")
    _git(hub, "init", "-q")
    _git(hub, "add", "-A")
    _git(hub, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "start")
    return hub


RULE = [{"path": "knowledge/pricing.md", "old_text": "Orders over 5,000 need one check.",
         "new_text": "Orders over 5,000 need a second check."}]


def test_a_proposal_waits_and_only_a_manager_applies_it_as_one_commit(hub):
    ch = hc.propose(hub, title="Second check", edits=RULE, actor=ANA, via="chat")
    assert ch["status"] == "pending" and "one check" in (hub / "knowledge" / "pricing.md").read_text()
    assert "+Orders over 5,000 need a second check." in hc.diff(ch)
    with pytest.raises(hc.ChangeError) as nope:
        hc.decide(hub, ch["id"], actor=ANA, decision="approve")
    assert nope.value.code == "permission_denied"
    with pytest.raises(hc.ChangeError):
        hc.propose(hub, title="x", edits=RULE, actor=BEN, via="chat")      # no curator grant
    done = hc.decide(hub, ch["id"], actor=OWNER, decision="approve", expected_revision=ch["revision"])
    assert done["status"] == "applied" and "second check" in (hub / "knowledge" / "pricing.md").read_text()
    msg = _git(hub, "log", "-1", "--format=%B")
    assert f"Hubzoid-Change: {hub.name.lower()}#{ch['id']}" in msg and f"Hubzoid-Approved-By: {OWNER}" in msg
    assert _git(hub, "show", "--name-only", "--format=", "HEAD").split() == ["knowledge/pricing.md"]


def test_two_proposals_on_one_base_the_second_conflicts_and_writes_nothing(hub):
    a = hc.propose(hub, title="a", edits=RULE, actor=ANA, via="chat")
    b = hc.propose(hub, title="b", edits=[{"path": "knowledge/pricing.md", "append": "Refunds need a note."}],
                   actor=ANA, via="chat")
    hc.decide(hub, a["id"], actor=OWNER, decision="approve")
    before = (hub / "knowledge" / "pricing.md").read_text()
    out = hc.decide(hub, b["id"], actor=OWNER, decision="approve")
    assert out["status"] == "conflict" and "knowledge/pricing.md" in out["problem"]
    assert (hub / "knowledge" / "pricing.md").read_text() == before


def test_scope_reserved_paths_secrets_and_exact_edits(hub):
    for path in ("hitl.yaml", "workflows/access.yaml", ".env", "tools_local/x.md", "../x.md", "knowledge/a.py"):
        with pytest.raises(hc.ChangeError):
            hc.build(hub, [{"path": path, "content": "x"}])
    with pytest.raises(hc.ChangeError) as twice:
        hc.build(hub, [{"path": "knowledge/pricing.md", "old_text": "e", "new_text": "E"}])
    assert twice.value.code == "edit_not_unique"
    with pytest.raises(hc.ChangeError) as secret:
        hc.build(hub, [{"path": "knowledge/keys.md", "content": "token ghp_" + "a" * 36}])
    assert secret.value.code == "secret_detected"


def test_only_a_person_applies_never_a_tool_call(hub):
    # An agent's tool call, even for a manager, only proposes (prompt injection can't apply).
    assert hc.propose(hub, title="Fix", edits=RULE, actor=OWNER, via="chat")["status"] == "pending"
    direct = hc.propose(hub, title="Fix", edits=RULE, actor=OWNER, via="cli")
    assert direct["status"] == "applied"           # a manager in their own terminal, knowledge only
    agents = hc.propose(hub, title="Tone", edits=[{"path": "AGENTS.md", "append": "Be brief."}], actor=OWNER,
                        via="cli")
    assert agents["status"] == "pending"
    unattended = hc.propose(hub, title="w", edits=[{"path": "knowledge/new.md", "content": "x"}], actor=OWNER,
                            via="workflow", present=False)
    assert unattended["status"] == "pending"


def test_undo_is_a_new_reviewed_change(hub):
    ch = hc.decide(hub, hc.propose(hub, title="a", edits=RULE, actor=ANA, via="chat")["id"], actor=OWNER,
                   decision="approve")
    back = hc.undo(hub, ch["id"], actor=ANA)
    assert back["status"] == "pending" and back["undoes"] == ch["id"]
    hc.decide(hub, back["id"], actor=OWNER, decision="approve")
    assert "one check" in (hub / "knowledge" / "pricing.md").read_text()


def test_a_crash_after_writing_recovers_to_one_commit(hub, monkeypatch):
    ch = hc.propose(hub, title="a", edits=RULE, actor=ANA, via="chat")
    real = hc._commit
    monkeypatch.setattr(hc, "_commit", lambda *a, **k: (_ for _ in ()).throw(OSError("power cut")))
    assert hc.decide(hub, ch["id"], actor=OWNER, decision="approve")["status"] == "needs_attention"
    # needs_attention after a failed commit is recovered by an operator re-run; simulate the crash state:
    with hc.engine(hub).begin() as c:
        c.execute(hc.changes.update().where(hc.changes.c.id == ch["id"]).values(status="applying"))
    monkeypatch.setattr(hc, "_commit", real)
    assert hc.recover(hub) == [ch["id"]]
    assert hc.recover(hub) == []
    assert _git(hub, "log", "--format=%s").splitlines()[:2] == ["a", "start"]


def test_remember_edits_the_topic_file_through_a_proposal(hub):
    from hubzoid.hub_changes.tools import make

    tools = {t.name: t for t in make(hub)}

    def call(who, **args):
        async def go():
            with identity_scope(Identity.make(who, surface="web")):
                return await tools["remember"].on_invoke_tool(None, json.dumps(args))
        return json.loads(asyncio.run(go()))

    first = call(ANA, topic="Billing", text="Refunds over 30 days need a manager.")
    assert first["hub_change"]["status"] == "pending"
    hc.decide(hub, first["hub_change"]["id"], actor=OWNER, decision="approve")
    path = hub / "knowledge" / "_learned" / "billing.md"
    assert "Refunds over 30 days" in path.read_text() and "name: learned/billing" in path.read_text()
    fix = call(OWNER, topic="Billing", text="Refunds over 60 days need a manager.",
               replace="Refunds over 30 days need a manager.")
    assert fix["hub_change"]["status"] == "pending"           # even a manager's note waits for a click
    hc.decide(hub, fix["hub_change"]["id"], actor=OWNER, decision="approve")
    assert "60 days" in path.read_text() and "30 days" not in path.read_text()


def test_personal_memory_is_per_person_and_feeds_the_chat(hub):
    from hubzoid import personal_memory as pm

    pm.add(hub, "acct-ana", ANA, "Prefers tables")
    pm.add(hub, "acct-ana", ANA, "Calls the North warehouse 'N1'")
    assert pm.get(hub, "acct-ben")["text"] == ""
    block = pm.prompt_block(hub, "acct-ana")
    assert "- Prefers tables" in block and "the hub wins" in block
    mem, n = pm.remove(hub, "acct-ana", ANA, "tables")
    assert n == 1 and "tables" not in mem["text"]
    with pytest.raises(pm.MemoryError_) as stale:
        pm.put(hub, "acct-ana", ANA, "x", expected_revision=1)
    assert stale.value.code == "revision_conflict"
    pm.put(hub, "acct-ana", ANA, "", expected_revision=mem["revision"])
    assert pm.prompt_block(hub, "acct-ana") == ""


def test_folder_names_are_canonical_and_prompts_are_out_of_scope(hub):
    assert hc.check_path("Knowledge/pricing.md") == "knowledge/pricing.md"
    assert "Orders over" in hc.read_text(hub, "Knowledge/pricing.md")
    with pytest.raises(hc.ChangeError):
        hc.check_path("prompts/tone.md")


def test_deleted_notes_keep_their_revision_so_a_stale_editor_fails(hub):
    from hubzoid import personal_memory as pm

    first = pm.put(hub, "acct-ana", ANA, "one")
    pm.put(hub, "acct-ana", ANA, "", expected_revision=first["revision"])
    again = pm.put(hub, "acct-ana", ANA, "two", expected_revision=2)
    assert again["revision"] == 3
    with pytest.raises(pm.MemoryError_):
        pm.put(hub, "acct-ana", ANA, "stale", expected_revision=first["revision"])


def test_unattended_runs_only_suggest(hub):
    from hubzoid.hitl import gate
    from hubzoid.hitl.service import Caller
    from hubzoid.hub_changes.tools import make

    tools = {t.name: t for t in make(hub)}
    unattended = Caller(account_id=OWNER, email=OWNER, present=False, origin="workflow")
    gate_caller = gate.caller
    try:
        gate.caller = lambda: unattended
        out = asyncio.run(tools["remember"].on_invoke_tool(None, json.dumps({"topic": "t", "text": "x"})))
    finally:
        gate.caller = gate_caller
    assert out.startswith("Nothing was proposed") and hc.list_(hub) == []
