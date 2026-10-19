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


def test_agent_instructions_draft_review_publish_and_rollback(client, hub):
    before = (hub / "AGENTS.md").read_text()
    d = client.post(f"{A}/drafts", headers=ORIGIN, json={"path": "AGENTS.md"}).json()
    assert d["content"] == before and d["diff"] == "" and not d["stale"]
    new = before + "\n\nAlways answer stock questions with the SKU first.\n"
    d = client.patch(f"{A}/drafts/{d['id']}", headers=ORIGIN, json={"content": new, "expected_revision": 1}).json()
    assert "+Always answer stock questions" in d["diff"]
    d = client.post(f"{A}/drafts/{d['id']}/submit", headers=ORIGIN, json={"expected_revision": 2}).json()
    assert d["state"] == "in_review"
    assert client.patch(f"{A}/drafts/{d['id']}", headers=ORIGIN,
                        json={"content": "x", "expected_revision": 2}).status_code == 409  # reviewed content is frozen
    wrong = client.post(f"{A}/drafts/{d['id']}/review", headers=ORIGIN, json={"decision": "approve", "digest": "sha256:0"})
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "review_superseded"
    client.post(f"{A}/drafts/{d['id']}/review", headers=ORIGIN, json={"decision": "approve", "digest": d["digest"]})
    pub = client.post(f"{A}/drafts/{d['id']}/publish", headers=ORIGIN, json={"digest": d["digest"]})
    assert pub.status_code == 200 and (hub / "AGENTS.md").read_text() == new
    vers = client.get(f"{A}/files/versions", params={"path": "AGENTS.md"}).json()["items"]
    assert [v["current"] for v in vers] == [True, False]  # the new one, and the file it replaced
    assert client.get(f"{A}/files").json()["hub"] == hub.name.lower()  # an agent's page opens its own file
    back = client.post(f"{A}/rollbacks", headers=ORIGIN, json={"path": "AGENTS.md", "version_id": vers[1]["version_id"]})
    assert back.status_code == 201 and back.json()["content"] == before and back.json()["note"].startswith("Roll back")


def test_stale_base_is_refused_not_rebased(client, hub):
    d = client.post(f"{A}/drafts", headers=ORIGIN, json={"path": "AGENTS.md"}).json()
    d = client.patch(f"{A}/drafts/{d['id']}", headers=ORIGIN,
                     json={"content": d["content"] + "\nNew rule.\n", "expected_revision": 1}).json()
    d = client.post(f"{A}/drafts/{d['id']}/submit", headers=ORIGIN, json={"expected_revision": 2}).json()
    client.post(f"{A}/drafts/{d['id']}/review", headers=ORIGIN, json={"decision": "approve", "digest": d["digest"]})
    (hub / "AGENTS.md").write_text((hub / "AGENTS.md").read_text() + "\nEdited in Claude Code meanwhile.\n")
    stale = client.post(f"{A}/drafts/{d['id']}/publish", headers=ORIGIN, json={"digest": d["digest"]})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "stale_base"
    assert "Edited in Claude Code meanwhile." in (hub / "AGENTS.md").read_text()


def test_publish_is_one_at_a_time_and_recovers_after_a_crash(client, hub):
    from hubzoid.chat.authoring import drafts

    svc = client.app.state.chat.authoring
    base = (hub / "AGENTS.md").read_text()

    def approved(text):
        d = client.post(f"{A}/drafts", headers=ORIGIN, json={"path": "AGENTS.md", "content": text}).json()
        d = client.post(f"{A}/drafts/{d['id']}/submit", headers=ORIGIN, json={"expected_revision": 1}).json()
        return client.post(f"{A}/drafts/{d['id']}/review", headers=ORIGIN,
                           json={"decision": "approve", "digest": d["digest"]}).json()

    a, b = approved(base + "\nA\n"), approved(base + "\nB\n")
    # A crash after the file was written but before the version was recorded:
    with svc.engine.begin() as conn:
        conn.execute(drafts.update().where(drafts.c.id == a["id"]).values(state="publishing"))
    (hub / "AGENTS.md").write_text(base + "\nA\n")
    busy = client.post(f"{A}/drafts/{b['id']}/publish", headers=ORIGIN, json={"digest": b["digest"]})
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "publish_in_progress"
    done = client.post(f"{A}/drafts/{a['id']}/publish", headers=ORIGIN, json={"digest": a["digest"]})
    assert done.status_code == 200 and (hub / "AGENTS.md").read_text().endswith("\nA\n")
    late = client.post(f"{A}/drafts/{b['id']}/publish", headers=ORIGIN, json={"digest": b["digest"]})
    assert late.status_code == 409 and late.json()["detail"]["code"] == "stale_base"  # B began from the old file


def test_validation_paths_and_permissions(client, monkeypatch):
    bad_skill = client.post(f"{A}/drafts", headers=ORIGIN, json={"path": "skills/stock/SKILL.md",
                                                                  "content": "---\ndescription: no name\n---\nBody\n"}).json()
    assert bad_skill["problems"]
    sub = client.post(f"{A}/drafts/{bad_skill['id']}/submit", headers=ORIGIN, json={"expected_revision": 1})
    assert sub.status_code == 422
    code = client.post(f"{A}/drafts", headers=ORIGIN, json={"path": "skills/stock/run.py", "content": "print(1)"}).json()
    assert code["problems"][0]["code"] == "not_text"  # code is never authored here
    for path in ("../outside.md", "tools_local/x.py", ".env", "knowledge/x.md"):
        assert client.post(f"{A}/drafts", headers=ORIGIN, json={"path": path, "content": "x"}).status_code == 422, path
    as_people(monkeypatch)
    assert client.get(f"{A}/files", headers=who("ana")).status_code == 403
    assert client.post(f"{A}/drafts", headers={**who("ana"), **ORIGIN}, json={"path": "AGENTS.md"}).status_code == 403


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


def test_openapi_import_converts_only_selected_operations(client):
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


def test_a_publication_is_one_commit_like_a_hub_change(client, hub):
    import subprocess

    git = lambda *a: subprocess.run(["git", "-C", str(hub), *a], capture_output=True, text=True, check=True).stdout
    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "start")
    before = (hub / "AGENTS.md").read_text()
    d = _approve(client, "AGENTS.md", new=before + "\nA reviewed rule.\n")
    pub = client.post(f"{A}/drafts/{d['id']}/publish", headers=ORIGIN, json={"digest": d["digest"]}).json()
    assert pub["commit_sha"] and git("rev-parse", "HEAD").strip() == pub["commit_sha"]
    body = git("log", "-1", "--format=%B")
    assert f"Hubzoid-Workspace-Draft: {hub.name.lower()}#{d['id']}" in body
    assert git("status", "--porcelain", "--", "AGENTS.md") == ""  # nothing left uncommitted


def test_flat_and_differently_spelt_skill_folders_are_listed(client, hub):
    import shutil

    if (hub / "skills").exists():
        shutil.move(str(hub / "skills"), str(hub / "Skill"))
    (hub / "Skill").mkdir(exist_ok=True)
    (hub / "Skill" / "triage.md").write_text("---\nname: triage\ndescription: Sort tickets\n---\nSort them.\n")
    files = {f["path"]: f["kind"] for f in client.get(f"{A}/files").json()["items"]}
    assert files.get("Skill/triage.md") == "skill"
    bad = client.post(f"{A}/drafts", headers=ORIGIN, json={"path": "Skill/triage.md"}).json()
    bad = client.patch(f"{A}/drafts/{bad['id']}", headers=ORIGIN,
                       json={"content": "---\nname: x\n---\nno description\n", "expected_revision": 1}).json()
    assert any(p["code"] == "invalid_frontmatter" for p in bad["problems"])  # checked like SKILL.md
    # A new skill goes into the folder the hub already has.
    d = _approve(client, "skills/notes/SKILL.md", content="---\nname: notes\ndescription: Notes\n---\nTake notes.\n")
    client.post(f"{A}/drafts/{d['id']}/publish", headers=ORIGIN, json={"digest": d["digest"]})
    assert (hub / "Skill" / "notes" / "SKILL.md").is_file() and not (hub / "skills").exists()


def test_broken_frontmatter_is_explained_in_plain_words():
    from hubzoid.chat.authoring import validate

    problems = validate("AGENTS.md", "---\nname: a\ndescription: x\nbad line: : here\n  oops\n---\nBody.\n")
    assert problems and "settings block" in problems[0]["message"]
    assert "while scanning" not in problems[0]["message"] and "unicode string" not in problems[0]["message"]
