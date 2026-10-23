"""Workspace authoring: drafts, review, publish, rollback, prompts and OpenAPI (HUB-81, first part)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.chat_helpers import ORIGIN, as_people, build_app, make_hub, who

A = "/api/chat/authoring"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def client(hub):
    with TestClient(build_app()) as c:
        yield c


def test_validation_paths_and_permissions(client, hub, monkeypatch):
    from hubzoid.hub_changes import service as hc

    # A skill change is checked with the runtime's loaders before anyone approves it.
    with pytest.raises(hc.ChangeError) as exc:
        hc.build(hub, [{"path": "skills/stock/SKILL.md", "content": "---\ndescription: no name\n---\nBody\n"}])
    assert exc.value.code == "invalid_file"
    for path in ("../outside.md", "tools_local/x.py", ".env", "skills/stock/run.py"):
        with pytest.raises(hc.ChangeError):
            hc.check_path(path)
    as_people(monkeypatch)
    assert client.get(f"{A}/files", headers=who("ana")).status_code == 403


def test_prompts_render_with_checked_variables(client, hub):
    (hub / "prompts").mkdir()
    (hub / "prompts" / "weekly-summary.md").write_text(
        "---\nname: Weekly summary\nvariables:\n  - name: site\n    options: [north, south]\n"
        "  - name: week\n    max_length: 12\n  - name: note\n    required: false\n---\n"
        "Summarise stock movements at the {{site}} site for week {{week}}. {{note}}\n")
    items = {p["id"]: p for p in client.get("/api/chat/prompts").json()["items"]}
    assert [v["name"] for v in items["weekly-summary"]["variables"]] == ["site", "week", "note"]
    ok = client.post("/api/chat/prompts/weekly-summary/render", headers=ORIGIN,
                     json={"scope": "hub", "values": {"site": "north", "week": "42"}}).json()
    assert ok["text"] == "Summarise stock movements at the north site for week 42."
    r = lambda values: client.post("/api/chat/prompts/weekly-summary/render", headers=ORIGIN,
                                   json={"scope": "hub", "values": values})
    assert r({"site": "east", "week": "1"}).status_code == 422
    assert r({"site": "north"}).status_code == 422
    assert r({"site": "north", "week": "1", "extra": "x"}).status_code == 422
    put = client.put("/api/chat/prompts/mine", headers=ORIGIN,
                     json={"content": "---\nname: Mine\nvariables: [{name: who}]\n---\nHello {{who}}", "expected_revision": None})
    assert put.status_code == 200
    mine = client.post("/api/chat/prompts/mine/render", headers=ORIGIN, json={"scope": "personal", "values": {"who": "Ana"}})
    assert mine.json()["text"] == "Hello Ana"
    undeclared = client.put("/api/chat/prompts/bad", headers=ORIGIN,
                            json={"content": "---\nname: Bad\n---\nHi {{who}}", "expected_revision": None})
    assert undeclared.status_code == 422


def test_openapi_import_converts_only_selected_operations(client, monkeypatch):
    monkeypatch.setenv("HUBZOID_IN_APP_EDITING", "true")   # Beta only (HUB-140 F30)
    spec = {"openapi": "3.0.3", "info": {"title": "Stock", "version": "1"},
            "servers": [{"url": "https://stock.example/api"}],
            "paths": {"/items/{sku}": {
                "get": {"operationId": "get_item", "parameters": [
                    {"name": "sku", "in": "path", "required": True, "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "ok"}}},
                "delete": {"operationId": "delete_item", "parameters": [
                    {"name": "sku", "in": "path", "required": True, "schema": {"type": "string"}}],
                           "responses": {"204": {"description": "gone"}}}}}}
    out = client.post(f"{A}/openapi/preview", headers=ORIGIN, json={"spec": spec, "selected": ["get_item"]}).json()
    assert [o["operation_id"] for o in out["operations"]] == ["get_item", "delete_item"]
    assert [t["name"] for t in out["tools"]] == ["get_item"]  # the delete operation is not exposed
    remote = {**spec, "components": {"schemas": {"X": {"$ref": "https://evil.example/schema.json"}}}}
    r = client.post(f"{A}/openapi/preview", headers=ORIGIN, json={"spec": remote})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "remote_reference"
    broken = client.post(f"{A}/openapi/preview", headers=ORIGIN, json={"spec": {"openapi": "3.0.3", "paths": {}}})
    assert broken.status_code == 422


def _approve(client, path, content=None, new=None):
    d = client.post(f"{A}/drafts", headers=ORIGIN, json={"path": path, **({"content": content} if content else {})}).json()
    if new is not None:
        d = client.patch(f"{A}/drafts/{d['id']}", headers=ORIGIN, json={"content": new, "expected_revision": 1}).json()
    d = client.post(f"{A}/drafts/{d['id']}/submit", headers=ORIGIN, json={"expected_revision": d["revision"]}).json()
    client.post(f"{A}/drafts/{d['id']}/review", headers=ORIGIN, json={"decision": "approve", "digest": d["digest"]})
    return d


def test_flat_and_differently_spelt_skill_folders_are_listed(client, hub):
    import shutil

    if (hub / "skills").exists():
        shutil.move(str(hub / "skills"), str(hub / "Skill"))
    (hub / "Skill").mkdir(exist_ok=True)
    (hub / "Skill" / "triage.md").write_text("---\nname: triage\ndescription: Sort tickets\n---\nSort them.\n")
    files = {f["path"]: f["kind"] for f in client.get(f"{A}/files").json()["items"]}
    assert files.get("Skill/triage.md") == "skill"


def test_broken_frontmatter_is_explained_in_plain_words():
    from hubzoid.chat.authoring import validate

    problems = validate("AGENTS.md", "---\nname: a\ndescription: x\nbad line: : here\n  oops\n---\nBody.\n")
    assert problems and "settings block" in problems[0]["message"]
    assert "while scanning" not in problems[0]["message"] and "unicode string" not in problems[0]["message"]


def test_a_rebuilt_runtime_opens_and_closes_in_the_lifespan_task(monkeypatch, tmp_path):
    """Publishing rebuilds the chat runtime in one long-lived task; a replaced
    runtime is closed there once nothing runs, and the original never is."""
    import asyncio
    from types import SimpleNamespace

    from hubzoid import runtime as runtime_lib
    from hubzoid.chat import authoring_routes as ar

    events = []

    class Fake:
        def __init__(self, n):
            self.n = n

        async def aopen(self):
            events.append(("open", self.n, asyncio.current_task()))

        async def aclose(self):
            events.append(("close", self.n, asyncio.current_task()))

    made = iter(range(1, 10))
    monkeypatch.setattr(runtime_lib, "build", lambda hub: Fake(next(made)))
    original = Fake(0)
    ctx = SimpleNamespace(runtime=original, models=None)
    running = {"busy": True}

    async def main():
        loop = asyncio.create_task(ar.runtime_reloader(ctx, tmp_path, original, lambda: running["busy"],
                                                       idle_check=0.01))
        await asyncio.sleep(0)
        assert await ar._reload_runtime(ctx) is True and ctx.runtime.n == 1
        assert await ar._reload_runtime(ctx) is True and ctx.runtime.n == 2
        await asyncio.sleep(0.05)
        assert not [e for e in events if e[0] == "close"]  # a reply is still running
        running["busy"] = False
        await asyncio.sleep(0.05)
        assert [e[1] for e in events if e[0] == "close"] == [1]
        loop.cancel()
        try:
            await loop
        except asyncio.CancelledError:
            pass
        return loop

    task = asyncio.run(main())
    assert [e[1] for e in events if e[0] == "close"] == [1, 2]  # never the original
    assert {e[2] for e in events} == {task}
    assert ar._reload_runtime.__doc__ and asyncio.run(ar._reload_runtime(SimpleNamespace())) is False


def test_workspace_edits_are_hub_changes_and_history_is_git(client, hub):
    import subprocess

    from hubzoid.hub_changes import service as changes

    assert client.post("/api/chat/authoring/drafts", headers=ORIGIN, json={"path": "AGENTS.md"}).status_code in (404, 405)
    subprocess.run(["git", "init", "-q"], cwd=hub, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.org", "-c", "user.name=t", "add", "-A"], cwd=hub, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.org", "-c", "user.name=t", "commit", "-qm", "start"],
                   cwd=hub, check=True)
    # A delegate's instructions and shared prompts are proposable like AGENTS.md.
    assert changes.check_path("agents/helper/AGENTS.md") == "agents/helper/AGENTS.md"
    assert changes.check_path("prompts/weekly.md") == "prompts/weekly.md"
    history = client.get("/api/chat/authoring/files/versions?path=AGENTS.md", headers=ORIGIN).json()["items"]
    assert history and history[0]["subject"] == "start"
