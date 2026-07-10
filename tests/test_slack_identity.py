"""Tests for #4: Slack sender -> Open WebUI identity mapping.

  * stream_reply forwards X-OpenWebUI-User-Email when a sender email is given,
    always declares the slack surface, and omits the email header otherwise.
  * _lookup_email extracts the profile email and fails closed.
  * the manifest requests users:read.email.
  * settings parses SLACK_IDENTITY_MAPPING.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from hubzoid.slack.adapter import _lookup_email, stream_reply


class _FakeSSEResponse:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    def iter_lines(self):
        for line in self._lines:
            yield line.decode("utf-8") if isinstance(line, bytes) else line

    def raise_for_status(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _done():
    return [b'data: {"choices":[{"delta":{"content":"ok"}}]}', b"data: [DONE]"]


def test_stream_reply_forwards_email_header_and_surface():
    fake = MagicMock()
    fake.stream.return_value = _FakeSSEResponse(_done())
    stream_reply(
        bridge_url="http://x/v1", api_key="k", model="m",
        messages=[{"role": "user", "content": "hi"}],
        on_delta=lambda _d: None,
        user_email="priya@isha.org",
        http_client=fake,
    )
    headers = fake.stream.call_args.kwargs["headers"]
    assert headers["X-Hubzoid-Surface"] == "slack"
    assert headers["X-OpenWebUI-User-Email"] == "priya@isha.org"


def test_stream_reply_omits_email_header_when_none():
    fake = MagicMock()
    fake.stream.return_value = _FakeSSEResponse(_done())
    stream_reply(
        bridge_url="http://x/v1", api_key="k", model="m",
        messages=[{"role": "user", "content": "hi"}],
        on_delta=lambda _d: None,
        http_client=fake,
    )
    headers = fake.stream.call_args.kwargs["headers"]
    assert "X-OpenWebUI-User-Email" not in headers
    assert headers["X-Hubzoid-Surface"] == "slack"   # surface still declared


def test_stream_reply_surface_is_configurable():
    for surf in ("slack-dm", "slack-channel"):
        fake = MagicMock()
        fake.stream.return_value = _FakeSSEResponse(_done())
        stream_reply(
            bridge_url="http://x/v1", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            on_delta=lambda _d: None, surface=surf, http_client=fake,
        )
        assert fake.stream.call_args.kwargs["headers"]["X-Hubzoid-Surface"] == surf


def test_owui_group_lookup_is_case_insensitive(tmp_path, monkeypatch):
    import sqlite3
    from hubzoid.access import owui_groups
    db = tmp_path / ".openwebui-data" / "webui.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript(
        'CREATE TABLE "group"(id TEXT, name TEXT);'
        'CREATE TABLE "user"(id TEXT, email TEXT);'
        'CREATE TABLE group_member(group_id TEXT, user_id TEXT);'
        "INSERT INTO \"group\" VALUES('g1','ornate');"
        "INSERT INTO \"user\" VALUES('u1','john.doe@corp.com');"
        "INSERT INTO group_member VALUES('g1','u1');"
    )
    con.commit()
    con.close()
    # Slack forwards mixed-case; must still match the lowercased OWUI email
    assert owui_groups.resolve_groups(tmp_path, "John.Doe@CORP.com") == {"ornate"}


def test_lookup_email_extracts_profile_email():
    client = MagicMock()
    client.users_info.return_value = {"user": {"profile": {"email": "u@x.org"}}}
    assert _lookup_email(client, "U123") == "u@x.org"


def test_lookup_email_fails_closed():
    client = MagicMock()
    client.users_info.side_effect = RuntimeError("missing scope")
    assert _lookup_email(client, "U123") is None
    # no email on profile -> None
    client2 = MagicMock()
    client2.users_info.return_value = {"user": {"profile": {}}}
    assert _lookup_email(client2, "U123") is None


def test_manifest_requests_email_scope():
    from hubzoid.slack import manifest as manifest_mod
    assert "users:read.email" in manifest_mod._BOT_SCOPES


def test_settings_parses_identity_mapping(tmp_path, monkeypatch):
    from hubzoid import settings as settingslib
    monkeypatch.delenv("SLACK_IDENTITY_MAPPING", raising=False)
    assert settingslib.load(tmp_path).slack_identity_mapping is False
    monkeypatch.setenv("SLACK_IDENTITY_MAPPING", "true")
    assert settingslib.load(tmp_path).slack_identity_mapping is True
