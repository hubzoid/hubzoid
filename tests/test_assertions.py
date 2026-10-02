"""Signed identity assertions (hubzoid.assertions) and how the bridge trusts
identity headers: in the web app mode only when a valid X-Hubzoid-Assertion
covers exactly those values; in the legacy Open WebUI mode as in 1.0.x."""
from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from starlette.requests import Request

from hubzoid import access, assertions, secretbox
from hubzoid.access import audit as auditlib


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("HUBZOID_UI", "HUBZOID_SECRET_KEY", "HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB",
              "DATABASE_URL", "HUBZOID_DEPLOYMENT", "HUBZOID_RESTRICTED_SURFACES"):
        monkeypatch.delenv(k, raising=False)
    from hubzoid import migrations

    access._stores.clear()
    migrations._done.clear()
    auditlib._IMPORTED.clear()
    secretbox.reset_cache()
    yield
    access._stores.clear()
    secretbox.reset_cache()


@pytest.fixture
def hub(tmp_path):
    hub = tmp_path / "sales"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: sales\ndescription: d\n---\nbody")
    return hub


def _lower(headers: dict) -> dict:
    return {k.lower(): v for k, v in headers.items()}


def _request(headers: dict) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                    "headers": raw, "query_string": b""})


# ---------------------------------------------------------------------------
# the token
# ---------------------------------------------------------------------------
def test_round_trip_is_canonical(hub):
    token = assertions.issue(hub, email=" Ann@Example.org ", groups=["Sales", "ops", "sales"],
                             surface="Slack-DM")
    a = assertions.verify(hub, token)
    assert (a.hub, a.email, a.groups, a.surface) == ("sales", "ann@example.org", ("ops", "sales"), "slack-dm")
    assert abs(a.issued_at - time.time()) < 5
    version, body, sig = token.split(".")
    assert version == "v1" and len(sig) == 64


def test_expired_and_future_tokens_are_refused(hub):
    now = time.time()
    assert assertions.verify(hub, assertions.issue(hub, surface="x", now=now - 61), now=now) is None
    assert assertions.verify(hub, assertions.issue(hub, surface="x", now=now - 59), now=now) is not None
    assert assertions.verify(hub, assertions.issue(hub, surface="x", now=now + 30), now=now) is None
    assert assertions.verify(hub, assertions.issue(hub, surface="x", now=now + 3), now=now) is not None


def test_tampered_and_malformed_tokens_are_refused(hub):
    token = assertions.issue(hub, email="ann@example.org", surface="whatsapp")
    version, body, sig = token.split(".")
    forged = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    forged["email"] = "boss@example.org"
    forged_body = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
    for bad in (
        f"{version}.{forged_body}.{sig}",               # payload changed
        f"{version}.{body}.{'0' * 64}",                 # signature changed
        f"v2.{body}.{sig}",                             # unknown version
        f"{version}.{body}",                            # missing part
        "garbage", "", "v1..", "v1.%%%.abc", "v1." + "a" * 9000 + ".b",
    ):
        assert assertions.verify(hub, bad) is None, bad
    assert assertions.verify(None, token) is None


def test_a_token_for_another_hub_or_key_is_refused(tmp_path, hub, monkeypatch):
    token = assertions.issue(hub, email="ann@example.org", surface="whatsapp")
    other = tmp_path / "support"
    other.mkdir()
    # Same deployment key, other hub: refused (the hub is part of the claim).
    monkeypatch.setenv("HUBZOID_SECRET_KEY", Fernet.generate_key().decode())
    token_same_key = assertions.issue(hub, email="ann@example.org", surface="whatsapp")
    assert assertions.verify(other, token_same_key) is None
    assert assertions.verify(hub, token_same_key) is not None
    # Another key: refused.
    assert assertions.verify(hub, token) is None


def test_rotated_keys_still_verify(hub, monkeypatch):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("HUBZOID_SECRET_KEY", old)
    token = assertions.issue(hub, email="ann@example.org", surface="whatsapp")
    monkeypatch.setenv("HUBZOID_SECRET_KEY", f"{new},{old}")
    assert assertions.verify(hub, token).email == "ann@example.org"


def test_covers_means_exactly_the_asserted_values(hub):
    a = assertions.verify(hub, assertions.issue(hub, email="ann@example.org", groups=["field"],
                                                surface="whatsapp"))
    ok = {"x-openwebui-user-email": "ANN@example.org", "x-hubzoid-groups": "Field",
          "x-hubzoid-surface": "whatsapp"}
    assert assertions.covers(a, ok)
    assert assertions.covers(a, dict(ok, **{"x-hubzoid-user": "ann@example.org"}))
    assert not assertions.covers(a, dict(ok, **{"x-hubzoid-user": "boss@example.org"}))
    assert not assertions.covers(a, dict(ok, **{"x-hubzoid-groups": "field,admin"}))
    assert not assertions.covers(a, dict(ok, **{"x-hubzoid-groups": ""}))
    assert not assertions.covers(a, dict(ok, **{"x-hubzoid-surface": "web"}))
    assert not assertions.covers(a, {k: v for k, v in ok.items() if k != "x-openwebui-user-email"})
    nobody = assertions.verify(hub, assertions.issue(hub, surface="slack-channel"))
    assert assertions.covers(nobody, {"x-hubzoid-surface": "slack-channel"})
    assert not assertions.covers(nobody, {"x-hubzoid-surface": "slack-channel",
                                          "x-openwebui-user-email": "ann@example.org"})


def test_identity_headers_by_mode(hub, monkeypatch):
    signed = assertions.identity_headers(hub, surface="whatsapp", email="ann@example.org",
                                         groups=["Field"])
    assert signed["X-Hubzoid-Surface"] == "whatsapp"
    assert signed["X-OpenWebUI-User-Email"] == "ann@example.org"
    assert signed["X-Hubzoid-Groups"] == "Field"
    assert assertions.vouched(hub, _lower(signed)).groups == ("field",)
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    legacy = assertions.identity_headers(hub, surface="whatsapp", email="ann@example.org")
    assert legacy == {"X-Hubzoid-Surface": "whatsapp", "X-OpenWebUI-User-Email": "ann@example.org"}


def test_an_unusable_key_sends_no_assertion_and_never_raises(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_SECRET_KEY", "not-a-fernet-key")
    headers = assertions.identity_headers(hub, surface="whatsapp", email="ann@example.org")
    assert assertions.HEADER not in headers
    assert assertions.verify(hub, "v1.abc.def") is None


# ---------------------------------------------------------------------------
# the bridge's trust rule
# ---------------------------------------------------------------------------
def _signed(hub, **kw):
    return assertions.identity_headers(hub, **kw)


def test_web_app_mode_trusts_only_asserted_headers(hub):
    from hubzoid.server import _derive_identity

    ident = _derive_identity({}, _request(_signed(hub, surface="whatsapp", email="ann@example.org",
                                                  groups=["Field"])), hub)
    assert (ident.user, ident.surface) == ("ann@example.org", "whatsapp")
    assert ident.groups == frozenset({"field"})

    # The same headers without an assertion: anonymous, whatever they claim.
    bare = {"X-OpenWebUI-User-Email": "ann@example.org", "X-Hubzoid-Groups": "admin",
            "X-Hubzoid-Surface": "owui", "X-Hubzoid-User": "boss@example.org"}
    ident = _derive_identity({"user": "boss@example.org"}, _request(bare), hub)
    assert ident.is_anonymous and ident.groups == frozenset() and ident.surface == "api"

    # A valid assertion with one header changed: anonymous.
    tampered = dict(_signed(hub, surface="whatsapp", email="ann@example.org"),
                    **{"X-Hubzoid-Groups": "admin"})
    assert _derive_identity({}, _request(tampered), hub).is_anonymous

    # An expired one: anonymous.
    stale = dict(_signed(hub, surface="whatsapp", email="ann@example.org"))
    stale[assertions.HEADER] = assertions.issue(hub, email="ann@example.org", surface="whatsapp",
                                                now=time.time() - 120)
    assert _derive_identity({}, _request(stale), hub).is_anonymous


def test_web_app_mode_entry_needs_an_asserted_identity(hub):
    from hubzoid.server import _enforce_use_hub

    gs = access.store_for(hub)
    gs.bootstrap(["boss@example.org"])
    gs.grant("ann@example.org", "sales", "use_hub", actor="t")
    with pytest.raises(HTTPException) as err:
        _enforce_use_hub(_request({"X-OpenWebUI-User-Email": "ann@example.org"}), hub)
    assert err.value.status_code == 403 and "requires sign-in" in err.value.detail
    _enforce_use_hub(_request(_signed(hub, surface="whatsapp", email="ann@example.org")), hub)
    with pytest.raises(HTTPException) as err:
        _enforce_use_hub(_request(_signed(hub, surface="whatsapp", email="carl@example.org")), hub)
    assert err.value.status_code == 403
    gs.suspend("ann@example.org", actor="t")
    with pytest.raises(HTTPException) as err:
        _enforce_use_hub(_request(_signed(hub, surface="whatsapp", email="ann@example.org")), hub)
    assert err.value.status_code == 403 and "blocked" in err.value.detail


def test_web_app_mode_never_rebinds_an_identity_from_a_header(hub):
    """Open WebUI's account id header means nothing without Open WebUI; in 1.0.x
    a changed id marks the account replaced (grants removed, blocked)."""
    from hubzoid.server import _enforce_use_hub

    gs = access.store_for(hub)
    gs.upsert_identity(email="ann@example.org", owui_id="u-1")
    gs.grant("ann@example.org", "sales", "use_hub", actor="t")
    headers = dict(_signed(hub, surface="whatsapp", email="ann@example.org"),
                   **{"X-OpenWebUI-User-Id": "attacker-account"})
    _enforce_use_hub(_request(headers), hub)
    assert gs.identity("ann@example.org")["owui_id"] == "u-1"
    assert not gs.is_suspended("ann@example.org")
    assert gs.can("ann@example.org", "sales", "use_hub")


def test_open_webui_mode_keeps_the_1_0_trust_rule(hub, monkeypatch):
    from hubzoid.server import _derive_identity, _enforce_use_hub

    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    bare = {"X-OpenWebUI-User-Email": "ann@example.org", "X-Hubzoid-Groups": "field"}
    ident = _derive_identity({}, _request(bare), hub)
    assert (ident.user, ident.surface) == ("ann@example.org", "owui")
    assert ident.groups == frozenset({"field"})
    gs = access.store_for(hub)
    gs.bootstrap(["boss@example.org"])
    gs.grant("ann@example.org", "sales", "use_hub", actor="t")
    _enforce_use_hub(_request(bare), hub)  # trusted as sent


def test_open_webui_owner_is_provisioned_on_first_chat(hub, monkeypatch):
    """Every agent needs a grant, so the configured owner, signed in to Open
    WebUI as an administrator, gets the owner's grants on their first chat (as
    a Console visit would). Nobody else ever self-provisions."""
    from hubzoid.server import _enforce_use_hub

    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    monkeypatch.setenv("WEBUI_AUTH", "true")
    monkeypatch.setenv("WEBUI_ADMIN_EMAIL", "boss@example.org")
    gs = access.store_for(hub)
    for email, role in (("boss@example.org", "user"), ("ann@example.org", "admin")):
        with pytest.raises(HTTPException) as err:
            _enforce_use_hub(_request({"X-OpenWebUI-User-Email": email,
                                       "X-OpenWebUI-User-Role": role}), hub)
        assert err.value.status_code == 403
    _enforce_use_hub(_request({"X-OpenWebUI-User-Email": "boss@example.org",
                               "X-OpenWebUI-User-Role": "admin"}), hub)
    assert gs.can("boss@example.org", "sales", "use_hub")
    assert not gs.can("ann@example.org", "sales", "use_hub")
