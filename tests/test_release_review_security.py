"""Release review regressions (2026-09-26), synthetic data only.

1. A markdown task's publish_artifact cannot publish private hub state: only
   its own scratch folder is exempt from the agent read guard, judged on the
   resolved path (no string prefix, no symlink escape).
2. Report ownership is bound to the chat-app account it was published under,
   the owner's current status and (on managed hubs) current hub access. A
   replacement account that reuses the email inherits nothing.
3. Run history shows the hub's managers operational metadata only; what a run
   produced is shown only to the account it acted as.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from hubzoid import artifacts as arts
from hubzoid.access import store_for

OWNER, MANAGER, TEAMMATE = "priya@example.org", "manager@example.org", "sam@example.org"
SECRET = "SYNTHETIC-PRIVATE-MAIL-CONTENT"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for k in ("HUBZOID_DEPLOYMENT", "WEBUI_AUTH", "DATABASE_URL", "HUBZOID_PUBLIC_URL",
              "WEBUI_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path}/ops.db")
    h = tmp_path / "sales"
    h.mkdir()
    (h / "AGENTS.md").write_text("---\nname: sales\n---\nTest")
    gs = store_for(h)
    gs.set_authoritative(True, hub="sales")
    for who, acct in ((OWNER, "acct-priya"), (MANAGER, "acct-manager"), (TEAMMATE, "acct-sam")):
        gs.upsert_identity(email=who, owui_id=acct)
        gs.grant(who, "sales", "use_hub", actor="test")
    return h


def _publish(h, owner=OWNER, account="acct-priya", name="report.html"):
    p = h / "run-out" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text("<p>Synthetic private report</p>")
    out = arts.publish(h, hub="sales", owner=owner, owner_account=account, source=p)
    return arts.get(h, out["id"])


# ---- 1. the publish tool cannot reach private hub state ----------------------------

def _publish_tool(h, write=(".",)):
    from hubzoid.scheduling import ScheduledTask, parse_cron
    from hubzoid.tools import schedule_tools
    from hubzoid.workflows.identity import RunIdentity, markdown_scratch

    ident = RunIdentity(OWNER, "acct-priya", "run_as", OWNER)
    task = ScheduledTask(name="report", schedule="0 8 * * *", cron=parse_cron("0 8 * * *"),
                         body="t", write=list(write), publish_artifacts=True,
                         run_identity=ident.to_dict(), run_id="run-1")
    task.state_rel = markdown_scratch(h, "report", ident)
    tool = next(t for t in schedule_tools.make(h, task, lambda **_: None)
                if t.name == "publish_artifact")
    return task, tool


def _invoke(tool, path):
    from agents import RunConfig
    from agents.tool_context import ToolContext

    raw = json.dumps({"path": path, "title": "t"})
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="c",
                      tool_arguments=raw, run_config=RunConfig())
    return asyncio.run(tool.on_invoke_tool(ctx, raw))


def test_publish_tool_refuses_private_state_even_with_a_broad_writable_path(hub):
    task, tool = _publish_tool(hub, write=(".",))
    scratch = hub / task.state_rel
    scratch.mkdir(parents=True)
    other = hub / ".hubzoid" / "schedule" / f"{task.name}@someone-else-1234"   # look-alike
    other.mkdir(parents=True)
    (other / "theirs.html").write_text("<p>not yours</p>")
    chat = hub / ".hubzoid" / "chats" / "c1" / "artifacts"
    chat.mkdir(parents=True)
    (chat / "chat.html").write_text("x")
    (hub / ".hubzoid" / "private.db").write_text("SYNTHETIC DATABASE SECRET")
    (hub / ".env").write_text("TOKEN=secret")
    (scratch / ".env").write_text("TOKEN=secret")
    (scratch / "renamed.txt").write_bytes(b"SQLite format 3\x00" + b"x" * 64)
    os.symlink(hub / ".hubzoid" / "private.db", scratch / "link.html")
    os.symlink(other / "theirs.html", scratch / "link2.html")
    refused = [".hubzoid/private.db", ".env", f"{task.state_rel}/.env",
               f"{task.state_rel}/renamed.txt", f"{task.state_rel}/link.html",
               f"{task.state_rel}/link2.html",
               f".hubzoid/schedule/{task.name}@someone-else-1234/theirs.html",
               ".hubzoid/chats/c1/artifacts/chat.html"]
    for path in refused:
        assert "refused" in _invoke(tool, path), path
    with store_for(hub)._engine.connect() as c:
        from sqlalchemy import text

        assert c.execute(text("SELECT count(*) FROM hz_artifacts")).scalar() == 0


def test_publish_tool_still_publishes_the_runs_own_output(hub):
    task, tool = _publish_tool(hub, write=("reports",))
    scratch = hub / task.state_rel
    scratch.mkdir(parents=True)
    (scratch / "digest.html").write_text("<h1>Digest</h1>")
    (hub / "reports").mkdir()
    (hub / "reports" / "weekly.csv").write_text("a,b\n1,2\n")
    for path in (f"{task.state_rel}/digest.html", "reports/weekly.csv"):
        assert _invoke(tool, path).startswith("Published artifact a"), path


# ---- 2. ownership is bound to the account, its status and its hub access -------------

def test_revoking_hub_access_removes_the_owners_access_until_restored(hub):
    gs = store_for(hub)
    art = _publish(hub)
    gs.revoke(OWNER, "sales", "use_hub", actor="test")
    assert arts.role(hub, art, OWNER) is None
    with pytest.raises(arts.ArtifactError):
        arts.set_audience(hub, art, OWNER, "hub")
    with pytest.raises(arts.ArtifactError):
        arts.delete(hub, art, OWNER)
    gs.grant(OWNER, "sales", "use_hub", actor="test")              # access restored
    assert arts.role(hub, art, OWNER) == "owner"


def test_a_blocked_owner_regains_their_reports_when_reactivated(hub):
    gs = store_for(hub)
    art = _publish(hub)
    gs.suspend(OWNER, actor="test")
    assert arts.role(hub, art, OWNER) is None
    gs.suspend(OWNER, actor="test", suspended=False)
    gs.grant(OWNER, "sales", "use_hub", actor="test")               # suspend removed grants
    assert arts.role(hub, art, OWNER) == "owner"


def test_a_replacement_account_inherits_no_report_share_or_link(hub):
    gs = store_for(hub)
    gs.grant(OWNER, "sales", arts.PUBLIC_LINK_PERMISSION, actor="test")
    art = _publish(hub)
    token = arts.create_link(hub, art, OWNER)["url"].split("#", 1)[1]
    shared = _publish(hub, name="shared.html")
    arts.set_audience(hub, shared, OWNER, "people", [TEAMMATE])
    assert arts.role(hub, arts.get(hub, shared.id), TEAMMATE) == "viewer"
    # Both emails now belong to different people: new chat-app accounts.
    for who, new in ((OWNER, "acct-priya-2"), (TEAMMATE, "acct-sam-2")):
        gs.upsert_identity(email=who, owui_id=new)
        gs.suspend(who, actor="test", suspended=False)
        gs.grant(who, "sales", "use_hub", actor="test")
    gs.grant(OWNER, "sales", arts.PUBLIC_LINK_PERMISSION, actor="test")
    assert arts.role(hub, arts.get(hub, art.id), OWNER) is None
    assert arts.role(hub, arts.get(hub, shared.id), TEAMMATE) is None
    assert arts.open_link(hub, token) is None
    with pytest.raises(arts.ArtifactError):
        arts.delete(hub, arts.get(hub, art.id), OWNER)
    # Reports the replacement account publishes itself are its own.
    assert arts.role(hub, _publish(hub, account="acct-priya-2", name="new.html"), OWNER) == "owner"


def test_reports_without_an_account_id_are_honoured_only_for_local_quickstart(hub, tmp_path,
                                                                            monkeypatch):
    art = _publish(hub, account=None)
    assert arts.role(hub, art, OWNER) is None                       # a person: needs the id
    local = tmp_path / "local"
    local.mkdir()
    (local / "AGENTS.md").write_text("---\nname: local\n---\nTest")
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path}/local.db")
    mine = _publish(local, owner="admin@localhost", account=None)
    assert arts.role(local, mine, "admin@localhost") == "owner"


def test_the_viewer_api_follows_the_same_owner_rules(hub, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hubzoid.access import session
    from hubzoid.artifacts import web

    monkeypatch.setattr(session, "verified_email",
                        lambda request, hub_dir=None, **_: request.cookies.get("who", ""))
    app = FastAPI()
    app.include_router(web.build_router(hub))
    c = TestClient(app, follow_redirects=False)
    c.cookies.set("who", OWNER)
    art = _publish(hub)
    assert c.get(f"/portal/artifacts/api/{art.id}").status_code == 200
    store_for(hub).revoke(OWNER, "sales", "use_hub", actor="test")
    for path in (f"/portal/artifacts/api/{art.id}", f"/portal/artifacts/{art.id}/content",
                 f"/portal/artifacts/{art.id}/download"):
        assert c.get(path).status_code == 404, path


def test_email_links_only_reports_of_the_same_account(hub, monkeypatch):
    from hubzoid import email_delivery as mail
    from hubzoid.workflows.identity import RunIdentity

    monkeypatch.setenv("HUBZOID_EMAIL_DELIVERY", "preview")
    art = _publish(hub)
    replacement = RunIdentity(OWNER, "acct-priya-2", "run_as", OWNER)
    result = mail.send_to_owner(hub, hub="sales", identity=replacement, subject="s",
                                body="b", artifact_ids=[art.id])
    assert result["status"] == "refused"


# ---- 3. run history: metadata for managers, results for the run's account ------------

class _FakeClient:
    """A DBOSClient stand-in holding three runs: priya's personal run, a legacy
    service run, and a run with no recorded identity."""

    runs = {
        "priya-run": ("personal_report", "SUCCESS", SECRET, None,
                      [{"function_name": "hz_run_identity",
                        "output": {"subject": OWNER, "source": "run_as"}, "error": None},
                       {"function_name": "_agent_step", "output": SECRET, "error": None}]),
        "priya-failed": ("personal_report", "ERROR", None, RuntimeError(f"boom {SECRET}"),
                         [{"function_name": "hz_run_identity",
                           "output": {"subject": OWNER, "source": "run_as"}, "error": None},
                          {"function_name": "_agent_step", "output": None,
                           "error": RuntimeError(SECRET)}]),
        "legacy-run": ("hz_markdown_task", "SUCCESS", "hub digest done", None,
                       [{"function_name": "hz_md_identity",
                         "output": {"subject": "workflow:md:digest",
                                    "source": "legacy-service"}, "error": None}]),
        "old-run": ("nightly", "SUCCESS", SECRET, None, []),
    }

    def __init__(self, **kwargs):
        pass

    def _w(self, wid):
        name, status, output, error, _ = self.runs[wid]
        return SimpleNamespace(created_at=1, dequeued_at=1, completed_at=2, name=name,
                               workflow_id=wid, status=status, output=output, error=error,
                               application_name=None)

    def list_workflows(self, workflow_ids=None, **kwargs):
        return [self._w(w) for w in (workflow_ids or self.runs)]

    def list_workflow_steps(self, wid):
        return self.runs[wid][4]

    def destroy(self):
        pass


@pytest.fixture
def history(hub, monkeypatch):
    import dbos

    from hubzoid import db

    monkeypatch.setattr(dbos, "DBOSClient", _FakeClient)
    dbfile = hub / "dbos.sqlite"
    dbfile.touch()
    monkeypatch.setattr(db, "dbos_url", lambda *_: "sqlite:///" + str(dbfile))
    store_for(hub).grant(MANAGER, "sales", "manage_access", actor="test")
    return hub


def _portal(hub, who, *, org=False):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hubzoid.portal import PortalAdmin, build_router

    app = FastAPI()
    app.include_router(build_router(hub, admin_resolver=lambda _: PortalAdmin(
        subject=who, is_org_admin=org, manageable=["sales"])))
    return TestClient(app)


@pytest.mark.parametrize("org", [False, True], ids=["hub-manager", "org-admin"])
def test_managers_see_run_metadata_but_not_personal_results(history, org):
    c = _portal(history, MANAGER, org=org)
    for query in ("hub=sales", "", "hub=sales&run_id=priya-run", "run_id=priya-run",
                  "hub=sales&run_id=priya-failed", "hub=sales&run_id=old-run"):
        body = c.get(f"/portal/api/runs?{query}").text
        assert SECRET not in body, query
    rows = {r["id"]: r for r in c.get("/portal/api/runs?hub=sales").json()["runs"]}
    assert rows["priya-run"]["status"] == "SUCCESS" and rows["priya-run"]["redacted"]
    assert rows["priya-run"]["run_as"] == OWNER
    assert rows["priya-failed"]["error"].startswith("RuntimeError")
    assert rows["legacy-run"]["output"] == "hub digest done"          # a service run
    assert rows["old-run"]["output"] is None                           # no identity: private
    (detail,) = c.get("/portal/api/runs?hub=sales&run_id=priya-run").json()["runs"]
    by_step = {s["name"]: s for s in detail["steps"]}
    assert by_step["hz_run_identity"]["output"]                        # who ran it: metadata
    assert by_step["_agent_step"]["output"] is None and by_step["_agent_step"]["redacted"]


def test_the_runs_own_account_sees_its_results(history):
    c = _portal(history, OWNER)
    (detail,) = c.get("/portal/api/runs?hub=sales&run_id=priya-run").json()["runs"]
    assert detail["output"] == SECRET
    assert {s["name"]: s["output"] for s in detail["steps"]}["_agent_step"] == SECRET
    (failed,) = c.get("/portal/api/runs?hub=sales&run_id=priya-failed").json()["runs"]
    assert SECRET in failed["error"]


def test_configuration_errors_stay_actionable_for_managers():
    from hubzoid.workflows.identity import IdentityError
    from hubzoid.workflows.observe import _error_summary

    msg = "Workflow 'w' in hub 'sales' has no account to run as. Set HUBZOID_WORKFLOW_USER."
    assert _error_summary(IdentityError(msg)) == msg
    assert SECRET not in _error_summary(RuntimeError(SECRET))
    assert SECRET not in _error_summary(f"stored {SECRET}")


def test_the_servers_operator_cli_keeps_full_detail(history):
    from hubzoid.workflows.observe import runs

    rows = {r["id"]: r for r in runs(history, trusted=True)}
    assert rows["priya-run"]["output"] == SECRET
    rows = {r["id"]: r for r in runs(history)}                          # default: private
    assert rows["priya-run"]["output"] is None
