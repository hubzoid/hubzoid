"""Offline Open WebUI migration refusals, re-import decisions and attachment safety."""
from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from typer.testing import CliRunner

from hubzoid import migrate_openwebui as mig
from tests.test_migrate_openwebui import _hub, clean_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("clean_env")


def _insert(database, table, **row):
    with sqlite3.connect(database) as conn:
        keys = ", ".join(f'"{k}"' for k in row)
        conn.execute(f'INSERT INTO "{table}" ({keys}) VALUES ({", ".join("?" for _ in row)})',
                     tuple(row.values()))


@pytest.fixture
def setup(tmp_path):
    hub = _hub(tmp_path / "hub", "Helper")
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE user (id TEXT, email TEXT, name TEXT, role TEXT, oauth TEXT,
                               oauth_sub TEXT, created_at REAL, updated_at REAL, last_active_at REAL);
            CREATE TABLE auth (id TEXT, password TEXT, active INTEGER);
            CREATE TABLE chat (id TEXT, user_id TEXT, title TEXT, chat TEXT, created_at REAL,
                               updated_at REAL, share_id TEXT, archived INTEGER, pinned INTEGER,
                               meta TEXT, folder_id TEXT, current_message_id TEXT);
            CREATE TABLE shared_chat (id TEXT, chat_id TEXT, title TEXT, chat TEXT,
                                      created_at REAL, updated_at REAL);
            CREATE TABLE access_grant (resource_type TEXT, resource_id TEXT, principal_type TEXT,
                                       principal_id TEXT, permission TEXT);
            CREATE TABLE model (id TEXT, base_model_id TEXT);
            CREATE TABLE file (id TEXT, filename TEXT, path TEXT);
            CREATE TABLE chat_message (id TEXT, chat_id TEXT, role TEXT, parent_id TEXT,
                content TEXT, output TEXT, model_id TEXT, files TEXT, done INTEGER,
                error TEXT, usage TEXT, created_at REAL);
            CREATE TABLE alembic_version (version_num TEXT);
            INSERT INTO alembic_version VALUES ('fixture-revision');
        ''')
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    return mig.Setup(hub, "hub", [mig.HubInfo("helper", "Helper", hub, "helper")], {},
                     URL.create("sqlite", database=str(path)), None, uploads,
                     f"sqlite:///{tmp_path / 'target.db'}", env={})


@pytest.fixture
def source(setup):
    engine = mig._source_engine(setup.source_url, None)
    yield mig.OwuiSource(engine)
    engine.dispose()


def _user(uid="user0001", **values):
    return dict(id=uid, email=f"{uid}@example.test", name="Person", role="user",
                created_at=100, updated_at=110, **values)


def _people(setup, users, target=None, previous=None, auth=None):
    report = mig.Report("dry-run")
    people = mig.plan_people(setup, {u["id"]: u for u in users}, auth or {},
                             target or mig.TargetState(), previous, report)
    return people, report


def _importer(setup, source, *, target=None, previous=None):
    people, report = _people(setup, [_user()])
    return mig.ConversationImporter(setup, source, people, target or mig.TargetState(),
                                    previous, report, apply=False)


@pytest.mark.parametrize(("value", "expected"), [
    (True, 7), (None, 7), ("invalid", 7), ([], 7), (float("nan"), 7), (-1, 7),
    (1_700_000_000_000_000_000, 1_700_000_000),
])
def test_invalid_timestamps_and_nanoseconds(value, expected):
    assert mig._ts(value, 7) == expected


def test_decoding_and_safe_descriptions():
    assert mig._json(bytearray(b'"{\\"a\\":1}"')) == {"a": 1}
    assert mig._json(" ") is None
    assert mig._json("bad json") is None
    assert mig._json(json.dumps(json.dumps(json.dumps("still a string")))) is None
    assert mig._json_scalar(b'"name"') == "name"
    assert mig._json_scalar("ordinary text") == "ordinary text"
    assert mig._safe_name(None) == ""
    assert mig._safe_name("../") == ""
    assert mig._safe_name("C:\\x\\ report\x00.txt ") == "report.txt"
    assert mig._scrub("connect postgresql://u:secret@host/db") == "connect postgresql://***@host/db"
    assert mig._redact("bad url") == "(unreadable URL)"
    assert list(mig._chunks([1, 2, 3], 2)) == [[1, 2], [3]]


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+asyncpg"])
def test_source_postgres_driver_normalized(scheme):
    assert mig._normalize_source_url(f"{scheme}://u:p@host/db").drivername == "postgresql+psycopg"


@pytest.mark.parametrize(("url", "message"), [
    ("nonsense", "not a database URL"), ("mysql://host/db", "SQLite or PostgreSQL"),
])
def test_source_url_refusals(url, message):
    with pytest.raises(mig.MigrationBlocked, match=message):
        mig._normalize_source_url(url)


def test_settings_use_environment_then_hub_files(setup, monkeypatch):
    (setup.entry / ".env").write_text("FIRST=hub\nMICROSOFT_CLIENT_TENANT_ID=A1234567-1234-1234-1234-1234567890AB\n"
                                      "OPENID_PROVIDER_URL=https://sso.example/.well-known/openid-configuration\n")
    assert mig._setting(setup, "FIRST", {"FIRST": "env"}) == "env"
    assert mig._setting(setup, "FIRST", {"FIRST": ""}) == "hub"
    assert mig._setting(setup, "MISSING", {}) is None
    assert mig._issuer("microsoft", setup) == (
        "https://login.microsoftonline.com/a1234567-1234-1234-1234-1234567890ab/v2.0", True)
    assert mig._issuer("oidc", setup) == ("https://sso.example", True)
    import dotenv

    monkeypatch.setattr(dotenv, "dotenv_values", lambda path: (_ for _ in ()).throw(OSError("denied")))
    assert mig._hub_env(setup.entry) == {}


def test_legacy_oauth_deduplicates_and_ignores_invalid_links():
    assert mig._oauth_links({"oauth": {" Google ": {"sub": "g"}, "bad": [], "none": {"sub": ""}},
                             "oauth_sub": "google@g"}) == [("google", "g")]
    assert mig._oauth_links({"oauth_sub": "legacy-subject"}) == [("oidc", "legacy-subject")]


@pytest.mark.parametrize(("change", "reason"), [
    ({"id": "x" * 65}, "id longer than 64 characters"),
    ({"email": "invalid"}, "no usable email address"),
    ({"email": "x" * 320 + "@test"}, "no usable email address"),
])
def test_invalid_accounts_are_retryable(setup, change, reason):
    user = {**_user(), **change}
    people, report = _people(setup, [user])
    assert people.rows == {}
    assert people.skipped_ids == {user["id"]}
    assert report.skipped == {"users": {reason: 1}}


def test_duplicate_email_and_rebound_identity_block(setup):
    first = _user()
    second = {**_user("user0002"), "email": first["email"].upper()}
    people, report = _people(setup, [first, second])
    assert list(people.rows) == [first["id"]]
    assert "Two Open WebUI accounts" in report.blocking[0]
    target = mig.TargetState()
    target.identities[first["email"]] = {"owui_id": "other-id"}
    people, report = _people(setup, [first], target)
    assert people.skipped_ids == {first["id"]}
    assert "account was re-created" in report.blocking[0]


def test_deleted_and_conflicting_accounts_are_not_imported(setup):
    user = _user()
    people, report = _people(setup, [user], previous={"time": 200})
    assert people.not_restored == {user["id"]}
    assert people.removed_emails == {user["email"]}
    assert report.counts["users"]["deleted_in_hubzoid_not_restored"] == 1
    target = mig.TargetState()
    target.users_by_email[user["email"]] = {"id": "different-id"}
    people, report = _people(setup, [user], target)
    assert people.rows == {}
    assert "different id" in report.blocking[0]


def test_changed_account_is_kept_and_external_links_cannot_be_stolen(setup):
    user = _user(oauth={"google": {"sub": "already"}, "oidc": {"sub": "taken"},
                        "microsoft": {"sub": "new"}, "github": {"sub": "unsupported"}})
    target = mig.TargetState()
    stored = {**user, "name": "Edited locally", "status": "active", "updated_at": 300,
              "password_hash": None}
    target.users[user["id"]] = stored
    target.user_identities[(mig.GOOGLE_ISSUER, "already")] = user["id"]
    target.user_identities[("openwebui-migrated:oidc", "taken")] = "another-user"
    people, report = _people(setup, [user], target, {"time": 200})
    assert people.action == {user["id"]: "keep"}
    assert people.rows[user["id"]]["name"] == "Edited locally"
    assert people.identity_rows == []
    assert report.counts["identities"]["already_present"] == 1
    assert report.counts["identities"]["linked_to_another_account_not_imported"] == 1
    assert report.counts["identities"]["unsupported_provider_not_imported"] == 1


def test_reactivated_inactive_account_stays_active_and_unknown_role_is_unprivileged(setup):
    user = {**_user(), "role": "superuser"}
    target = mig.TargetState()
    target.users[user["id"]] = {**user, "role": "user", "status": "active", "password_hash": None,
                                "password_enabled": 0}
    target.meta["suspended:" + user["email"]] = "0"
    people, report = _people(setup, [user], target, {"time": 200},
                             {user["id"]: {"active": False, "password": "plaintext"}})
    assert people.to_block == set()
    assert people.action[user["id"]] == "same"
    assert report.counts["users"]["password_format_unrecognized"] == 1
    assert report.counts["users"]["unknown_role_imported_as_user"] == 1
    assert report.counts["users"]["deactivated_in_open_webui_reactivated_in_hubzoid"] == 1


def test_missing_owner_and_previously_imported_users_are_reported(setup):
    setup.env["HUBZOID_ADMIN_EMAIL"] = "owner@example.test"
    target = mig.TargetState()
    target.users["removed"] = {"source": mig.MIGRATED}
    target.users["unrelated"] = {"source": mig.MIGRATED}
    people, report = _people(setup, [], target, {"time": 200, "users": ["removed"]})
    assert people.owner_missing
    assert report.counts["users"]["imported_earlier_not_in_open_webui_now"] == 1
    assert any("won't exist" in warning for warning in report.warnings)
    assert report.ids["users"] == [{"id": "removed", "reason": "no longer in Open WebUI"}]


def test_source_reader_supports_old_tables_and_cached_file_rows(setup, source):
    path = setup.source_url.database
    _insert(path, "user", **_user())
    _insert(path, "file", id="file0001", filename="note.txt", path="/not/trusted")
    _insert(path, "model", id="workspace", base_model_id="helper")
    _insert(path, "chat_message", id="chat0001-message1", chat_id="chat0001", role="user",
            content='"hello"', files='[]', done=1, created_at=100)
    assert source.revision() == "fixture-revision"
    assert list(source.users()) == ["user0001"]
    assert source.auth() == {}
    assert source.model_bases() == {"workspace": "helper"}
    with source.engine.connect() as conn:
        assert source.chat_message_rows(conn, "chat0001")["message1"]["content"] == "hello"
        assert source.file_row(conn, "file0001")["filename"] == "note.txt"
        with sqlite3.connect(path) as writer:
            writer.execute("DELETE FROM file")
        assert source.file_row(conn, "file0001")["filename"] == "note.txt"
        assert source.file_row(conn, "missing") is None
    with sqlite3.connect(path) as writer:
        writer.executescript("DROP TABLE auth; DROP TABLE model; DROP TABLE alembic_version; DROP TABLE access_grant;")
    old = mig.OwuiSource(source.engine)
    assert old.revision() is None
    assert old.auth() == {}
    assert old.model_bases() == {}
    assert old.share_audiences()["any-chat"] == "signed_in"


def test_share_audiences_choose_broadest_read_or_write_grant(setup, source):
    for cid, ptype, principal, permission in [
        ("public", "anyone", "*", "read"), ("public", "group", "staff", "read"),
        ("signed", "user", "*", "write"), ("private", "user", "person", "read"),
        ("ignored", "anyone", "*", "admin"),
    ]:
        _insert(setup.source_url.database, "access_grant", resource_type="shared_chat", resource_id=cid,
                principal_type=ptype, principal_id=principal, permission=permission)
    audiences = source.share_audiences()
    assert [audiences[k] for k in ("public", "signed", "private", "ignored", "missing")] == [
        "anyone", "signed_in", "restricted", "owner", "owner"]


def test_unsupported_source_and_memory_target_refused(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    try:
        with pytest.raises(mig.MigrationBlocked, match="no user or chat table"):
            mig.OwuiSource(engine)
    finally:
        engine.dispose()
    with pytest.raises(mig.MigrationBlocked, match="file or server"):
        mig._target_engine("sqlite:///:memory:", readonly=True)


def test_attachment_fallback_names_missing_bytes_and_malformed_inline_data(setup, source, tmp_path):
    report = mig.Report("dry-run")
    attachments = mig.Attachments(source, setup.uploads_dir, report, False)
    folder = tmp_path / "chat-uploads"
    with source.engine.connect() as conn:
        parts = attachments.parts(conn, [None, {"type": "collection"},
            {"type": "image", "url": "data:text/plain,not-base64"},
            {"url": "data:image/png;base64,x"}, {},
            {"url": "/api/v1/files/12345678/content?q=1", "size": "unknown"}], folder)
    assert parts == [{"type": "file", "file_id": "attachment-12345678", "name": "attachment-12345678",
                      "mime": "application/octet-stream", "size": 0}]
    assert report.counts["files"] == {"non_file_attachments_not_imported": 1,
        "unreadable_inline_attachment_not_imported": 2, "unnamed_attachment_not_imported": 1, "missing": 1}
    assert not folder.exists()


def test_attachment_writes_verified_bytes_and_sidecar_and_preserves_existing_file(setup, source, tmp_path):
    folder = tmp_path / "chat-uploads"
    folder.mkdir()
    (folder / "notes.txt").write_bytes(b"original")
    report = mig.Report("apply")
    attachments = mig.Attachments(source, setup.uploads_dir, report, True)
    entry = {"name": "notes.txt", "url": "data:text/plain;base64," + base64.b64encode(b"new text").decode()}
    part = attachments.parts(None, [entry], folder)[0]
    assert part["file_id"] != "notes.txt"
    assert (folder / part["file_id"]).read_bytes() == b"new text"
    assert (folder / "notes.txt").read_bytes() == b"original"
    assert attachments.parts(None, [entry], folder) == [part]
    from hubzoid import uploads

    assert uploads.read_meta(folder, part["file_id"])["mime"] == "text/plain"
    assert report.counts["files"] == {"extracted_inline_image": 1, "present": 1}


def test_attachments_do_not_follow_storage_links_or_escape_names(setup, source, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (setup.uploads_dir / "file0001_note.txt").symlink_to(outside)
    _insert(setup.source_url.database, "file", id="file0001", filename="note.txt", path=str(outside))
    attachments = mig.Attachments(source, setup.uploads_dir, mig.Report("apply"), True)
    with source.engine.connect() as conn:
        assert attachments._owui_bytes(conn, "file0001", "note.txt") is None
    with pytest.raises(OSError, match="leaves the uploads folder"):
        attachments._write(tmp_path / "safe", "../escape", b"bad", None, "text/plain")
    assert not (tmp_path / "escape").exists()
    with pytest.raises(mig.MigrationBlocked, match="leave the rehearsal copy"):
        attachments._write(tmp_path / "other", "note.txt", b"bad", None, "text/plain", write_root=tmp_path / "copy")
    assert not (tmp_path / "other").exists()


def test_history_repairs_flat_lists_and_branch_cycles():
    messages, current = mig._history({"messages": [None, {"id": "one"}, {"id": "two"}]})
    assert current == "two" and messages["two"]["parentId"] == "one"
    assert mig._branch({"a": {"parentId": "b"}, "b": {"parentId": "a"}}, "a") == ["b", "a"]
    assert mig._user_parts({"content": [{"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}) == (
        [{"type": "text", "text": "hello"}], [{"type": "image", "url": "data:image/png;base64,AA=="}])
    assert mig._user_parts({"content": None}) == ([], [])


def test_output_parsing_retains_bad_tool_arguments_and_counts_unknown_items():
    stats = {}
    parts = mig.assistant_parts({"output": [None, {"type": "unknown"},
        {"type": "function_call", "name": "bad", "arguments": "not json", "status": "failed"},
        {"type": "function_call", "name": "list", "arguments": "[1,2]"},
        {"type": "message", "content": {"text": "hello"}}]}, "message1", stats)
    assert parts[0]["args"] == {"input": "not json"}
    assert parts[0]["result"] == {"status": "error", "message": ""}
    assert parts[1]["args"] == {"input": [1, 2]}
    assert parts[2] == {"type": "text", "text": "hello"}
    assert stats == {"unsupported_output_items": 1, "tool_errors": 1}


@pytest.mark.parametrize(("changes", "reason"), [
    ({"meta": '{"internal":true}'}, "internal Open WebUI chat (notes, automations)"),
    ({"id": "bad"}, "unsupported conversation id"),
    ({"user_id": "missing"}, "owner account missing in Open WebUI"),
    ({"chat": "invalid"}, "unreadable chat data"),
    ({"chat": '{"models":["retired"]}'}, "agent (model) not in this deployment"),
])
def test_unsafe_chats_and_their_shares_are_retryable(setup, source, changes, reason):
    importer = _importer(setup, source)
    chat = {"id": "chat0001", "user_id": "user0001", "chat": '{"models":["helper"]}', **changes}
    importer.shares_by_chat[chat["id"]] = ["share001"]
    with source.engine.connect() as conn:
        outcome = importer._one(chat, conn, None)
    assert outcome.action == "skip"
    assert importer.report.skipped["conversations"] == {reason: 1}
    assert importer.skipped["shares"] == {"share001"}


def test_conversation_collision_and_deleted_owner_refusals(setup, source):
    target = mig.TargetState()
    importer = _importer(setup, source, target=target)
    chat = {"id": "chat0001", "user_id": "user0001", "chat": '{"models":["helper"]}'}
    with source.engine.connect() as conn:
        target.conversations[chat["id"]] = {"source": "native"}
        assert importer._one(chat, conn, None).action == "skip"
        target.conversations[chat["id"]] = {"source": mig.MIGRATED, "owner_id": "other"}
        assert importer._one(chat, conn, None).action == "skip"
        importer.people.email_of.clear()
        importer.people.not_restored.add("user0001")
        assert importer._one(chat, conn, None).action == "skip"
        importer.people.not_restored.clear()
        importer.people.skipped_ids.add("user0001")
        assert importer._one(chat, conn, None).action == "skip"
    assert importer.report.skipped["conversations"] == {
        "a Hubzoid conversation already uses this id": 1, "existing conversation belongs to someone else": 1,
        "owner deleted in Hubzoid": 1, "owner not imported": 1}


def test_message_repair_status_usage_and_head_selection(setup, source):
    importer = _importer(setup, source)
    messages = {
        "system01": {"role": "system"},
        "user0001": {"role": "user", "parentId": "system01", "content": "hello"},
        "answer01": {"role": "assistant", "parentId": "unknown", "content": "$$x$$ ```mermaid",
                     "error": {"message": "denied"}, "usage": {"prompt_tokens": 4, "completion_tokens": 2}},
        "cycle001": {"role": "assistant", "parentId": "cycle002", "done": False, "usage": {"other": 1}},
        "cycle002": {"role": "assistant", "parentId": "cycle001"},
    }
    ids = {k: k for k in messages}
    with source.engine.connect() as conn:
        rows = importer._rows(conn, "chat0001", messages, ids, setup.entry / "uploads", setup.hubs[0], 100)
    by_id = {r["id"]: r for r in rows}
    assert by_id["user0001"]["parent_id"] is None
    assert by_id["answer01"]["parent_id"] is None
    assert by_id["answer01"]["status"] == "error" and by_id["answer01"]["error"] == "denied"
    assert json.loads(by_id["answer01"]["usage"]) == {"input_tokens": 4, "output_tokens": 2}
    assert by_id["cycle001"]["parent_id"] is None
    assert by_id["cycle001"]["status"] == "cancelled" and by_id["cycle001"]["usage"] is None
    assert importer._head(rows, "missing", ids) == "user0001"
    assert importer._head([], None, {}) is None
    assert importer.report.counts["messages"]["parent_cycle_broken"] == 1
    assert importer.report.counts["content"] == {"assistant_messages_with_math": 1, "assistant_messages_with_mermaid": 1}


def test_share_refusals_and_existing_snapshot_refresh(setup, source):
    target = mig.TargetState()
    importer = _importer(setup, source, target=target, previous={"time": 200})
    importer.shares_by_chat["chat0001"] = ["bad", "conflict", "removed1", "present1", "refresh1"]
    target.shares.update(conflict={"conversation_id": "other"}, present1={"conversation_id": "chat0001"},
                         refresh1={"conversation_id": "chat0001"})
    importer.share_meta["refresh1"] = {"created_at": 100, "updated_at": 300, "legacy": False}
    outcome = mig.ChatOutcome()
    with source.engine.connect() as conn:
        importer._shares({"user_id": "user0001", "updated_at": 100}, {"messages": [
            {"id": "system01", "role": "system"}, {"id": "user0001", "role": "user", "content": "hi"}]},
            "chat0001", {}, setup.hubs[0], conn, setup.entry / "uploads", "Title", outcome)
    assert len(outcome.shares) == 1
    action, row = outcome.shares[0]
    assert action == "update" and row["id"] == "refresh1"
    snapshot = json.loads(row["snapshot"])
    assert snapshot["head_id"] == "user0001"
    assert snapshot["messages"][0]["parent_id"] is None
    assert importer.report.counts["shares"]["already_present"] == 1
    assert importer.report.counts["shares"]["removed_in_hubzoid_not_restored"] == 1
    assert importer.skipped["shares"] == {"bad", "conflict"}


@pytest.mark.slow
def test_apply_rerun_updates_imported_rows_but_keeps_native_edits(setup):
    path = setup.source_url.database
    _insert(path, "user", **_user(oauth=json.dumps({"google": {"sub": "subject1"}})))
    messages = {"user0001": {"role": "user", "content": "Question"},
                "answer01": {"role": "assistant", "parentId": "user0001", "content": "Old answer"}}
    data = {"models": ["helper"], "history": {"messages": messages, "currentId": "answer01"}}
    _insert(path, "chat", id="chat0001", user_id="user0001", title="Original", chat=json.dumps(data),
            created_at=100, updated_at=110, share_id="share001")
    report = mig.run(setup, apply=True)
    assert report.applied and not report.blocking
    engine = create_engine(setup.operational_url)
    try:
        state = mig.TargetState.load(engine)
        assert state.users["user0001"]["email"] == "user0001@example.test"
        assert state.user_identities[(mig.GOOGLE_ISSUER, "subject1")] == "user0001"
        assert state.conversations["chat0001"]["head_id"] == "answer01"
        assert "share001" in state.shares
        messages["answer01"]["content"] = "Updated answer"
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE user SET name='New name', updated_at=120")
            conn.execute("UPDATE chat SET title='Changed', updated_at=120, chat=?", (json.dumps(data),))
        updated = mig.run(setup, apply=True)
        assert updated.counts["users"]["updated"] == 1
        assert updated.counts["conversations"]["updated"] == 1
        assert updated.counts["messages"]["updated"] == 1
        with engine.begin() as conn:
            assert conn.execute(text("SELECT text FROM hz_messages WHERE id='answer01'")).scalar() == "Updated answer"
            conn.execute(text("UPDATE hz_conversations SET title='Local edit', updated_at=:t"), {"t": 9e10})
            conn.execute(text("UPDATE hz_users SET name='Local user', updated_at=:t"), {"t": 9e10})
        kept = mig.run(setup, apply=True)
        assert kept.counts["conversations"]["kept_changed_in_hubzoid"] == 1
        assert mig.TargetState.load(engine).users["user0001"]["name"] == "Local user"
        with engine.connect() as conn:
            assert conn.execute(text("SELECT title FROM hz_conversations")).scalar() == "Local edit"
    finally:
        engine.dispose()


def test_target_previous_marker_and_reimport_boundaries():
    state = mig.TargetState()
    for raw in ("not json", "[]", '{"time":0}'):
        state.meta[mig.MARKER_PREFIX + "source"] = raw
        assert state.previous("source") is None
    previous = {"time": 100, "skipped": {"users": ["retry"]}}
    state.meta[mig.MARKER_PREFIX + "source"] = json.dumps(previous)
    assert state.previous("source") == previous
    assert mig._existed_before(previous, 50, "users", "retry") is False
    assert mig._existed_before(previous, None, "users", "old") is False
    assert mig._changed_since(previous, None) is True
    assert mig._changed_since(previous, 100) is False


@pytest.mark.parametrize(("source_url", "target_url", "message"), [
    ("postgresql://host/source", "sqlite:///target", "Open WebUI database in SQLite"),
    ("sqlite:///source", "postgresql://host/target", "operational store in SQLite"),
])
def test_rehearsal_refuses_live_postgres(setup, tmp_path, source_url, target_url, message):
    with pytest.raises(mig.MigrationBlocked, match=message):
        mig.rehearsal_setup(replace(setup, source_url=mig._normalize_source_url(source_url),
                                    operational_url=target_url), tmp_path / "copy")


def test_rehearsal_rejects_nonempty_destination_and_original_subfolder(setup, tmp_path):
    with pytest.raises(mig.MigrationBlocked, match="not empty"):
        mig.rehearsal_setup(setup, setup.entry)
    with pytest.raises(mig.MigrationBlocked, match="outside the original hub"):
        mig.rehearsal_setup(setup, setup.entry / "copy")
    assert not (setup.entry / "copy").exists()


def test_standalone_rehearsal_materializes_external_sqlite_and_uploads(setup, tmp_path):
    copy = mig.rehearsal_setup(setup, tmp_path / "scratch")
    assert copy.entry != setup.entry and copy.write_root == tmp_path / "scratch"
    assert Path(copy.source_url.database).is_file()
    assert copy.uploads_dir.is_dir()
    assert not Path(copy.operational_url.split("sqlite:///", 1)[1]).exists()
    (copy.entry / "AGENTS.md").write_text("copy only")
    assert "name: Helper" in (setup.entry / "AGENTS.md").read_text()


def test_locate_checks_missing_folder_source_alias_and_deployment(setup, tmp_path, monkeypatch):
    from hubzoid import deployment

    with pytest.raises(mig.MigrationBlocked, match="not a folder"):
        mig.locate(tmp_path / "missing", env={})
    with pytest.raises(mig.MigrationBlocked, match="no agent"):
        mig.locate(setup.entry, owui_db=str(setup.source_url), aliases={"old": "unknown"}, env={})
    with pytest.raises(mig.MigrationBlocked, match="Nothing to migrate"):
        mig.locate(setup.entry, owui_db=f"sqlite:///{tmp_path / 'missing.db'}", env={})
    found = mig.locate(setup.entry, owui_db=str(setup.source_url), env={})
    assert found.source_url == setup.source_url
    monkeypatch.setattr(deployment, "hubs", lambda path: [])
    with pytest.raises(mig.MigrationBlocked, match="No hub was found"):
        mig.locate(setup.entry, env={})
    monkeypatch.setattr(deployment, "read", lambda *a: (_ for _ in ()).throw(ValueError("invalid manifest")))
    with pytest.raises(mig.MigrationBlocked, match="invalid manifest"):
        mig.locate(setup.entry, env={})


def test_operational_url_conflict_is_refused(setup, monkeypatch):
    from hubzoid import deployment

    monkeypatch.setattr(deployment, "read", lambda *a: {"operational_url": "sqlite:///registered.db"})
    with pytest.raises(mig.MigrationBlocked, match="differs from the registered deployment"):
        mig._operational_url(setup.entry, {"HUBZOID_OPERATIONAL_DB": "sqlite:///other.db"})
    assert mig._operational_url(setup.entry, {}) == "sqlite:///registered.db"


def test_cli_alias_refusals_and_blocking_json(tmp_path):
    runner = CliRunner()
    bad_alias = runner.invoke(mig.migrate_app, ["openwebui", str(tmp_path), "--model-alias", "bad"])
    assert bad_alias.exit_code == 2 and "expects OLD=AGENT" in bad_alias.output
    blocked = runner.invoke(mig.migrate_app, ["openwebui", str(tmp_path / "missing"), "--json", "--apply"])
    assert blocked.exit_code == 2
    body = json.loads(blocked.stdout)
    assert body["mode"] == "apply" and body["applied"] is False
    assert "not a folder" in body["blocking"][0]


def test_report_human_rendering_and_cli_rehearsal_metadata(setup, tmp_path, monkeypatch):
    report = mig.Report("apply")
    report.info.update(previous_import={"time": 100}, deployment={"kind": "hub", "hubs": [
        {"key": "helper", "model_id": "helper"}]}, source={"location": "source"}, target={"location": "target"})
    report.bump("messages", "to_import", 2)
    report.skip("messages", "unsupported", "message1")
    report.unknown_models["retired"] = 1
    report.block("conflict")
    report.block("conflict")
    report.warn("warning")
    report.warn("warning")
    report.note("note")
    report.note("note")
    rendered = mig.render(report, True)
    assert "Previous import:" in rendered and "message1 (unsupported)" in rendered
    assert "retired: 1" in rendered and "Nothing was written" in rendered
    assert report.blocking == ["conflict"] and report.warnings == ["warning"] and report.notes == ["note"]
    report.blocking.clear()
    report.applied = True
    monkeypatch.setattr(mig, "locate", lambda *a, **kw: setup)
    monkeypatch.setattr(mig, "rehearsal_setup", lambda *a: setup)
    monkeypatch.setattr(mig, "run", lambda *a, **kw: report)
    result = CliRunner().invoke(mig.migrate_app, ["openwebui", "--apply", "--rehearse", str(tmp_path / "copy"), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["rehearsal_copy"] == str(setup.entry)
    assert "Migrated the rehearsal copy" in mig.render(report)


def test_owner_creation_failure_leaves_actionable_warning(setup, monkeypatch):
    from hubzoid import db

    setup.env.update(HUBZOID_ADMIN_EMAIL="owner@example.test", HUBZOID_ADMIN_PASSWORD="test-password")
    monkeypatch.setattr(db, "operational_url", lambda path: (_ for _ in ()).throw(OSError("unavailable")))
    report = mig.Report("apply")
    mig._create_owner(setup, report)
    assert len(report.warnings) == 1
    assert "hubzoid admin create" in report.warnings[0]


def test_gateway_rehearsal_copies_workflow_storage_and_rewrites_manifest(setup, tmp_path):
    from hubzoid import deployment

    second = _hub(tmp_path / "second", "Second")
    workflow = tmp_path / "workflow.db"
    with sqlite3.connect(workflow) as conn:
        conn.execute("CREATE TABLE proof (value TEXT)")
        conn.execute("INSERT INTO proof VALUES ('kept')")
    (setup.uploads_dir / "note.txt").write_text("attachment")
    manifest = {"version": 1, "hubs": [
        {"key": "helper", "path": str(setup.entry), "model_id": "helper", "dbos_url": f"sqlite:///{workflow}"},
        {"key": "second", "path": str(second), "model_id": "second"}],
        "owui_db": setup.source_url.database, "owui_database_url": str(setup.source_url),
        "branding_dir": str(setup.entry), "operational_url": setup.operational_url}
    gateway = replace(setup, kind="gateway", manifest=manifest, hubs=[
        *setup.hubs, mig.HubInfo("second", "Second", second, "second")])
    copy = mig.rehearsal_setup(gateway, tmp_path / "scratch")
    saved = json.loads((tmp_path / "scratch" / "gateway-data" / "deployment.json").read_text())
    assert [h.path.name for h in copy.hubs] == ["helper", "second"]
    assert saved["branding_dir"] == str(copy.hubs[0].path)
    assert saved["owui_db"] == copy.source_url.database
    assert saved["owui_database_url"] == str(copy.source_url)
    assert saved["hubs"][0]["dbos_url"] != f"sqlite:///{workflow}"
    with sqlite3.connect(saved["hubs"][0]["dbos_url"].split("sqlite:///", 1)[1]) as conn:
        assert conn.execute("SELECT value FROM proof").fetchone() == ("kept",)
    assert (copy.uploads_dir / "note.txt").read_text() == "attachment"
    assert deployment.read(copy.entry, {})["operational_url"] == copy.operational_url


def test_gateway_rehearsal_refuses_original_subfolder_and_postgres_workflow(setup, tmp_path):
    gateway = replace(setup, kind="gateway", manifest={"version": 1, "hubs": [
        {"key": "helper", "path": str(setup.entry), "dbos_url": "postgresql://host/workflows"}]})
    with pytest.raises(mig.MigrationBlocked, match="outside every hub"):
        mig.rehearsal_setup(gateway, setup.entry / "scratch")
    with pytest.raises(mig.MigrationBlocked, match="workflow runs in PostgreSQL"):
        mig.rehearsal_setup(gateway, tmp_path / "scratch")


def test_attachment_copy_from_vendor_and_write_failure_are_reported(setup, source, tmp_path, monkeypatch):
    payload = setup.uploads_dir / "file0001_vendor.txt"
    payload.write_text("vendor bytes")
    _insert(setup.source_url.database, "file", id="file0001", filename="vendor.txt", path=str(payload))
    report = mig.Report("apply")
    attachments = mig.Attachments(source, setup.uploads_dir, report, True)
    with source.engine.connect() as conn:
        part = attachments.parts(conn, [{"id": "file0001", "name": "display.txt"}], tmp_path / "chat")[0]
        assert (tmp_path / "chat" / part["file_id"]).read_text() == "vendor bytes"
        assert part["size"] == len("vendor bytes")
        monkeypatch.setattr(attachments, "_write", lambda *a, **kw: (_ for _ in ()).throw(PermissionError()))
        failed = attachments.parts(conn, [{"id": "file0001", "name": "display.txt"}], tmp_path / "unwritable")
    assert failed[0]["name"] == "display.txt"
    assert report.counts["files"] == {"copied_from_open_webui": 1, "could_not_be_written": 1}
    assert "PermissionError" in report.warnings[0]


def test_sqlite_readonly_fallback_refuses_uncheckpointed_wal(setup, monkeypatch):
    real_create = mig.create_engine
    calls = []

    class UnreadableEngine:
        disposed = False

        def connect(self):
            raise OSError("read-only WAL unavailable")

        def dispose(self):
            self.disposed = True

    unreadable = UnreadableEngine()

    def create(url, **kwargs):
        calls.append(url)
        return unreadable if len(calls) == 1 else real_create(url, **kwargs)

    monkeypatch.setattr(mig, "create_engine", create)
    fallback = mig._source_engine(setup.source_url, None)
    try:
        assert calls[1].query["immutable"] == "1"
        assert unreadable.disposed
        with fallback.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "fixture-revision"
            with pytest.raises(Exception, match="readonly"):
                conn.execute(text("DELETE FROM alembic_version"))
    finally:
        fallback.dispose()
    calls.clear()
    Path(setup.source_url.database + "-wal").write_bytes(b"uncheckpointed")
    with pytest.raises(mig.MigrationBlocked, match="write-ahead log"):
        mig._source_engine(setup.source_url, None)


def test_partial_target_schema_reads_as_empty_and_empty_source_has_stable_fingerprint(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    try:
        state = mig.TargetState.load(engine)
        assert state.users == {} and state.conversations == {} and state.revision is None
    finally:
        engine.dispose()
    url = URL.create("sqlite", database=str(tmp_path / "source.db"))
    assert mig._fingerprint({}, url) == mig._fingerprint({}, url)
    assert len(mig._fingerprint({}, url)) == 16


def test_deleted_chat_is_not_restored_and_fallback_message_history_is_used(setup, source):
    importer = _importer(setup, source, previous={"time": 200})
    _insert(setup.source_url.database, "chat_message", id="chat0001-message1", chat_id="chat0001", role="user",
            content='"recovered"', created_at=100)
    chat = {"id": "chat0001", "user_id": "user0001", "created_at": 100, "chat": '{"models":["helper"]}'}
    with source.engine.connect() as conn:
        assert importer._one(chat, conn, None).conversation is None
        assert importer.report.counts["conversations"]["deleted_in_hubzoid_not_restored"] == 1
        importer.prev["skipped"] = {"conversations": ["chat0001"]}
        outcome = importer._one(chat, conn, None)
    assert outcome.action == "insert"
    assert outcome.insert_messages[0]["text"] == "recovered"
    assert outcome.conversation["head_id"] == outcome.insert_messages[0]["id"]
