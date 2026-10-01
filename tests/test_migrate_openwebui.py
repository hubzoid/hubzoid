"""``hubzoid migrate openwebui`` against databases built by Open WebUI 0.11.4 itself.

The fixture runs Open WebUI's own migrations and models in a subprocess (it
configures itself from the environment at import), so every row has the exact
shape a real install writes: users and credentials, groups, model access,
chats with branches and 1.0.x markup, attachments, shares and share access.
It is built once per content hash and cached by pytest. All people are synthetic.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from hubzoid import deployment
from hubzoid import migrate_openwebui as mig

BUILD = r'''
import asyncio, base64, json, sys, time, uuid
from pathlib import Path

out = Path(sys.argv[1])
from alembic import command
from alembic.config import Config
from open_webui.env import OPEN_WEBUI_DIR

cfg = Config(str(OPEN_WEBUI_DIR / "alembic.ini"))
cfg.set_main_option("script_location", str(OPEN_WEBUI_DIR / "migrations"))
command.upgrade(cfg, "head")

import bcrypt
from argon2 import PasswordHasher
from sqlalchemy import text
from open_webui.internal.db import engine, async_engine
from open_webui.models.users import Users
from open_webui.models.groups import Groups, GroupForm
from open_webui.models.models import Models, ModelForm, ModelMeta, ModelParams
from open_webui.models.chats import Chats, ChatForm
from open_webui.models.files import Files, FileForm
from open_webui.models.access_grants import AccessGrants

NS = uuid.UUID("5b0c6b0e-8a4e-4a39-9d7a-6a4bcf1f0a10")
def uid(name):
    return str(uuid.uuid5(NS, name))

T0 = int(time.time()) - 3 * 86400
manifest = {"users": {}, "passwords": {}, "groups": {}, "chats": {}, "messages": {}, "files": {}, "shares": {}}

PEOPLE = [
    ("admin", "Admin@Example.com", "Ada Admin", "admin", ("bcrypt", "admin-pass-1"), None, True),
    ("alice", "alice@example.com", "Alice", "user", ("bcrypt", "alice-pass-1"), None, True),
    ("bob", "bob@example.com", "Bob", "user", ("argon2", "bob-pass-1"), None, True),
    ("carol", "carol@example.com", "Carol", "pending", ("bcrypt", "carol-pass-1"), None, True),
    ("dave", "dave@example.com", "Dave", "user", ("bcrypt", "google-only-random"), {"google": {"sub": "google-sub-123"}}, True),
    ("erin", "erin@example.com", "Erin", "user", None, {"microsoft": {"sub": "ms-sub-7"}}, None),
    ("frank", "frank@example.com", "Frank", "user", ("bcrypt", "frank-pass-1"), None, False),
    ("olivia", "olivia@example.com", "Olivia", "user", ("bcrypt", "olivia-pass-1"),
     {"oidc": {"sub": "oidc-sub-9"}, "github": {"sub": "gh-1"}}, True),
]


def msg(mid, parent, role, content="", ts=None, **kw):
    return {"id": mid, "parentId": parent, "childrenIds": [], "role": role, "content": content,
            "timestamp": ts or T0, **kw}


def assistant(mid, parent, model, body, reasoning=None, ts=None, **kw):
    output = []
    if reasoning is not None:
        output.append({"type": "reasoning", "id": "rs_" + mid[:8], "status": "completed",
                       "start_tag": "<think>", "end_tag": "</think>", "attributes": {},
                       "content": [{"type": "output_text", "text": reasoning}], "summary": None,
                       "duration": 1, "started_at": T0, "ended_at": T0 + 1})
    output.append({"type": "message", "id": "msg_" + mid[:8], "status": "completed", "role": "assistant",
                   "content": [{"type": "output_text", "text": body}]})
    kw.setdefault("done", True)
    return msg(mid, parent, "assistant", "", ts=ts, model=model, modelName=model, output=output, **kw)


def chat_json(title, model, msgs, current):
    by = {m["id"]: m for m in msgs}
    for m in msgs:
        if m["parentId"] in by:
            by[m["parentId"]]["childrenIds"].append(m["id"])
    branch, cur = [], current
    while cur:
        branch.append(by[cur])
        cur = by[cur]["parentId"]
    return {"id": "", "title": title, "models": [model], "params": {},
            "history": {"messages": by, "currentId": current},
            "messages": list(reversed(branch)), "tags": [], "files": [], "timestamp": T0 * 1000}


async def main():
    ids = manifest["users"]
    for key, email, name, role, password, oauth, active in PEOPLE:
        ids[key] = uid("user-" + key)
        assert await Users.insert_new_user(ids[key], name, email, role=role, oauth=oauth)
        if password is not None:
            kind, plain = password
            hashed = (bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=4)).decode() if kind == "bcrypt"
                      else PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(plain))
            with engine.begin() as c:
                c.execute(text("INSERT INTO auth (id, email, password, active) VALUES (:i, :e, :p, :a)"),
                          {"i": ids[key], "e": email, "p": hashed, "a": active})
            manifest["passwords"][key] = plain
    groups = manifest["groups"]
    for name, members in (("finance-team", ["alice", "carol", "dave"]), ("ops-team", ["bob", "frank"]),
                          ("ledger", ["alice", "bob"]), ("reports", ["bob"]), ("empty-group", [])):
        group = await Groups.insert_new_group(ids["admin"], GroupForm(name=name, description=name + " group"))
        groups[name] = group.id
        if members:
            assert await Groups.add_users_to_group(group.id, [ids[m] for m in members])
    assert await Models.insert_new_model(ModelForm(
        id="finance", name="Finance", meta=ModelMeta(), params=ModelParams(),
        access_grants=[{"principal_type": "group", "principal_id": groups["finance-team"], "permission": "read"}]),
        ids["admin"])
    assert await Models.insert_new_model(ModelForm(
        id="ops", name="Ops", meta=ModelMeta(), params=ModelParams(),
        access_grants=[{"principal_type": "group", "principal_id": groups["ops-team"], "permission": "read"},
                       {"principal_type": "user", "principal_id": ids["erin"], "permission": "read"}]),
        ids["admin"])
    assert await Models.insert_new_model(ModelForm(
        id="finance-helper", base_model_id="finance", name="Finance helper", meta=ModelMeta(),
        params=ModelParams(), access_grants=[]), ids["admin"])

    chats, mids = manifest["chats"], manifest["messages"]
    def m(name):
        mids[name] = uid("msg-" + name)
        return mids[name]

    async def add(name, owner, title, model, msgs, current, **extra):
        chats[name] = uid("chat-" + name)
        assert await Chats.insert_new_chat(chats[name], ids[owner], ChatForm(chat=chat_json(title, model, msgs, current)), **extra)
        return chats[name]

    # 1. Branches, reasoning placeholder, compact tool block, full tool line, error, math, mermaid.
    c1 = [msg(m("c1u1"), None, "user", "What is our revenue?", models=["finance"]),
          assistant(m("c1a1"), mids["c1u1"], "finance",
                    "\n\n<details>\n<summary>↳ read_knowledge</summary>\n\n`name=revenue`\n\n</details>\n\n"
                    "Revenue grew. The formula is $$r = p q$$.", reasoning="_Thinking…_", ts=T0 + 10),
          assistant(m("c1a2"), mids["c1u1"], "finance",
                    "Second try.\n\n```mermaid\ngraph TD; A-->B\n```", reasoning="Let me think again.", ts=T0 + 20),
          msg(m("c1u2"), mids["c1a2"], "user", "And the ledger?", ts=T0 + 30),
          assistant(m("c1a3"), mids["c1u2"], "finance",
                    "\n\n> ↳ **ledger_lookup** `account=4000`\n\n\n\n> ⚠ **ledger_lookup** Permission denied\n\n"
                    "I could not read the ledger.", ts=T0 + 40)]
    await add("branches", "alice", "Revenue", "finance", c1, mids["c1a3"])

    # 2. Attachments: in the chat's uploads folder, only in Open WebUI storage, missing, inline image, collection.
    report_id, missing_id, notes_id = uid("file-report"), uid("file-missing"), uid("file-notes")
    manifest["files"] = {"report": report_id, "missing": missing_id, "notes": notes_id}
    uploads = out / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    pdf = b"%PDF-1.4 synthetic report\n"
    (uploads / f"{report_id}_report.pdf").write_bytes(pdf)
    await Files.insert_new_file(ids["alice"], FileForm(id=report_id, filename="report.pdf",
        path=str(uploads / f"{report_id}_report.pdf"), meta={"name": "report.pdf", "content_type": "application/pdf", "size": len(pdf)}))
    await Files.insert_new_file(ids["alice"], FileForm(id=missing_id, filename="missing.docx",
        path=str(uploads / f"{missing_id}_missing.docx"), meta={"name": "missing.docx", "size": 5}))
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    files = [
        {"type": "file", "id": notes_id, "name": "notes.txt", "size": 11, "url": f"/api/v1/files/{notes_id}",
         "file": {"id": notes_id, "filename": "notes.txt", "meta": {"name": "notes.txt", "content_type": "text/plain", "size": 11}}},
        {"type": "file", "id": report_id, "name": "report.pdf", "size": len(pdf), "url": f"/api/v1/files/{report_id}",
         "content_type": "application/pdf"},
        {"type": "file", "id": missing_id, "name": "missing.docx", "size": 5, "url": f"/api/v1/files/{missing_id}"},
        {"type": "image", "url": "data:image/png;base64," + base64.b64encode(png).decode()},
        {"type": "collection", "id": "kb-1", "name": "Handbook"},
    ]
    c2 = [msg(m("c2u1"), None, "user", "Please read these.", files=files),
          assistant(m("c2a1"), mids["c2u1"], "finance", "Read them.", ts=T0 + 5)]
    await add("files", "alice", "Attachments", "finance", c2, mids["c2a1"])

    # 3. 1.0.0 markup in plain content: <think>, full-format lines, fenced code, and a legacy share row.
    c3 = [msg(m("c3u1"), None, "user", "Search please"),
          msg(m("c3a1"), mids["c3u1"], "assistant",
              "<think>\nI should search the index.\n</think>\n\n> ✓ **search** `query=quarterly numbers`\n\n"
              "> ↳ **list_files**\n\nHere is what I found.\n\n```md\n> ↳ **not_a_tool** `x=1`\n```\n",
              model="ops", done=True, ts=T0 + 5)]
    legacy_chat = await add("legacy", "bob", "Search", "ops", c3, mids["c3a1"])
    token = uid("share-legacy")
    assert await Chats.insert_new_chat(token, "shared-" + legacy_chat, ChatForm(chat=chat_json("Search", "ops", c3, mids["c3a1"])))
    await Chats.update_chat_share_id_by_id(legacy_chat, token)
    manifest["shares"]["legacy"] = token

    # 4. Archived and pinned.
    c4 = [msg(m("c4u1"), None, "user", "Old question"), assistant(m("c4a1"), mids["c4u1"], "ops", "Old answer")]
    archived = await add("archived", "bob", "Archived chat", "ops", c4, mids["c4a1"])
    await Chats.toggle_chat_archive_by_id(archived)
    await Chats.toggle_chat_pinned_by_id(archived)

    # 5. Shared to everyone signed in, then continued (the link keeps the snapshot).
    c5 = [msg(m("c5u1"), None, "user", "Share this"), assistant(m("c5a1"), mids["c5u1"], "finance", "Shared answer")]
    shared = await add("shared", "dave", "Shared chat", "finance", c5, mids["c5a1"])
    result = await Chats.insert_shared_chat_by_chat_id(shared)
    manifest["shares"]["shared"] = result.share_id
    await AccessGrants.set_access_grants("shared_chat", shared, [{"principal_type": "user", "principal_id": "*", "permission": "read"}])
    later = c5 + [msg(m("c5u2"), mids["c5a1"], "user", "After sharing", ts=T0 + 50),
                  msg(m("c5a2"), mids["c5u2"], "assistant", "", ts=T0 + 60, model="finance", done=True, output=[
                      {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "calendar_list",
                       "arguments": "{\"day\": \"mon\"}", "status": "completed"},
                      {"type": "function_call_output", "call_id": "call_9", "output": [{"type": "input_text", "text": "[]"}], "status": "completed"},
                      {"type": "message", "id": "msg_c5a2", "status": "completed", "role": "assistant",
                       "content": [{"type": "output_text", "text": "Nothing on Monday."}]}])]
    await Chats.update_chat_by_id(shared, chat_json("Shared chat", "finance", later, mids["c5a2"]))

    # 6. A clone of chat 1: same message ids in another conversation.
    clone = uid("chat-clone")
    chats["clone"] = clone
    assert await Chats.insert_new_chat(clone, ids["alice"], ChatForm(chat=chat_json("Revenue (copy)", "finance", c1, mids["c1a3"])))

    # 7. A pending person's chat.
    await add("pending", "carol", "Pending chat", "finance",
              [msg(m("c7u1"), None, "user", "Hello"), assistant(m("c7a1"), mids["c7u1"], "finance", "Hi")], mids["c7a1"])

    # 8. A model this deployment does not serve.
    await add("unknown", "alice", "Old agent", "retired-agent",
              [msg(m("c8u1"), None, "user", "Hi old"), assistant(m("c8a1"), mids["c8u1"], "retired-agent", "Bye")], mids["c8a1"])

    # 9. A system message at the root.
    await add("system", "admin", "With system", "finance",
              [msg(m("c9s0"), None, "system", "You are helpful"), msg(m("c9u1"), mids["c9s0"], "user", "Hi"),
               assistant(m("c9a1"), mids["c9u1"], "finance", "Hello")], mids["c9a1"])

    # 10. An error and an interrupted answer.
    await add("errors", "erin", "Errors", "ops",
              [msg(m("c10u1"), None, "user", "Try"),
               msg(m("c10a1"), mids["c10u1"], "assistant", "", model="ops", done=True, error={"content": "Upstream failed"}),
               msg(m("c10u2"), mids["c10a1"], "user", "Again", ts=T0 + 20),
               assistant(m("c10a2"), mids["c10u2"], "ops", "Partial ans", ts=T0 + 30, done=False)], mids["c10a2"])

    # 11. An internal chat (notes).
    await add("internal", "alice", "Note helper", "finance",
              [msg(m("c11u1"), None, "user", "note")], mids["c11u1"], internal_meta={"internal": True, "type": "note"})

    # 12. Older Open WebUI reasoning and tool details in content.
    await add("details", "olivia", "Details", "ops",
              [msg(m("c12u1"), None, "user", "Explain"),
               msg(m("c12a1"), mids["c12u1"], "assistant",
                   '<details type="reasoning" done="true" duration="2">\n<summary>Thought for 2 seconds</summary>\n'
                   '> step one\n> step two\n</details>\n'
                   '<details type="tool_calls" done="true" id="call_1" name="web_search" '
                   'arguments="{&quot;q&quot;: &quot;x&quot;}" result="&quot;ok&quot;">\n<summary>Tool Executed</summary>\n</details>\n'
                   'Final answer with \\(a+b\\).', model="ops", done=True, ts=T0 + 5)], mids["c12a1"])

    # 13. A workspace model built on the finance agent.
    await add("workspace", "alice", "Helper", "finance-helper",
              [msg(m("c13u1"), None, "user", "Help"),
               assistant(m("c13a1"), mids["c13u1"], "finance-helper", "Helped")], mids["c13a1"])

    await async_engine.dispose()
    with engine.connect() as c:
        c.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print("OWUI_FIXTURE_OK")

asyncio.run(main())
'''


def _owui_env(root: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATABASE_URL", "DATA_DIR", "HUBZOID_DEPLOYMENT", "DATABASE_SCHEMA")}
    env.update(DATA_DIR=str(root), DATABASE_URL="sqlite:///" + str(root / "webui.db"),
               ENABLE_DB_MIGRATIONS="false", OFFLINE_MODE="true", HF_HUB_OFFLINE="1",
               WEBUI_SECRET_KEY="synthetic-migration-fixture", ENABLE_VERSION_UPDATE_CHECK="false")
    return env


@pytest.fixture(scope="session")
def owui_fixture(request) -> Path:
    """A cached Open WebUI 0.11.4 database built by Open WebUI's own code."""
    version = importlib.metadata.version("open-webui")
    key = hashlib.sha256((BUILD + version).encode()).hexdigest()[:16]
    root = Path(request.config.cache.mkdir("hubzoid-owui-migration")) / key
    if not (root / "manifest.json").is_file():
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        result = subprocess.run([sys.executable, "-c", BUILD, str(root)], env=_owui_env(root),
                                capture_output=True, text=True, timeout=600)
        assert result.returncode == 0 and "OWUI_FIXTURE_OK" in result.stdout, result.stderr[-6000:]
    return root


# ---------------------------------------------------------------------------
# Deployments around the fixture
# ---------------------------------------------------------------------------
_ENV_KEYS = ("DATABASE_URL", "DATABASE_SCHEMA", "HUBZOID_OPERATIONAL_DB", "HUBZOID_DEPLOYMENT",
             "HUBZOID_OWUI_DB", "BYPASS_MODEL_ACCESS_CONTROL", "HUBZOID_GATEWAY_ALLOW_BYPASS",
             "WEBUI_AUTH", "MODEL_LABEL", "OWUI_NATIVE_MCP", "OPENID_PROVIDER_URL",
             "MICROSOFT_CLIENT_TENANT_ID", "HUBZOID_GATEWAY_ADMIN_EMAIL", "HUBZOID_UI", "HUBZOID_AUTH")
PASSWORDS = {"admin": "admin-pass-1", "alice": "alice-pass-1", "bob": "bob-pass-1"}


class Deployment:
    def __init__(self, root: Path, owui: Path, op: Path, manifest: dict, entry: Path, hubs: dict):
        self.root, self.owui, self.op, self.m, self.entry, self.hubs = root, owui, op, manifest, entry, hubs

    def uid(self, name: str) -> str:
        return self.m["users"][name]

    def chat(self, name: str) -> str:
        return self.m["chats"][name]

    def msg(self, name: str) -> str:
        return self.m["messages"][name]

    def engine(self):
        return create_engine(f"sqlite:///{self.op}")

    def rows(self, sql: str, **params) -> list[dict]:
        eng = self.engine()
        try:
            with eng.connect() as conn:
                return [dict(r) for r in conn.execute(text(sql), params).mappings()]
        finally:
            eng.dispose()

    def store(self):
        from hubzoid.access.store import GrantStore

        return GrantStore(self.engine())


@pytest.fixture()
def clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    import hubzoid.access as access

    access._stores.clear()


def _hub(path: Path, name: str, restricted: tuple[str, ...] = ()) -> Path:
    (path / "restricted").mkdir(parents=True)
    (path / "AGENTS.md").write_text(f"---\nname: {name}\ndescription: {name} helper\n---\nYou help.\n")
    for perm in restricted:
        (path / "restricted" / f"{perm}.py").write_text("# restricted tool\n")
    return path


@pytest.fixture()
def gateway(tmp_path, owui_fixture, clean_env) -> Deployment:
    """Two hubs behind one gateway and one Open WebUI, as `hubzoid gateway` registers them."""
    manifest = json.loads((owui_fixture / "manifest.json").read_text())
    gw = tmp_path / "gateway-data"
    gw.mkdir()
    shutil.copy2(owui_fixture / "webui.db", gw / "webui.db")
    shutil.copytree(owui_fixture / "uploads", gw / "uploads")
    finance = _hub(tmp_path / "finance", "Finance", ("ledger",))
    ops = _hub(tmp_path / "ops", "Ops", ("reports",))
    deployment.save(gw / "deployment.json",
                    hubs=[dict(key="finance", name="Finance", path=str(finance), model_id="finance"),
                          dict(key="ops", name="Ops", path=str(ops), model_id="ops")],
                    operational_url=f"sqlite:///{gw / 'operational.db'}", owui_url="http://127.0.0.1:9",
                    owui_db=str(gw / "webui.db"), owui_database_url=f"sqlite:///{gw / 'webui.db'}")
    # The 1.0.x bridge copied this attachment into the chat's uploads folder at chat time.
    uploads = finance / ".hubzoid" / "chats" / manifest["chats"]["files"] / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "notes.txt").write_text("hello notes")
    return Deployment(tmp_path, gw / "webui.db", gw / "operational.db", manifest, finance,
                      {"finance": finance, "ops": ops})


def _run(dep: Deployment, apply: bool = False, **kw) -> mig.Report:
    return mig.run(mig.locate(dep.entry, aliases=kw.pop("aliases", None)), apply=apply, **kw)


def _tree(root: Path) -> dict[str, str]:
    """Every file and its hash, except SQLite's shared-memory and log files,
    which a read-only reader may create next to a database in WAL mode."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.name.endswith(("-shm", "-wal")):
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _group_aware_store(monkeypatch) -> None:
    """Contract 6.7 (lane E): a grant to group:<id> applies to every member.
    Emulated only when this build's store does not do it itself."""
    if mig.store_resolves_groups():
        return
    from hubzoid.access.identity import normalize
    from hubzoid.access.store import GrantStore

    original = GrantStore.can

    def can(self, subject, hub, action):
        subject = normalize(subject)
        if not subject or self.is_suspended(subject):
            return False
        if original(self, subject, hub, action):
            return True
        with self._engine.connect() as conn:
            groups = [g for (g,) in conn.execute(
                text("SELECT group_id FROM hz_group_members WHERE email=:e"), {"e": subject})]
        return any(original(self, f"group:{g}", hub, action) for g in groups)

    monkeypatch.setattr(GrantStore, "can", can)
    assert mig.store_resolves_groups()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_dry_run_changes_nothing(gateway):
    before = _tree(gateway.root)
    report = _run(gateway)
    assert _tree(gateway.root) == before
    assert not gateway.op.exists()
    assert report.blocking == [] and report.applied is False
    counts = report.counts
    assert counts["users"]["to_import"] == 8
    assert counts["conversations"]["to_import"] == 11
    assert counts["files"]["copied_from_open_webui"] == 1  # would copy: nothing written
    assert report.access["finance"]["state"] == "convert"
    assert report.access["finance"]["differences"] == 0


def test_apply_gateway_moves_people_groups_access_and_chats(gateway):
    report = _run(gateway, apply=True)
    assert report.applied and report.blocking == []
    d = gateway

    # People: same ids, normalized emails, roles, pending, passwords that verify.
    users = {r["id"]: r for r in d.rows("SELECT * FROM hz_users")}
    assert set(users) == {d.uid(k) for k in ("admin", "alice", "bob", "carol", "dave", "erin", "frank", "olivia")}
    admin = users[d.uid("admin")]
    assert (admin["email"], admin["role"], admin["status"], admin["source"]) == \
        ("admin@example.com", "admin", "active", "migrated")
    carol = users[d.uid("carol")]
    assert (carol["role"], carol["status"]) == ("user", "pending")
    erin = users[d.uid("erin")]
    assert erin["password_hash"] is None and erin["password_enabled"] == 0
    from pwdlib import PasswordHash
    from pwdlib.hashers.argon2 import Argon2Hasher
    from pwdlib.hashers.bcrypt import BcryptHasher

    hasher = PasswordHash((Argon2Hasher(), BcryptHasher()))
    for name, password in PASSWORDS.items():
        assert users[d.uid(name)]["password_enabled"] == 1
        assert hasher.verify(password, users[d.uid(name)]["password_hash"]), name
    assert not hasher.verify("wrong", users[d.uid("alice")]["password_hash"])
    identities = {(r["provider"], r["issuer"], r["subject"]): r["user_id"]
                  for r in d.rows("SELECT * FROM hz_user_identities")}
    assert identities == {
        ("google", "https://accounts.google.com", "google-sub-123"): d.uid("dave"),
        ("microsoft", "openwebui-migrated:microsoft", "ms-sub-7"): d.uid("erin"),
        ("oidc", "openwebui-migrated:oidc", "oidc-sub-9"): d.uid("olivia"),
    }
    bound = {r["subject"]: r for r in d.rows("SELECT * FROM hz_identities WHERE owui_id IS NOT NULL")}
    assert {r["owui_id"] for r in bound.values()} == set(users)
    assert bound["carol@example.com"]["pending"] == 1
    store = d.store()
    assert store.is_suspended("frank@example.com")      # deactivated in Open WebUI
    assert store.is_suspended("carol@example.com")      # pending

    # Groups with members, same ids and names, members by email.
    groups = {r["id"]: r for r in d.rows("SELECT * FROM hz_groups")}
    assert {g["name"] for g in groups.values()} == {"finance-team", "ops-team", "ledger", "reports"}
    assert d.m["groups"]["empty-group"] not in groups
    members = {(r["group_id"], r["email"]) for r in d.rows("SELECT * FROM hz_group_members")}
    assert (d.m["groups"]["ledger"], "bob@example.com") in members and len(members) == 8

    # Access: Console-managed now; allowed and denied people exactly as in Open WebUI.
    for hub in ("finance", "ops"):
        assert store.is_authoritative(hub)
    assert store.can("alice@example.com", "finance", "use_hub")
    assert store.can("dave@example.com", "finance", "use_hub")
    assert store.can("admin@example.com", "finance", "use_hub")
    assert not store.can("bob@example.com", "finance", "use_hub")
    assert not store.can("carol@example.com", "finance", "use_hub")
    assert store.can("alice@example.com", "finance", "ledger")
    assert not store.can("bob@example.com", "finance", "ledger")   # tool group, but no hub access
    assert store.can("bob@example.com", "ops", "reports")
    assert store.can("erin@example.com", "ops", "use_hub")          # granted by name on the model
    assert not store.can("frank@example.com", "ops", "use_hub")
    assert not store.can("alice@example.com", "ops", "use_hub")
    for hub in ("finance", "ops"):
        summary = report.access[hub]
        assert summary["differences"] == 0 and summary["matrix_denied"] > 0
    assert len(report.backups) == 2 and all(Path(p).is_file() for p in report.backups)

    # Conversations: same ids, owner, hub, agent, archive state, current branch.
    convs = {r["id"]: r for r in d.rows("SELECT * FROM hz_conversations")}
    assert set(convs) == {d.chat(k) for k in ("branches", "files", "legacy", "archived", "shared", "clone",
                                              "pending", "system", "errors", "details", "workspace")}
    assert (convs[d.chat("workspace")]["hub"], convs[d.chat("workspace")]["agent"]) == ("finance", "finance")
    assert report.counts["conversations"]["agent_found_through_a_workspace_model"] == 1
    branches = convs[d.chat("branches")]
    assert (branches["owner_id"], branches["hub"], branches["agent"], branches["head_id"],
            branches["source"], branches["title_source"]) == \
        (d.uid("alice"), "finance", "finance", d.msg("c1a3"), "migrated", "migrated")
    assert convs[d.chat("archived")]["archived"] == 1
    assert convs[d.chat("legacy")]["hub"] == "ops"
    msgs = {r["id"]: r for r in d.rows("SELECT * FROM hz_messages WHERE conversation_id=:c",
                                       c=d.chat("branches"))}
    assert msgs[d.msg("c1a1")]["parent_id"] == msgs[d.msg("c1a2")]["parent_id"] == d.msg("c1u1")
    a1 = json.loads(msgs[d.msg("c1a1")]["content"])
    assert [p["type"] for p in a1] == ["reasoning", "tool-call", "text"]
    assert a1[0]["text"] == "" and a1[1]["toolName"] == "read_knowledge" and a1[1]["args"] == {"name": "revenue"}
    a3 = json.loads(msgs[d.msg("c1a3")]["content"])
    assert a3[0]["result"] == {"status": "error", "message": "Permission denied"}
    assert "Revenue grew" in msgs[d.msg("c1a1")]["text"]
    # A clone shares message ids with the original: its copies get their own ids.
    clone = d.rows("SELECT * FROM hz_messages WHERE conversation_id=:c", c=d.chat("clone"))
    assert len(clone) == 5 and not {r["id"] for r in clone} & set(msgs)
    assert convs[d.chat("clone")]["head_id"] in {r["id"] for r in clone}
    # A system message is left out and its child becomes a root.
    system = d.rows("SELECT id, parent_id, role FROM hz_messages WHERE conversation_id=:c", c=d.chat("system"))
    assert sorted(r["role"] for r in system) == ["assistant", "user"]
    assert [r["parent_id"] for r in system if r["role"] == "user"] == [None]
    errors = {r["id"]: r for r in d.rows("SELECT * FROM hz_messages WHERE conversation_id=:c", c=d.chat("errors"))}
    assert errors[d.msg("c10a1")]["status"] == "error" and errors[d.msg("c10a1")]["error"] == "Upstream failed"
    assert errors[d.msg("c10a2")]["status"] == "cancelled"
    legacy = d.rows("SELECT content FROM hz_messages WHERE id=:i", i=d.msg("c3a1"))[0]
    parts = json.loads(legacy["content"])
    assert [p["type"] for p in parts] == ["reasoning", "tool-call", "tool-call", "text"]
    assert parts[0]["text"] == "I should search the index."
    assert parts[1]["args"] == {"query": "quarterly numbers"} and parts[2]["toolName"] == "list_files"
    assert "not_a_tool" in parts[3]["text"]
    details = json.loads(d.rows("SELECT content FROM hz_messages WHERE id=:i", i=d.msg("c12a1"))[0]["content"])
    assert details[0] == {"type": "reasoning", "text": "step one\nstep two"}
    assert details[1]["toolName"] == "web_search" and details[1]["args"] == {"q": "x"}

    # Attachments: kept, copied from Open WebUI storage, inline image extracted, missing reported.
    files_msg = json.loads(d.rows("SELECT content FROM hz_messages WHERE id=:i", i=d.msg("c2u1"))[0]["content"])
    kinds = [(p["type"], p.get("name")) for p in files_msg]
    assert kinds[0] == ("text", None)
    assert ("file", "notes.txt") in kinds and ("file", "report.pdf") in kinds and ("file", "missing.docx") in kinds
    image = next(p for p in files_msg if p["type"] == "image")
    folder = d.hubs["finance"] / ".hubzoid" / "chats" / d.chat("files") / "uploads"
    assert (folder / "report.pdf").read_bytes().startswith(b"%PDF")
    assert (folder / "report.pdf.hubzoid.json").is_file()
    assert (folder / image["file_id"]).is_file() and image["mime"] == "image/png"
    assert report.counts["files"] == {"copied_from_open_webui": 1, "extracted_inline_image": 1, "missing": 1,
                                      "non_file_attachments_not_imported": 1, "present": 1}

    # Share links keep their ids; the snapshot is the shared copy, not the continued chat.
    shares = {r["id"]: r for r in d.rows("SELECT * FROM hz_shares")}
    assert set(shares) == {d.m["shares"]["shared"], d.m["shares"]["legacy"]}
    snap = json.loads(shares[d.m["shares"]["shared"]]["snapshot"])
    assert [m["id"] for m in snap["messages"]] == [d.msg("c5u1"), d.msg("c5a1")]
    assert snap["audience"] == "signed_in" and shares[d.m["shares"]["shared"]]["owner_id"] == d.uid("dave")
    assert json.loads(shares[d.m["shares"]["legacy"]]["snapshot"])["audience"] == "owner"
    shared_msgs = d.rows("SELECT id, content FROM hz_messages WHERE conversation_id=:c", c=d.chat("shared"))
    assert len(shared_msgs) == 4
    call = json.loads(next(r["content"] for r in shared_msgs if r["id"] == d.msg("c5a2")))[0]
    assert (call["toolCallId"], call["toolName"], call["args"]) == ("call_9", "calendar_list", {"day": "mon"})

    # Skips and report counts.
    assert report.unknown_models == {"retired-agent": 1}
    skipped = report.skipped["conversations"]
    assert skipped == {"agent (model) not in this deployment": 1,
                       "internal Open WebUI chat (notes, automations)": 1}
    assert report.skipped["messages"] == {"role system not imported": 1}
    assert report.counts["conversations"]["pinned_now_normal"] == 1
    assert report.counts["content"] == {"assistant_messages_with_math": 3, "assistant_messages_with_mermaid": 2}
    assert report.counts["users"]["with_external_identity"] == 3
    marker = [r for r in d.rows("SELECT k, v FROM hz_meta WHERE k LIKE 'openwebui_migration:%'")]
    assert len(marker) == 1 and json.loads(marker[0]["v"])["converted_hubs"] == ["finance", "ops"]


def _dump(dep: Deployment) -> dict:
    tables = {"hz_users": "id", "hz_user_identities": "subject", "hz_identities": "subject",
              "hz_groups": "id", "hz_group_members": "group_id, email", "hz_conversations": "id",
              "hz_messages": "id", "hz_shares": "id", "hz_grants": "subject, hub, permission"}
    out = {t: dep.rows(f"SELECT * FROM {t} ORDER BY {order}") for t, order in tables.items()}
    out["hz_meta"] = [r for r in dep.rows("SELECT k, v FROM hz_meta ORDER BY k")
                      if not r["k"].startswith("openwebui_migration:")]
    return out


def test_apply_twice_is_idempotent(gateway):
    first = _run(gateway, apply=True)
    assert first.applied
    tree = _tree(gateway.root / "finance")
    before = _dump(gateway)
    second = _run(gateway, apply=True)
    assert second.applied and second.blocking == []
    assert _dump(gateway) == before
    assert _tree(gateway.root / "finance") == tree
    counts = second.counts
    assert counts["users"]["unchanged"] == 8 and "imported" not in counts["users"]
    assert counts["conversations"]["unchanged"] == 11
    assert counts["messages"].get("imported", 0) == 0 and counts["messages"].get("updated", 0) == 0
    assert counts["shares"]["already_present"] == 2
    assert counts["groups"]["already_present"] == 4
    assert all(a["state"] == "console-managed" for a in second.access.values())
    assert second.backups == []


def test_group_grants_keep_the_intersection(gateway, monkeypatch):
    """With group grants: hub visibility groups and exact tool groups become
    group grants; a tool group that holds people outside the hub stays per person."""
    _group_aware_store(monkeypatch)
    report = _run(gateway, apply=True)
    assert report.applied
    finance, ops = report.access["finance"], report.access["ops"]
    assert finance["strategy"] == "mixed" and finance["per_person_capabilities"] == ["ledger"]
    assert ops["strategy"] == "groups"
    assert finance["differences"] == ops["differences"] == 0
    grants = set(gateway.store().list_grants())
    g = gateway.m["groups"]
    assert (f"group:{g['finance-team']}", "finance", "use_hub") in grants
    assert (f"group:{g['ops-team']}", "ops", "use_hub") in grants
    assert (f"group:{g['reports']}", "ops", "reports") in grants
    assert (f"group:{g['ledger']}", "finance", "ledger") not in grants
    assert ("alice@example.com", "finance", "ledger") in grants
    store = gateway.store()
    assert not store.can("bob@example.com", "finance", "ledger")
    assert not store.can("bob@example.com", "finance", "use_hub")
    assert store.can("alice@example.com", "finance", "use_hub")
    assert store.can("bob@example.com", "ops", "reports")
    # Going forward, group membership decides: a new ops-team member can use Ops.
    with gateway.engine().begin() as conn:
        conn.execute(text("INSERT INTO hz_group_members (group_id, email, added_at) VALUES (:g, :e, 1)"),
                     {"g": g["ops-team"], "e": "alice@example.com"})
    assert gateway.store().can("alice@example.com", "ops", "use_hub")


def test_a_matrix_difference_refuses_apply(gateway, monkeypatch):
    from hubzoid.access import migrate as access_migrate

    original = access_migrate.plan_from_owui

    def lossy(*args, **kw):
        plan = original(*args, **kw)
        # Dave holds nothing else there, so without this grant he would lose Finance.
        plan.grants = [g for g in plan.grants if g != ("dave@example.com", "finance", "use_hub")]
        return plan

    monkeypatch.setattr(access_migrate, "plan_from_owui", lossy)
    report = _run(gateway, apply=True, grants_mode="people")
    assert not report.applied
    assert any("would change who may use what" in b for b in report.blocking)
    assert report.access["finance"]["differences"] > 0
    assert not gateway.op.exists()  # nothing written, not even the schema


def test_console_managed_hub_keeps_its_grants(gateway):
    from hubzoid.access.store import GrantStore

    store = GrantStore(gateway.engine())
    store.grant("frank@example.com", "ops", "use_hub", actor="console")
    store.set_authoritative(True, hub="ops")
    before = sorted(store.list_grants("ops"))
    report = _run(gateway, apply=True)
    assert report.applied
    assert report.access["ops"]["state"] == "console-managed"
    assert report.access["finance"]["state"] == "convert"
    assert sorted(gateway.store().list_grants("ops")) == before
    assert {r["name"] for r in gateway.rows("SELECT name FROM hz_groups")} >= {"ops-team", "reports"}


def test_an_existing_account_with_the_same_email_blocks(gateway):
    from hubzoid.access.store import GrantStore

    GrantStore(gateway.engine())  # schema
    with gateway.engine().begin() as conn:
        conn.execute(text("INSERT INTO hz_users (id, email, name, role, status, source, created_at, updated_at) "
                          "VALUES ('someone-else-1', 'alice@example.com', 'A', 'user', 'active', 'admin', 1, 1)"))
    report = _run(gateway, apply=True)
    assert not report.applied
    assert any("different id" in b for b in report.blocking)
    assert gateway.rows("SELECT count(*) AS n FROM hz_conversations")[0]["n"] == 0


def test_rerun_keeps_hubzoid_changes_and_does_not_restore_deletions(gateway):
    import time as _time

    assert _run(gateway, apply=True).applied
    now = _time.time()
    with gateway.engine().begin() as conn:
        # Alice changed her password in Hubzoid; the archived chat and a share were deleted there.
        conn.execute(text("UPDATE hz_users SET password_hash='$argon2id$v=19$m=8,t=1,p=1$c2FsdHNhbHQ$aGFzaA', "
                          "updated_at=:t WHERE id=:i"), {"t": now + 5, "i": gateway.uid("alice")})
        conn.execute(text("DELETE FROM hz_messages WHERE conversation_id=:c"), {"c": gateway.chat("archived")})
        conn.execute(text("DELETE FROM hz_conversations WHERE id=:c"), {"c": gateway.chat("archived")})
        conn.execute(text("DELETE FROM hz_shares WHERE id=:s"), {"s": gateway.m["shares"]["shared"]})
    # Meanwhile Open WebUI kept running: a new chat, and Carol was approved.
    owui = sqlite3.connect(gateway.owui)
    new_chat = {"title": "Later", "models": ["ops"], "history": {"currentId": "later-msg-0001", "messages": {
        "later-msg-0001": {"id": "later-msg-0001", "parentId": None, "role": "user", "content": "New question",
                           "timestamp": int(now) + 100}}}}
    owui.execute("INSERT INTO chat (id, user_id, title, chat, created_at, updated_at, archived, pinned, meta) "
                 "VALUES ('later-chat-0001', ?, 'Later', ?, ?, ?, 0, 0, '{}')",
                 (gateway.uid("bob"), json.dumps(new_chat), int(now) + 100, int(now) + 100))
    owui.execute("UPDATE user SET role='user' WHERE id=?", (gateway.uid("carol"),))
    # An answer edited in Open WebUI, Bob left the reports group, Dave joined it.
    raw = json.loads(owui.execute("SELECT chat FROM chat WHERE id=?", (gateway.chat("pending"),)).fetchone()[0])
    answer = raw["history"]["messages"][gateway.msg("c7a1")]
    answer["output"][-1]["content"][0]["text"] = "Hi there, edited"
    owui.execute("UPDATE chat SET chat=?, updated_at=? WHERE id=?",
                 (json.dumps(raw), int(now) + 100, gateway.chat("pending")))
    owui.execute("DELETE FROM group_member WHERE group_id=? AND user_id=?",
                 (gateway.m["groups"]["reports"], gateway.uid("bob")))
    owui.execute("INSERT INTO group_member (id, group_id, user_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                 ("gm-later-0001", gateway.m["groups"]["reports"], gateway.uid("dave"), int(now) + 100, int(now) + 100))
    owui.commit()
    owui.close()

    report = _run(gateway, apply=True)
    assert report.applied
    alice = gateway.rows("SELECT password_hash FROM hz_users WHERE id=:i", i=gateway.uid("alice"))[0]
    assert alice["password_hash"].startswith("$argon2id$v=19$m=8")
    assert report.counts["users"]["kept_changed_in_hubzoid"] == 1
    carol = gateway.rows("SELECT status FROM hz_users WHERE id=:i", i=gateway.uid("carol"))[0]
    assert carol["status"] == "active" and not gateway.store().is_suspended("carol@example.com")
    ids = {r["id"] for r in gateway.rows("SELECT id FROM hz_conversations")}
    assert gateway.chat("archived") not in ids and "later-chat-0001" in ids
    assert report.counts["conversations"]["deleted_in_hubzoid_not_restored"] == 1
    assert report.counts["shares"]["removed_in_hubzoid_not_restored"] == 1
    assert gateway.m["shares"]["shared"] not in {r["id"] for r in gateway.rows("SELECT id FROM hz_shares")}
    edited = gateway.rows("SELECT content FROM hz_messages WHERE id=:i", i=gateway.msg("c7a1"))[0]
    assert "Hi there, edited" in edited["content"]
    members = {r["email"] for r in gateway.rows("SELECT email FROM hz_group_members WHERE group_id=:g",
                                                 g=gateway.m["groups"]["reports"])}
    assert members == {"dave@example.com"}


def test_a_failed_data_step_undoes_the_access_step(gateway, monkeypatch):
    def boom(self, plan):
        raise RuntimeError("disk full")

    monkeypatch.setattr(mig.Writer, "groups", boom)
    with pytest.raises(RuntimeError):
        _run(gateway, apply=True)
    store = gateway.store()
    assert not store.is_authoritative("finance") and not store.is_authoritative("ops")
    assert store.list_grants("finance") == [] and store.list_grants("ops") == []
    assert gateway.rows("SELECT count(*) AS n FROM hz_users")[0]["n"] == 0
    assert gateway.rows("SELECT count(*) AS n FROM hz_meta WHERE k LIKE 'openwebui_migration:%'")[0]["n"] == 0
    monkeypatch.undo()
    assert _run(gateway, apply=True).applied  # and the next run succeeds


def test_skipped_chats_are_retried_later(gateway):
    first = _run(gateway, apply=True)
    assert first.unknown_models == {"retired-agent": 1}
    second = _run(gateway, apply=True, aliases={"retired-agent": "finance"})
    assert second.counts["conversations"]["to_import"] == 1
    row = gateway.rows("SELECT hub, agent FROM hz_conversations WHERE id=:c", c=gateway.chat("unknown"))
    assert row == [{"hub": "finance", "agent": "finance"}]


@pytest.fixture()
def standalone(tmp_path, owui_fixture, clean_env) -> Deployment:
    """A single hub with its own Open WebUI data (the `hubzoid run` layout)."""
    manifest = json.loads((owui_fixture / "manifest.json").read_text())
    hub = _hub(tmp_path / "solo", "Solo", ("ledger",))
    data = hub / ".openwebui-data"
    data.mkdir()
    shutil.copy2(owui_fixture / "webui.db", data / "webui.db")
    shutil.copytree(owui_fixture / "uploads", data / "uploads")
    (hub / ".env").write_text("MODEL_LABEL=finance\n")
    return Deployment(tmp_path, data / "webui.db", hub / ".hubzoid" / "hub.db", manifest, hub, {"solo": hub})


def test_standalone_rehearsal_on_a_copy(standalone, tmp_path):
    original = _tree(standalone.entry)
    scratch = tmp_path / "rehearsal"
    result = CliRunner().invoke(mig.migrate_app, ["openwebui", str(standalone.entry), "--rehearse",
                                                 str(scratch), "--apply", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["applied"] is True
    assert _tree(standalone.entry) == original and not standalone.op.exists()
    copy = Deployment(tmp_path, scratch / "solo" / ".openwebui-data" / "webui.db",
                      scratch / "solo" / ".hubzoid" / "hub.db", standalone.m, scratch / "solo", {})
    store = copy.store()
    # A single hub showed its agent to everyone signed in: that stays, for active accounts.
    assert ("*", "solo", "use_hub") in set(store.list_grants("solo"))
    assert store.can("bob@example.com", "solo", "use_hub")
    assert not store.can("carol@example.com", "solo", "use_hub")
    assert not store.can("frank@example.com", "solo", "use_hub")
    assert store.can("alice@example.com", "solo", "ledger") and store.can("bob@example.com", "solo", "ledger")
    assert not store.can("dave@example.com", "solo", "ledger")
    assert report["unknown_models"] == {"ops": 4, "retired-agent": 1}
    convs = copy.rows("SELECT hub, agent FROM hz_conversations")
    assert {(r["hub"], r["agent"]) for r in convs} == {("solo", "finance")}


def test_model_alias_imports_an_old_agent(standalone):
    report = _run(standalone, aliases={"ops": "finance", "retired-agent": "solo"})
    assert report.unknown_models == {}
    assert report.counts["conversations"]["to_import"] == 12


def test_report_has_no_content_and_no_email(gateway):
    runner = CliRunner()
    for args in (["--json"], ["--json", "--verbose"], []):
        result = runner.invoke(mig.migrate_app, ["openwebui", str(gateway.entry), *args])
        assert result.exit_code == 0, result.output
        out = result.stdout
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", out), "an email address in the report"
        for secret in ("revenue", "Upstream failed", "hello notes", "Permission denied", "step one",
                       "google-sub-123", "$2b$", "argon2"):
            assert secret not in out
    assert "Dry run: nothing was changed" in runner.invoke(
        mig.migrate_app, ["openwebui", str(gateway.entry)]).stdout


def test_gateway_data_folder_is_accepted(gateway):
    setup = mig.locate(gateway.owui.parent)
    assert setup.kind == "gateway" and [h.key for h in setup.hubs] == ["finance", "ops"]
    assert setup.public == {"finance": False, "ops": False}


def test_rehearse_refuses_a_gateway(gateway, tmp_path):
    result = CliRunner().invoke(mig.migrate_app, ["openwebui", str(gateway.entry), "--rehearse",
                                                 str(tmp_path / "x")])
    assert result.exit_code == 2 and "restored copy" in result.output


def test_postgres_operational_store(gateway, postgres_url):
    """The same migration into a PostgreSQL operational store, twice."""
    import uuid

    from sqlalchemy.engine import make_url

    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    name = "hzmig_" + uuid.uuid4().hex[:12]
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    url = make_url(postgres_url).set(database=name).render_as_string(hide_password=False)
    setup = mig.locate(gateway.entry, operational_db=url)
    dry = mig.run(setup)
    assert dry.blocking == [] and dry.counts["conversations"]["to_import"] == 11
    report = mig.run(setup, apply=True)
    assert report.applied and report.counts["messages"]["imported"] == 32
    again = mig.run(mig.locate(gateway.entry, operational_db=url), apply=True)
    assert again.applied and again.counts["conversations"]["unchanged"] == 11
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM hz_users")).scalar() == 8
            assert conn.execute(text("SELECT count(*) FROM hz_shares")).scalar() == 2
    finally:
        engine.dispose()


def test_postgres_source_is_read_only(postgres_url):
    """The Open WebUI engine on PostgreSQL reads in a read-only transaction."""
    import uuid

    from sqlalchemy.engine import make_url

    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    schema = "owui_" + uuid.uuid4().hex[:10]
    with admin.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f'CREATE TABLE "{schema}"."user" (id TEXT PRIMARY KEY, email TEXT, name TEXT, role TEXT)'))
        conn.execute(text(f'CREATE TABLE "{schema}".chat (id TEXT PRIMARY KEY, user_id TEXT, chat JSON)'))
        conn.execute(text(f'INSERT INTO "{schema}"."user" VALUES (\'u1\', \'a@example.com\', \'A\', \'user\')'))
    admin.dispose()
    engine = mig._source_engine(make_url(postgres_url).set(drivername="postgresql+psycopg"), schema)
    try:
        source = mig.OwuiSource(engine)
        assert set(source.users()) == {"u1"}
        with engine.connect() as conn:
            with pytest.raises(Exception, match="read-only"):
                conn.execute(text("INSERT INTO chat (id) VALUES ('x')"))
    finally:
        engine.dispose()


def test_a_different_file_with_the_same_name_is_kept_apart(gateway):
    folder = gateway.hubs["finance"] / ".hubzoid" / "chats" / gateway.chat("files") / "uploads"
    (folder / "report.pdf").write_bytes(b"a later, different report with the same name")
    report = _run(gateway, apply=True)
    assert report.applied
    parts = json.loads(gateway.rows("SELECT content FROM hz_messages WHERE id=:i",
                                    i=gateway.msg("c2u1"))[0]["content"])
    pdf = next(p for p in parts if p.get("name") == "report.pdf")
    tag = gateway.m["files"]["report"][:8]
    assert pdf["file_id"] == f"report ({tag}).pdf"
    assert (folder / pdf["file_id"]).read_bytes().startswith(b"%PDF")
    assert (folder / "report.pdf").read_bytes().startswith(b"a later")  # untouched


def test_history_only_in_the_chat_message_table(gateway):
    """Open WebUI 0.9+ also writes each message to chat_message. When a chat's
    own history is empty, the messages come from there."""
    owui = sqlite3.connect(gateway.owui)
    raw = json.loads(owui.execute("SELECT chat FROM chat WHERE id=?", (gateway.chat("pending"),)).fetchone()[0])
    raw["history"] = {"messages": {}, "currentId": None}
    raw["messages"] = []
    owui.execute("UPDATE chat SET chat=? WHERE id=?", (json.dumps(raw), gateway.chat("pending")))
    owui.commit()
    owui.close()
    report = _run(gateway, apply=True)
    assert report.counts["conversations"]["history_read_from_chat_message_table"] == 1
    rows = gateway.rows("SELECT id, parent_id, role, content FROM hz_messages WHERE conversation_id=:c",
                        c=gateway.chat("pending"))
    assert {r["id"] for r in rows} == {gateway.msg("c7u1"), gateway.msg("c7a1")}
    answer = next(r for r in rows if r["role"] == "assistant")
    assert answer["parent_id"] == gateway.msg("c7u1") and json.loads(answer["content"])[-1]["text"] == "Hi"
    head = gateway.rows("SELECT head_id FROM hz_conversations WHERE id=:c", c=gateway.chat("pending"))[0]
    assert head["head_id"] == gateway.msg("c7a1")


def test_older_open_webui_layouts(tmp_path, clean_env):
    """Before 0.6: `oauth_sub` on the user row, members as `group.user_ids`,
    model access as JSON, share copies as chat rows owned by `shared-<chat id>`."""
    hub = _hub(tmp_path / "old", "Old", ("ledger",))
    data = hub / ".openwebui-data"
    data.mkdir()
    history = {"messages": {"old-msg-0001": {"id": "old-msg-0001", "parentId": None, "role": "user",
                                             "content": "hi", "timestamp": 1700000000}},
               "currentId": "old-msg-0001"}
    con = sqlite3.connect(data / "webui.db")
    con.executescript("""
        CREATE TABLE user (id TEXT PRIMARY KEY, name TEXT, email TEXT, role TEXT, oauth_sub TEXT,
                           created_at INTEGER, updated_at INTEGER, last_active_at INTEGER);
        CREATE TABLE auth (id TEXT PRIMARY KEY, email TEXT, password TEXT, active INTEGER);
        CREATE TABLE "group" (id TEXT PRIMARY KEY, user_id TEXT, name TEXT, description TEXT,
                              user_ids TEXT, created_at INTEGER, updated_at INTEGER);
        CREATE TABLE model (id TEXT PRIMARY KEY, user_id TEXT, is_active INTEGER, access_control TEXT);
        CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT, title TEXT, chat TEXT, created_at INTEGER,
                           updated_at INTEGER, share_id TEXT, archived INTEGER);
    """)
    con.execute("INSERT INTO user VALUES ('old-user-0001', 'Grace', 'Grace@Example.com', 'user', "
                "'google@gsub-1', 1700000000, 1700000000, 1700000000)")
    con.execute("INSERT INTO user VALUES ('old-user-0002', 'Hal', 'hal@example.com', 'user', "
                "'abc-oidc-sub', 1700000001, 1700000001, 1700000001)")
    con.execute('INSERT INTO "group" VALUES (\'old-group-001\', \'old-user-0001\', \'ledger\', \'\', '
                '\'["old-user-0001"]\', 1700000000, 1700000000)')
    con.execute("INSERT INTO chat VALUES ('old-chat-0001', 'old-user-0001', 'Old', ?, 1700000000, "
                "1700000000, 'old-share-001', 0)", (json.dumps({"models": ["old"], "history": history}),))
    con.execute("INSERT INTO chat VALUES ('old-share-001', 'shared-old-chat-0001', 'Old', ?, 1700000000, "
                "1700000000, NULL, 0)", (json.dumps({"models": ["old"], "history": history}),))
    con.commit()
    con.close()
    (hub / ".env").write_text("MODEL_LABEL=old\n")
    report = mig.run(mig.locate(hub), apply=True)
    assert report.applied, report.blocking
    dep = Deployment(tmp_path, data / "webui.db", hub / ".hubzoid" / "hub.db", {}, hub, {})
    links = {(r["provider"], r["subject"]) for r in dep.rows("SELECT * FROM hz_user_identities")}
    assert links == {("google", "gsub-1"), ("oidc", "abc-oidc-sub")}
    assert dep.rows("SELECT email FROM hz_group_members") == [{"email": "grace@example.com"}]
    share = dep.rows("SELECT id, conversation_id, snapshot FROM hz_shares")
    assert [(s["id"], s["conversation_id"]) for s in share] == [("old-share-001", "old-chat-0001")]
    assert json.loads(share[0]["snapshot"])["audience"] == "signed_in"
    store = dep.store()
    assert store.can("grace@example.com", "old", "ledger") and not store.can("hal@example.com", "old", "ledger")
    assert store.can("hal@example.com", "old", "use_hub")


def test_an_unwritable_uploads_folder_is_reported_not_fatal(gateway):
    folder = gateway.hubs["finance"] / ".hubzoid" / "chats" / gateway.chat("files") / "uploads"
    folder.chmod(0o500)
    try:
        report = _run(gateway, apply=True)
    finally:
        folder.chmod(0o700)
    assert report.applied
    assert report.counts["files"]["could_not_be_written"] == 2   # the copied PDF and the inline image
    assert any("could not be written" in w for w in report.warnings)
    assert not list(folder.glob(".migrating-*"))
    parts = json.loads(gateway.rows("SELECT content FROM hz_messages WHERE id=:i",
                                    i=gateway.msg("c2u1"))[0]["content"])
    assert {p.get("name") for p in parts} >= {"report.pdf", "notes.txt", "missing.docx"}
