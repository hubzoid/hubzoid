"""Console account management against a fake Open WebUI (httpx.MockTransport).

The adapter always creates `role: "user"`, never keeps the new account's token,
maps EMAIL_TAKEN, finds an account by email, and refuses a public-only URL. The
service creates, binds and grants in one step; when access fails after the
account exists it says so and a retry grants access to that account instead of
creating a second one. A password never reaches the audit log, the access
store, a response or a log line, and a Google sign-in-only account's random
password is never returned at all.
"""
from __future__ import annotations

import json
import logging
import re

import httpx
import pytest
from sqlalchemy import text

from hubzoid import deployment
from hubzoid.access import accounts as accountlib
from hubzoid.access.accounts import AccountError, OwuiAccounts
from hubzoid.access.service import Denied

from tests.test_access_service import (  # noqa: F401 — shared fixture and helpers
    DELEGATE,
    ROOT,
    SERVICE,
    FakeOwui,
    actor,
    dep,
    make_deployment,
)

PASSWORD = "Correct-Horse-7-battery"


def _adapter(fake):
    return OwuiAccounts("http://owui.internal", SERVICE, "svc-secret",
                        transport=httpx.MockTransport(fake))


def _everything_stored(dep) -> str:
    """Every row Hubzoid wrote, flattened, for leak checks."""
    out = []
    with dep.gs.engine.connect() as c:
        for table in ("hz_access_audit", "hz_identities", "hz_meta", "hz_change_requests",
                      "hz_grants"):
            out += [repr(tuple(r)) for r in c.execute(text(f"SELECT * FROM {table}"))]
    return "\n".join(out)


# ---- adapter ----------------------------------------------------------------------

def test_create_always_sends_user_role_and_drops_token():
    fake = FakeOwui()
    created = _adapter(fake).create(email="ann@x.org", name="Ann", password=PASSWORD)
    method, path, body = fake.requests[-1]
    assert (method, path) == ("POST", "/api/v1/auths/add")
    assert body["role"] == "user" and body["password"] == PASSWORD
    assert created == {"id": created["id"], "email": "ann@x.org", "name": "Ann", "role": "user"}
    assert "token" not in created
    with pytest.raises(ValueError):
        _adapter(fake).create(email="b@x.org", name="B", password=PASSWORD, role="admin")


def test_email_taken_and_rejections_are_mapped_without_the_password():
    fake = FakeOwui()
    fake.add_user("ann@x.org", "Ann")
    with pytest.raises(AccountError) as e:
        _adapter(fake).create(email="ann@x.org", name="Ann", password=PASSWORD)
    assert (e.value.status, e.value.code) == (409, "account_exists")
    fake.reject_password = True
    with pytest.raises(AccountError) as e:
        _adapter(fake).create(email="new@x.org", name="New", password=PASSWORD)
    assert e.value.code == "rejected" and PASSWORD not in e.value.message


def test_unreachable_chat_app_is_reported_as_uncertain():
    fake = FakeOwui()
    adapter = _adapter(fake)
    adapter.create(email="a@x.org", name="A", password=PASSWORD)  # signs in fine
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if request.url.path == "/api/v1/auths/add":
            raise httpx.ReadTimeout("slow", request=request)
        return fake(request)

    with pytest.raises(AccountError) as e:
        _adapter(flaky).create(email="b@x.org", name="B", password=PASSWORD)
    assert e.value.code == "uncertain" and e.value.certain is False
    fake.down = True
    with pytest.raises(AccountError) as e:
        _adapter(fake).get("x")
    assert e.value.code == "accounts_unavailable"


def test_service_account_must_sign_in():
    with pytest.raises(AccountError) as e:
        OwuiAccounts("http://owui.internal", SERVICE, "wrong",
                     transport=httpx.MockTransport(FakeOwui())).get("x")
    assert e.value.code == "accounts_unavailable"
    assert "wrong" not in e.value.message


def test_refuses_public_only_url(tmp_path, monkeypatch):
    for key in ("HUBZOID_DEPLOYMENT", "OWUI_INTERNAL_URL", "WEBUI_URL", "HUBZOID_PUBLIC_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_EMAIL", SERVICE)
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_PASSWORD", "svc-secret")
    monkeypatch.setenv("WEBUI_URL", "https://hub.example.com")
    assert accountlib.internal_url(tmp_path) == ""
    assert not accountlib.configured(tmp_path)
    with pytest.raises(AccountError) as e:
        accountlib.for_deployment(tmp_path)
    assert e.value.code == "accounts_unavailable"
    # An "internal" URL that is really the public one is refused too.
    monkeypatch.setenv("OWUI_INTERNAL_URL", "https://hub.example.com")
    assert accountlib.internal_url(tmp_path) == ""
    monkeypatch.setenv("OWUI_INTERNAL_URL", "http://127.0.0.1:43080")
    assert accountlib.internal_url(tmp_path) == "http://127.0.0.1:43080"
    assert accountlib.configured(tmp_path)


def test_manifest_url_is_internal(dep):
    assert accountlib.internal_url(dep.hub_dir) == "http://owui.internal"
    assert accountlib.configured(dep.hub_dir)


# ---- create --------------------------------------------------------------------------

def test_create_binds_grants_and_audits(dep, caplog):
    caplog.set_level(logging.DEBUG)
    result = dep.svc.create_account(
        actor(DELEGATE), email="Ann@X.org", name="Ann", password=PASSWORD,
        grants=[("finance", "ledger")])
    assert result["subject"] == "ann@x.org" and result["grants"] == {"finance": ["ledger"]}
    assert PASSWORD not in repr(result)
    user = dep.owui.by_email("ann@x.org")
    assert user and user["role"] == "user"
    ident = dep.gs.identity("ann@x.org")
    assert ident["owui_id"] == user["id"] and not ident["pending"]
    assert dep.gs.can("ann@x.org", "finance", "ledger")
    actions = [r["action"] for r in dep.gs.read_access_audit(20, subject="ann@x.org")]
    assert "account_create" in actions and actions.count("grant") == 2
    assert PASSWORD not in _everything_stored(dep)
    assert PASSWORD not in caplog.text


def test_delegate_cannot_create_beyond_ceiling_or_without_access(dep):
    for grants in ([("finance", "payroll")], [("ops", "use_hub")], [("finance", "manage_access")], []):
        with pytest.raises(Denied):
            dep.svc.create_account(actor(DELEGATE), email="ann@x.org", name="Ann",
                                   password=PASSWORD, grants=grants)
    # Refused before the chat app was asked to create anything.
    assert not any(p == "/api/v1/auths/add" for _, p, _ in dep.owui.requests)
    assert dep.owui.by_email("ann@x.org") is None


def test_delegate_cannot_create_an_account_that_inherits_wider_access(dep):
    """Whoever sets the password can use every grant already waiting for the
    email, so a delegate may not create one that holds more than they do."""
    for subject, hub, perm in (("pre@x.org", "finance", "payroll"),
                               ("ops@x.org", "ops", "inventory"),
                               ("boss@x.org", "*", "manage_access")):
        dep.gs.grant(subject, hub, perm, actor=ROOT)
        with pytest.raises(Denied) as e:
            dep.svc.create_account(actor(DELEGATE), email=subject, name="X", password=PASSWORD,
                                   grants=[("finance", "use_hub")])
        assert e.value.code == "outside_ceiling"
        assert dep.owui.by_email(subject) is None
    # Pre-granted access within the delegate's ceiling is fine.
    dep.gs.grant("ok@x.org", "finance", "ledger", actor=ROOT)
    dep.svc.create_account(actor(DELEGATE), email="ok@x.org", name="Ok", password=PASSWORD,
                           grants=[("finance", "use_hub")])
    assert dep.gs.can("ok@x.org", "finance", "ledger")
    # An organization administrator may create any of them.
    dep.svc.create_account(actor(ROOT), email="boss@x.org", name="Boss", password=PASSWORD,
                           grants=[])


def test_org_admin_may_create_without_access(dep):
    dep.svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=PASSWORD, grants=[])
    assert dep.owui.by_email("ann@x.org")
    assert not dep.gs.permissions_for("ann@x.org", "finance")


def test_existing_account_offers_grant_instead(dep):
    # Known locally: refused before calling the chat app.
    dep.gs.upsert_identity(email="ann@x.org", owui_id="u-ann", display="Ann")
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=PASSWORD,
                               grants=[("finance", "use_hub")])
    assert (e.value.status, e.value.code) == (409, "account_exists")
    # Unknown locally but taken in the chat app: no grant is applied.
    dep.owui.add_user("bob@x.org", "Bob")
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(ROOT), email="bob@x.org", name="Bob", password=PASSWORD,
                               grants=[("finance", "ledger")])
    assert e.value.code == "account_exists"
    assert not dep.gs.can("bob@x.org", "finance", "ledger")


def _adds(dep, email):
    return [b for m, p, b in dep.owui.requests if p == "/api/v1/auths/add" and b["email"] == email]


def _fail_grants_once(dep, monkeypatch, *, record_too=False):
    """The store refuses the bind-with-grants transaction once. With
    `record_too` the follow-up bind without access fails as well (store down)."""
    original = dep.gs.bind_new_account
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if k.get("grants") or record_too:
            raise RuntimeError("database went away")
        return original(*a, **k)

    monkeypatch.setattr(dep.gs, "bind_new_account", flaky)
    return lambda: monkeypatch.setattr(dep.gs, "bind_new_account", original)


def test_failed_grant_keeps_the_account_and_a_retry_grants_to_it(dep, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    restore = _fail_grants_once(dep, monkeypatch)
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=PASSWORD,
                               grants=[("finance", "ledger")])
    restore()
    # Accurate: the account exists, access does not, and nothing was "rolled back".
    assert e.value.code == "partial" and "was created, but access was not granted" in e.value.message
    assert "removed" not in e.value.message and PASSWORD not in e.value.message
    assert e.value.extra == {"account": {"subject": "ann@x.org", "name": "Ann", "sign_in": "password"},
                             "access_granted": False, "recorded": True,
                             "reason": "Access could not be saved."}
    user = dep.owui.by_email("ann@x.org")
    assert user and not any(m == "DELETE" for m, _, _ in dep.owui.requests)
    assert dep.gs.identity("ann@x.org")["owui_id"] == user["id"]
    assert not dep.gs.can("ann@x.org", "finance", "use_hub")
    assert "account_create" in [r["action"] for r in dep.gs.read_access_audit(20, subject="ann@x.org")]
    # Creating again is refused as a duplicate; granting to the account works.
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=PASSWORD,
                               grants=[("finance", "ledger")])
    assert e.value.code == "account_exists"
    out = dep.svc.grant_existing_account(actor(ROOT), email="ann@x.org",
                                         grants=[("finance", "ledger")])
    assert out["grants"] == {"finance": ["ledger"]}
    assert dep.gs.can("ann@x.org", "finance", "ledger")
    assert len(_adds(dep, "ann@x.org")) == 1  # never created twice
    assert PASSWORD not in _everything_stored(dep) and PASSWORD not in caplog.text


def test_retry_finds_an_account_the_store_could_not_record(dep, monkeypatch):
    restore = _fail_grants_once(dep, monkeypatch, record_too=True)
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(DELEGATE), email="ann@x.org", name="Ann",
                               password=PASSWORD, grants=[("finance", "ledger")])
    restore()
    assert e.value.code == "partial" and e.value.extra["recorded"] is False
    assert dep.gs.identity("ann@x.org") is None and dep.owui.by_email("ann@x.org")
    # The retry by the same delegate finds the account in the chat app.
    dep.svc.grant_existing_account(actor(DELEGATE), email="Ann@X.org",
                                   grants=[("finance", "ledger")])
    assert dep.gs.identity("ann@x.org")["owui_id"] == dep.owui.by_email("ann@x.org")["id"]
    assert dep.gs.can("ann@x.org", "finance", "ledger")
    assert len(_adds(dep, "ann@x.org")) == 1


def test_uncertain_create_then_retry_detects_the_account(dep):
    """The chat app created the account but its answer was lost. Retrying
    finds it, never duplicates it, and grants access to it."""
    lost = {"n": 0}

    def answer_lost(request):
        response = dep.owui(request)
        if request.url.path == "/api/v1/auths/add" and not lost["n"]:
            lost["n"] += 1
            raise httpx.ReadTimeout("answer lost", request=request)
        return response

    directory = OwuiAccounts("http://owui.internal", SERVICE, "svc-secret",
                             transport=httpx.MockTransport(answer_lost))
    from hubzoid.access.service import AccessService

    svc = AccessService(dep.hub_dir, accounts=directory)
    with pytest.raises(Denied) as e:
        svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=PASSWORD,
                           grants=[("finance", "ledger")])
    assert (e.value.status, e.value.code) == (503, "uncertain")
    assert "won't be created twice" in e.value.message
    with pytest.raises(Denied) as e:
        svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=PASSWORD,
                           grants=[("finance", "ledger")])
    assert e.value.code == "account_exists"
    svc.grant_existing_account(actor(ROOT), email="ann@x.org", grants=[("finance", "ledger")])
    assert dep.gs.can("ann@x.org", "finance", "ledger")
    assert sum(u["email"] == "ann@x.org" for u in dep.owui.users.values()) == 1


def test_ceiling_shrinking_mid_create_leaves_an_account_without_that_access(dep, monkeypatch):
    original = dep.directory.create

    def create_then_shrink(**kwargs):
        out = original(**kwargs)
        dep.gs.revoke(DELEGATE, "finance", "ledger", actor="test")
        return out

    monkeypatch.setattr(dep.directory, "create", create_then_shrink)
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(DELEGATE), email="ann@x.org", name="Ann",
                               password=PASSWORD, grants=[("finance", "ledger")])
    assert e.value.code == "partial" and "Outside your access" in e.value.message
    assert dep.owui.by_email("ann@x.org") and dep.gs.identity("ann@x.org")["owui_id"]
    assert not dep.gs.can("ann@x.org", "finance", "ledger")
    # The retry is checked against the ceiling as it is now.
    with pytest.raises(Denied) as e:
        dep.svc.grant_existing_account(actor(DELEGATE), email="ann@x.org",
                                       grants=[("finance", "ledger")])
    assert e.value.code == "outside_ceiling"
    dep.svc.grant_existing_account(actor(DELEGATE), email="ann@x.org",
                                   grants=[("finance", "use_hub")])
    assert dep.gs.can("ann@x.org", "finance", "use_hub")


def test_initial_access_is_per_agent_and_checked_before_creating(dep):
    """Nobody creates an organization administrator through Add user."""
    for who in (ROOT, DELEGATE):
        with pytest.raises(Denied) as e:
            dep.svc.create_account(actor(who), email="boss@x.org", name="Boss",
                                   password=PASSWORD, grants=[("*", "manage_access")])
        assert e.value.status in (403, 422)
    assert not _adds(dep, "boss@x.org") and not dep.gs.can("boss@x.org", "*", "manage_access")


# ---- existing accounts ------------------------------------------------------------

def test_grant_to_an_existing_account(dep):
    uid = dep.owui.add_user("bob@x.org", "Bob")
    dep.gs.upsert_identity(email="bob@x.org", owui_id=uid, display="Bob")
    out = dep.svc.grant_existing_account(actor(DELEGATE), email="bob@x.org",
                                         grants=[("finance", "ledger")])
    assert out == {"subject": "bob@x.org", "name": "Bob", "grants": {"finance": ["ledger"]}}
    assert dep.gs.can("bob@x.org", "finance", "ledger")
    assert not any(p == "/api/v1/auths/add" for _, p, _ in dep.owui.requests)


@pytest.mark.parametrize("subject,grants,code", [
    ("bob@x.org", [("finance", "payroll")], "outside_ceiling"),
    ("bob@x.org", [("ops", "use_hub")], "forbidden"),
    ("bob@x.org", [("finance", "manage_access")], "forbidden"),
    ("bob@x.org", [("*", "manage_access")], "invalid_grant"),
    (DELEGATE, [("finance", "use_hub")], "self_change"),
    (ROOT, [("finance", "use_hub")], "forbidden"),
    ("bob@x.org", [], "grant_required"),
])
def test_delegate_ceiling_on_existing_accounts(dep, subject, grants, code):
    for email in ("bob@x.org", DELEGATE, ROOT):
        uid = dep.owui.add_user(email, email)
        dep.gs.upsert_identity(email=email, owui_id=uid)
    before = dep.gs.revision()
    with pytest.raises(Denied) as e:
        dep.svc.grant_existing_account(actor(DELEGATE), email=subject, grants=grants)
    assert e.value.code == code
    assert dep.gs.revision() == before


def test_existing_account_path_never_grants_to_an_email_without_an_account(dep):
    with pytest.raises(Denied) as e:
        dep.svc.grant_existing_account(actor(ROOT), email="nobody@x.org",
                                       grants=[("finance", "use_hub")])
    assert (e.value.status, e.value.code) == (404, "no_account")
    assert dep.gs.identity("nobody@x.org") is None
    assert not dep.gs.can("nobody@x.org", "finance", "use_hub")


def test_existing_account_grants_are_reported_agent_by_agent(dep, monkeypatch):
    uid = dep.owui.add_user("bob@x.org", "Bob")
    dep.gs.upsert_identity(email="bob@x.org", owui_id=uid)
    original = dep.svc.apply_access_change

    def ops_fails(actor_, subject, hub, ops, **kw):
        if hub == "ops":
            raise Denied(503, "store_unavailable", "Access data is unavailable.")
        return original(actor_, subject, hub, ops, **kw)

    monkeypatch.setattr(dep.svc, "apply_access_change", ops_fails)
    with pytest.raises(Denied) as e:
        dep.svc.grant_existing_account(actor(ROOT), email="bob@x.org",
                                       grants=[("finance", "ledger"), ("ops", "inventory")])
    assert e.value.code == "partial_access"
    assert e.value.extra == {"granted": {"finance": ["ledger"]}, "failed": ["ops"]}
    assert dep.gs.can("bob@x.org", "finance", "ledger")


# ---- Google sign-in only ---------------------------------------------------------------

def _sign_in(dep, **flags):
    """Record the deployment's sign-in flags in its manifest, as the gateway does."""
    manifest = json.loads((dep.hub_dir / ".hubzoid" / "deployment.json").read_text())["manifest"]
    data = json.loads(open(manifest).read())
    data["sign_in"] = flags
    open(manifest, "w").write(json.dumps(data))


def test_google_sign_in_only_is_offered_only_when_it_can_attach(dep):
    assert dep.svc.sign_in_options() == {"password": True, "google": False}
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", sign_in="google",
                               grants=[("finance", "use_hub")])
    assert e.value.code == "google_unavailable" and not _adds(dep, "ann@x.org")
    for flags in (dict(google=True, merge_by_email=False), dict(google=False, merge_by_email=True),
                  dict(google=True, merge_by_email=True, oauth_settings_in_app=True)):
        _sign_in(dep, **flags)
        assert dep.svc.sign_in_options()["google"] is False, flags
    _sign_in(dep, google=True, merge_by_email=True)
    assert dep.svc.sign_in_options() == {"password": True, "google": True}
    _sign_in(dep, google=True, merge_by_email=True, allowed_domains=["x.org"])
    assert dep.svc.sign_in_options()["google_domains"] == ["x.org"]
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(ROOT), email="ann@elsewhere.org", name="Ann",
                               sign_in="google", grants=[("finance", "use_hub")])
    assert e.value.code == "google_domain" and not _adds(dep, "ann@elsewhere.org")


def test_google_account_has_a_password_nobody_knows(dep, caplog):
    caplog.set_level(logging.DEBUG)
    _sign_in(dep, google=True, merge_by_email=True)
    with pytest.raises(Denied) as e:  # the administrator never sets one
        dep.svc.create_account(actor(DELEGATE), email="ann@x.org", name="Ann", sign_in="google",
                               password=PASSWORD, grants=[("finance", "ledger")])
    assert e.value.code == "invalid_request"
    out = dep.svc.create_account(actor(DELEGATE), email="ann@x.org", name="Ann",
                                 sign_in="google", grants=[("finance", "ledger")])
    dep.svc.create_account(actor(ROOT), email="bob@x.org", name="Bob", sign_in="google", grants=[])
    sent = [_adds(dep, e)[0]["password"] for e in ("ann@x.org", "bob@x.org")]
    # Random, different each time, within bcrypt's limit and Open WebUI's strength rule.
    assert sent[0] != sent[1] and all(len(p.encode()) <= 72 for p in sent)
    strong = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^\w\s]).{8,}$")
    assert all(strong.match(p) for p in sent)
    assert out["sign_in"] == "google" and dep.gs.can("ann@x.org", "finance", "ledger")
    for secret in sent:
        assert secret not in repr(out) and secret not in _everything_stored(dep)
        assert secret not in caplog.text


def test_password_accounts_are_unchanged_by_google_settings(dep):
    _sign_in(dep, google=True, merge_by_email=True)
    out = dep.svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=PASSWORD,
                                 grants=[("finance", "use_hub")])
    assert out["sign_in"] == "password" and _adds(dep, "ann@x.org")[0]["password"] == PASSWORD


def test_adapter_finds_an_account_by_exact_email():
    fake = FakeOwui()
    for i in range(40):  # substring matches fill the first pages
        fake.add_user(f"joann@x.org.{i}.example", f"Filler {i}")
    fake.add_user("ann@x.org", "Ann")
    adapter = _adapter(fake)
    assert adapter.find("ANN@x.org")["email"] == "ann@x.org"
    assert adapter.find("nobody@x.org") is None
    assert adapter.find("ann@x.org.1") is None  # substring only, never a partial match


def test_recreating_a_deleted_accounts_email(dep):
    dep.gs.upsert_identity(email="old@x.org", owui_id="u-gone")
    dep.gs.reconcile_accounts([])  # its chat account vanished
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(DELEGATE), email="old@x.org", name="Old",
                               password=PASSWORD, grants=[("finance", "use_hub")])
    assert e.value.code == "account_replaced"
    dep.svc.create_account(actor(ROOT), email="old@x.org", name="Old", password=PASSWORD,
                           grants=[("finance", "use_hub")])
    ident = dep.gs.identity("old@x.org")
    assert ident["owui_id"] != "u-gone" and dep.gs.can("old@x.org", "finance", "use_hub")
    actions = [r["action"] for r in dep.gs.read_access_audit(20, subject="old@x.org")]
    assert "account_replaced" in actions and "account_create" in actions


def test_blocked_email_is_not_recreated(dep):
    dep.gs.suspend("ann@x.org", actor="test")
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=PASSWORD,
                               grants=[])
    assert e.value.code == "blocked"


@pytest.mark.parametrize("password", ["", "short", "x" * 73])
def test_password_rules(dep, password):
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=password,
                               grants=[])
    assert e.value.code == "invalid_password" and (not password or password not in e.value.message)


# ---- account-wide actions (organization administrators) --------------------------------

def _bound(dep, email="ann@x.org", role="user"):
    uid = dep.owui.add_user(email, email.split("@")[0], role=role)
    dep.gs.upsert_identity(email=email, owui_id=uid, pending=role == "pending")
    return uid


def test_password_reset(dep, caplog):
    caplog.set_level(logging.DEBUG)
    uid = _bound(dep)
    with pytest.raises(Denied) as e:
        dep.svc.set_password(actor(DELEGATE), "ann@x.org", PASSWORD)
    assert e.value.status == 403
    dep.svc.set_password(actor(ROOT), "ann@x.org", PASSWORD)
    assert dep.owui.users[uid].get("password_changed")
    assert "account_password_reset" in [r["action"] for r in dep.gs.read_access_audit(5)]
    assert PASSWORD not in _everything_stored(dep) and PASSWORD not in caplog.text


def test_account_actions_refuse_self_service_account_and_stale_links(dep):
    _bound(dep, ROOT)
    _bound(dep, SERVICE.replace("svc", "svc2"))
    with pytest.raises(Denied) as e:
        dep.svc.set_password(actor(ROOT), ROOT, PASSWORD)
    assert e.value.code == "self_change"
    dep.gs.upsert_identity(email=SERVICE, owui_id=dep.owui.by_email(SERVICE)["id"])
    with pytest.raises(Denied) as e:
        dep.svc.delete_account(actor(ROOT), SERVICE)
    assert e.value.code == "service_account"
    dep.gs.upsert_identity(email="ghost@x.org", owui_id="u-missing")
    with pytest.raises(Denied) as e:
        dep.svc.set_password(actor(ROOT), "ghost@x.org", PASSWORD)
    assert e.value.code == "account_changed"
    with pytest.raises(Denied) as e:
        dep.svc.set_password(actor(ROOT), "never@x.org", PASSWORD)
    assert e.value.code == "no_account"


def test_approve_and_role(dep):
    uid = _bound(dep, role="pending")
    with pytest.raises(Denied):
        dep.svc.set_chat_role(actor(ROOT), "ann@x.org", "admin")  # approve first
    dep.svc.approve_account(actor(ROOT), "ann@x.org")
    assert dep.owui.users[uid]["role"] == "user"
    assert not dep.gs.is_suspended("ann@x.org")
    with pytest.raises(Denied) as e:
        dep.svc.approve_account(actor(ROOT), "ann@x.org")
    assert e.value.code == "not_pending"
    dep.svc.set_chat_role(actor(ROOT), "ann@x.org", "admin")
    assert dep.owui.users[uid]["role"] == "admin"
    with pytest.raises(Denied):
        dep.svc.set_chat_role(actor(ROOT), "ann@x.org", "pending")
    with pytest.raises(Denied):
        dep.svc.set_chat_role(actor(DELEGATE), "ann@x.org", "user")


def test_delete_revokes_then_deletes(dep):
    uid = _bound(dep)
    dep.gs.grant("ann@x.org", "finance", "ledger", actor="test")
    with pytest.raises(Denied):
        dep.svc.delete_account(actor(DELEGATE), "ann@x.org")
    dep.svc.delete_account(actor(ROOT), "ann@x.org")
    assert uid not in dep.owui.users
    assert all(g[0] != "ann@x.org" for g in dep.gs.list_grants())
    assert dep.gs.is_suspended("ann@x.org")  # unavailable until re-created
    actions = [r["action"] for r in dep.gs.read_access_audit(5, subject="ann@x.org")]
    assert "revoke_all" in actions and "account_delete" in actions


def test_delete_another_org_admin(dep):
    uid = _bound(dep, "co@x.org")
    dep.gs.grant("co@x.org", "*", "manage_access", actor="test")
    dep.svc.delete_account(actor(ROOT), "co@x.org")
    assert uid not in dep.owui.users and not dep.gs.can("co@x.org", "*", "manage_access")


def test_delete_stops_when_last_admin_guard_trips(dep, monkeypatch):
    from hubzoid.access.store import LastAdminError

    uid = _bound(dep, "co@x.org")

    def guard(*_a, **_k):
        raise LastAdminError("cannot remove the last org admin; grant another first")

    monkeypatch.setattr(dep.gs, "revoke_all", guard)
    with pytest.raises(Denied) as e:
        dep.svc.delete_account(actor(ROOT), "co@x.org")
    assert (e.value.status, e.value.code) == (409, "last_admin")
    assert uid in dep.owui.users  # the chat account is untouched


def test_delete_when_chat_app_fails_reports_partial(dep):
    _bound(dep)
    dep.gs.grant("ann@x.org", "finance", "ledger", actor="test")
    dep.owui.fail["delete"] = 500
    with pytest.raises(Denied) as e:
        dep.svc.delete_account(actor(ROOT), "ann@x.org")
    assert e.value.code == "partial" and "Access was removed" in e.value.message
    assert not dep.gs.can("ann@x.org", "finance", "ledger")


def test_manifest_owui_url_used_by_default(dep):
    assert deployment.read(dep.hub_dir)["owui_url"] == "http://owui.internal"


def test_new_access_reaches_the_chat_picker_at_once(dep, monkeypatch):
    """Release review: a new account's first sign-in showed no agent until the
    30-second sync. Creating it, changing access and blocking now project
    visibility straight away."""
    from hubzoid.access import reconcile

    calls = []
    monkeypatch.setattr(reconcile, "sync_owui", lambda hub_dir: calls.append(hub_dir) or {"state": "ok"})
    dep.svc.create_account(actor(ROOT), email="new@x.org", name="New", password=PASSWORD,
                           grants=[("finance", "use_hub")])
    assert len(calls) == 1
    dep.svc.apply_access_change(actor(ROOT), "new@x.org", "finance", [("grant", "ledger")])
    assert len(calls) == 2
    dep.svc.set_blocked(actor(ROOT), "new@x.org", True)
    assert len(calls) == 3


def test_a_failed_immediate_sync_never_fails_the_saved_change(dep, monkeypatch):
    from hubzoid.access import reconcile

    def boom(hub_dir):
        raise RuntimeError("chat app down")

    monkeypatch.setattr(reconcile, "sync_owui", boom)
    result = dep.svc.create_account(actor(ROOT), email="ok2@x.org", name="Ok", password=PASSWORD,
                                    grants=[("finance", "use_hub")])
    assert result["subject"] == "ok2@x.org" and dep.gs.can("ok2@x.org", "finance", "use_hub")
