"""How a new account can sign in, as the Console's Add user offers it: read from
the hub's own sign-in settings, without a network call."""
from __future__ import annotations

from hubzoid.access import accounts as accountlib


def test_sign_in_options_follow_the_hubs_own_settings(tmp_path, monkeypatch):
    for key in ("HUBZOID_DEPLOYMENT", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                "OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "OAUTH_ALLOWED_DOMAINS",
                "ENABLE_LOGIN_FORM", "ENABLE_PASSWORD_AUTH"):
        monkeypatch.delenv(key, raising=False)
    hub = tmp_path / "solo"
    hub.mkdir()
    assert accountlib.sign_in_options(hub) == {"password": True, "google": False, "links": True}
    # Google needs a client and permission to attach to the account by its verified email.
    (hub / ".env").write_text("GOOGLE_CLIENT_ID=id\nGOOGLE_CLIENT_SECRET=secret\n")
    assert accountlib.sign_in_options(hub)["google"] is False
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    assert accountlib.sign_in_options(hub)["google"] is True
    monkeypatch.setenv("OAUTH_ALLOWED_DOMAINS", "x.org")
    assert accountlib.sign_in_options(hub)["google_domains"] == ["x.org"]
    monkeypatch.setenv("ENABLE_LOGIN_FORM", "false")
    assert accountlib.sign_in_options(hub)["password"] is False


def test_no_client_id_or_secret_is_ever_returned(tmp_path, monkeypatch):
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    (tmp_path / ".env").write_text("GOOGLE_CLIENT_ID=client-canary\nGOOGLE_CLIENT_SECRET=secret-canary\n")
    text = repr(accountlib.sign_in_options(tmp_path))
    assert "client-canary" not in text and "secret-canary" not in text
