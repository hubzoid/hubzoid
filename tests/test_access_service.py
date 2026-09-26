"""The authorization service: scope, the delegate ceiling and access changes.

No model, no network: a two-hub deployment on SQLite and, where accounts are
involved, a fake Open WebUI served through httpx.MockTransport. Helpers here
are reused by the account, change-request, portal and tool tests.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest

from hubzoid import deployment
import hubzoid.access as access
from hubzoid.access.accounts import OwuiAccounts
from hubzoid.access.service import AccessService, Actor, Denied

ROOT = "root@x.org"
DELEGATE = "dele@x.org"
SERVICE = "svc@x.org"


class FakeOwui:
    """Open WebUI's account admin API, in memory. Records every request body so
    tests can assert what was sent (and that nothing leaks elsewhere)."""

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict]] = []
        self.fail: dict[str, int] = {}  # "add"|"update"|"delete" -> HTTP status
        self.reject_password = False
        self.down = False
        self.add_user(SERVICE, "Service", role="admin")

    def add_user(self, email, name, role="user"):
        uid = str(uuid.uuid4())
        self.users[uid] = dict(id=uid, email=email, name=name, role=role)
        return uid

    def by_email(self, email):
        return next((u for u in self.users.values() if u["email"] == email), None)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down", request=request)
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        self.requests.append((request.method, path, body))
        if path == "/api/v1/auths/signin":
            ok = body == {"email": SERVICE, "password": "svc-secret"}
            return httpx.Response(200 if ok else 400, json={"token": "svc-token"} if ok else {})
        if request.headers.get("authorization") != "Bearer svc-token":
            return httpx.Response(401, json={"detail": "Not authenticated"})
        if path == "/api/v1/auths/add":
            if "add" in self.fail:
                return httpx.Response(self.fail["add"], json={"detail": "boom"})
            if self.by_email(body["email"]):
                return httpx.Response(400, json={"detail": "Uh-oh! This email is already registered."})
            if self.reject_password:
                return httpx.Response(400, json={"detail": f"Invalid password {body['password']}"})
            uid = self.add_user(body["email"], body["name"], role=body.get("role", "pending"))
            return httpx.Response(200, json={**self.users[uid], "token": "new-user-token"})
        parts = path.strip("/").split("/")
        if parts == ["api", "v1", "users"] and request.method == "GET":
            # The admin list: substring search on name and email, 30 per page.
            query = (request.url.params.get("query") or "").lower()
            page = int(request.url.params.get("page") or 1)
            hits = [u for u in self.users.values()
                    if query in u["email"].lower() or query in u["name"].lower()]
            return httpx.Response(200, json={"users": hits[(page - 1) * 30:page * 30],
                                             "total": len(hits)})
        if parts[:3] == ["api", "v1", "users"] and len(parts) >= 4:
            uid = parts[3]
            user = self.users.get(uid)
            if request.method == "GET":
                return httpx.Response(200, json=user) if user else httpx.Response(
                    400, json={"detail": "not found"})
            if request.method == "DELETE":
                if "delete" in self.fail:
                    return httpx.Response(self.fail["delete"], json={"detail": "boom"})
                if not user:
                    return httpx.Response(400, json={"detail": "not found"})
                del self.users[uid]
                return httpx.Response(200, json=True)
            if parts[4:] == ["update"]:
                if "update" in self.fail:
                    return httpx.Response(self.fail["update"], json={"detail": "boom"})
                if not user:
                    return httpx.Response(400, json={"detail": "not found"})
                for k in ("role", "name"):
                    if k in body:
                        user[k] = body[k]
                if "password" in body:
                    user["password_changed"] = True
                return httpx.Response(200, json=user)
        return httpx.Response(404, json={"detail": "no route"})


def make_deployment(tmp_path, monkeypatch, *, owui: FakeOwui | None = None):
    """finance (ledger, payroll) and ops (inventory), both managed. ROOT is the
    organization administrator; DELEGATE manages finance and holds ledger."""
    for key in ("HUBZOID_OPERATIONAL_DB", "DATABASE_URL", "HUBZOID_DEPLOYMENT",
                "OWUI_INTERNAL_URL", "WEBUI_URL", "HUBZOID_PUBLIC_URL",
                "HUBZOID_GATEWAY_ADMIN_EMAIL", "HUBZOID_GATEWAY_ADMIN_PASSWORD",
                "HUBZOID_RESTRICTED_SURFACES", "HUBZOID_MANAGEMENT_TOOLS",
                "HUBZOID_CHANGE_REQUEST_TTL", "HUBZOID_PORTAL_DEV", "HUBZOID_PORTAL_DEV_USER"):
        monkeypatch.delenv(key, raising=False)
    access._stores.clear()
    dirs = {}
    for name, perms in (("finance", ("ledger", "payroll")), ("ops", ("inventory",))):
        d = tmp_path / name
        (d / "restricted").mkdir(parents=True)
        for perm in perms:
            (d / "restricted" / f"{perm}.py").write_text("# permission declaration")
        dirs[name] = d
    deployment.save(
        tmp_path / "gateway" / "deployment.json",
        hubs=[dict(key=n, name=n.title(), path=str(d), model_id=n) for n, d in dirs.items()],
        operational_url=f"sqlite:///{tmp_path}/ops.db",
        owui_url="http://owui.internal",
        owui_db=str(tmp_path / "owui.db"),
    )
    gs = access.store_for(dirs["finance"])
    gs.bootstrap([ROOT])
    for n in dirs:
        gs.set_authoritative(True, hub=n)
    gs.grant(DELEGATE, "finance", "manage_access", actor="test")
    gs.grant(DELEGATE, "finance", "ledger", actor="test")
    owui = owui or FakeOwui()
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_EMAIL", SERVICE)
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_PASSWORD", "svc-secret")
    directory = OwuiAccounts("http://owui.internal", SERVICE, "svc-secret",
                             transport=httpx.MockTransport(owui))
    svc = AccessService(dirs["finance"], accounts=directory)
    return SimpleNamespace(dirs=dirs, gs=gs, svc=svc, owui=owui, directory=directory,
                           hub_dir=dirs["finance"])


def actor(subject, surface="console", via="session"):
    return Actor(subject=subject, surface=surface, via=via)


@pytest.fixture
def dep(tmp_path, monkeypatch):
    return make_deployment(tmp_path, monkeypatch)


# ---- scope and ceiling --------------------------------------------------------

def test_scope_from_store_only(dep):
    assert dep.svc.scope(actor(ROOT)).org_admin
    assert set(dep.svc.scope(actor(ROOT)).hubs) == {"finance", "ops"}
    s = dep.svc.scope(actor(DELEGATE))
    assert not s.org_admin and s.hubs == ("finance",)
    assert not dep.svc.scope(actor("nobody@x.org")).any
    assert not dep.svc.scope(actor("workflow:daily")).any
    assert not dep.svc.scope(actor("*")).any


def test_ceiling_matrix(dep):
    svc, gs = dep.svc, dep.gs
    full = {"use_hub", "manage_access", "curator", "ledger", "payroll", "share_public_links", "jev"}
    assert svc.ceiling(actor(ROOT), "finance") == full
    # A delegate: what they hold, never manage_access.
    assert svc.ceiling(actor(DELEGATE), "finance") == {"use_hub", "ledger"}
    with pytest.raises(Denied) as e:
        svc.ceiling(actor(DELEGATE), "ops")
    assert e.value.status == 403
    # A public hub gives the delegate entry, never tools.
    gs.grant("*", "finance", "use_hub", actor="test", carry_over_public=True)
    assert svc.ceiling(actor(DELEGATE), "finance") == {"use_hub", "ledger"}
    # A blocked delegate manages nothing.
    gs.suspend(DELEGATE, actor="test")
    with pytest.raises(Denied):
        svc.ceiling(actor(DELEGATE), "finance")
    assert svc.grantable(actor(DELEGATE)) == {}


def test_grantable_reports_legacy_hubs_as_empty(dep):
    dep.gs.set_authoritative(False, hub="ops")
    g = dep.svc.grantable(actor(ROOT))
    assert g["ops"] == [] and "payroll" in g["finance"]


# ---- delegate rules -------------------------------------------------------------

def test_delegate_grants_within_ceiling(dep):
    dep.svc.apply_access_change(actor(DELEGATE), "ann@x.org", "finance", [("grant", "ledger")])
    assert dep.gs.can("ann@x.org", "finance", "ledger")
    assert dep.gs.can("ann@x.org", "finance", "use_hub")


@pytest.mark.parametrize("subject,hub,ops,status,code", [
    ("ann@x.org", "finance", [("grant", "payroll")], 403, "outside_ceiling"),
    ("ann@x.org", "finance", [("grant", "manage_access")], 403, "forbidden"),
    ("ann@x.org", "ops", [("grant", "use_hub")], 403, "forbidden"),
    (DELEGATE, "finance", [("grant", "use_hub")], 403, "self_change"),
    (ROOT, "finance", [("grant", "ledger")], 403, "forbidden"),
    ("*", "finance", [("grant", "use_hub")], 403, "no_new_everyone"),
    ("ann@x.org", "*", [("grant", "manage_access")], 403, "forbidden"),
    ("ann@x.org", "finance", [("grant", "no_such_tool")], 422, "unknown_permission"),
])
def test_delegate_refusals(dep, subject, hub, ops, status, code):
    before = dep.gs.revision()
    with pytest.raises(Denied) as e:
        dep.svc.apply_access_change(actor(DELEGATE), subject, hub, ops)
    assert (e.value.status, e.value.code) == (status, code)
    assert dep.gs.revision() == before  # nothing written


def test_delegate_self_elevation_is_refused(dep):
    # Neither a tool they lack nor manage rights, even for themselves.
    for ops in ([("grant", "payroll")], [("grant", "manage_access")], [("revoke", "ledger")]):
        with pytest.raises(Denied):
            dep.svc.apply_access_change(actor(DELEGATE), DELEGATE, "finance", ops)
    assert not dep.gs.can(DELEGATE, "finance", "payroll")


def test_revoke_symmetry(dep):
    gs, svc = dep.gs, dep.svc
    gs.grant("ann@x.org", "finance", "ledger", actor="test")
    gs.grant("bob@x.org", "finance", "ledger", actor="test")
    gs.grant("bob@x.org", "finance", "payroll", actor="test")
    # A capability the delegate holds can be removed...
    svc.apply_access_change(actor(DELEGATE), "ann@x.org", "finance", [("revoke", "ledger")])
    assert not gs.can("ann@x.org", "finance", "ledger")
    # ...one they don't hold cannot...
    with pytest.raises(Denied) as e:
        svc.apply_access_change(actor(DELEGATE), "bob@x.org", "finance", [("revoke", "payroll")])
    assert e.value.code == "outside_ceiling"
    # ...and removing all access would remove payroll too, so it is refused.
    with pytest.raises(Denied) as e:
        svc.apply_access_change(actor(DELEGATE), "bob@x.org", "finance", [("revoke", "use_hub")])
    assert e.value.code == "outside_ceiling" and "payroll" in e.value.message
    assert gs.can("bob@x.org", "finance", "payroll")
    # Entry alone can be removed.
    svc.apply_access_change(actor(DELEGATE), "ann@x.org", "finance", [("revoke", "use_hub")])
    assert not gs.can("ann@x.org", "finance", "use_hub")


def test_delegate_cannot_remove_another_manager(dep):
    dep.gs.grant("co@x.org", "finance", "manage_access", actor="test")
    with pytest.raises(Denied) as e:
        dep.svc.apply_access_change(actor(DELEGATE), "co@x.org", "finance", [("revoke", "use_hub")])
    assert e.value.status == 403


def test_workflow_subjects_follow_the_ceiling(dep):
    dep.svc.apply_access_change(actor(DELEGATE), "workflow:close", "finance", [("grant", "ledger")])
    assert dep.gs.can("workflow:close", "finance", "ledger")
    with pytest.raises(Denied):
        dep.svc.apply_access_change(actor(DELEGATE), "workflow:close", "finance",
                                    [("grant", "payroll")])


def test_ceiling_is_live(dep):
    dep.gs.revoke(DELEGATE, "finance", "ledger", actor="test")
    with pytest.raises(Denied) as e:
        dep.svc.apply_access_change(actor(DELEGATE), "ann@x.org", "finance", [("grant", "ledger")])
    assert e.value.code == "outside_ceiling"


# ---- organization administrators and common rules --------------------------------

def test_org_admin_keeps_scope(dep):
    svc, gs = dep.svc, dep.gs
    svc.apply_access_change(actor(ROOT), "ann@x.org", "ops", [("grant", "inventory")])
    with pytest.raises(Denied) as e:  # nobody creates new access for everyone
        svc.apply_access_change(actor(ROOT), "*", "finance", [("grant", "use_hub")])
    assert (e.value.status, e.value.code) == (403, "no_new_everyone")
    svc.apply_access_change(actor(ROOT), "co@x.org", "*", [("grant", "manage_access")])
    assert gs.can("ann@x.org", "ops", "inventory") and gs.can("co@x.org", "*", "manage_access")
    svc.apply_access_change(actor(ROOT), "co@x.org", "*", [("revoke", "manage_access")])
    with pytest.raises(Denied) as e:  # last admin
        svc.apply_access_change(actor(ROOT), ROOT, "*", [("revoke", "manage_access")])
    assert e.value.status == 409


def test_legacy_blocked_and_stale_revision(dep):
    svc, gs = dep.svc, dep.gs
    gs.set_authoritative(False, hub="ops")
    with pytest.raises(Denied) as e:
        svc.apply_access_change(actor(ROOT), "ann@x.org", "ops", [("grant", "use_hub")])
    assert (e.value.status, e.value.code) == (409, "legacy")
    gs.suspend("ann@x.org", actor="test")
    with pytest.raises(Denied) as e:
        svc.apply_access_change(actor(ROOT), "ann@x.org", "finance", [("grant", "use_hub")])
    assert e.value.code == "blocked"
    stale = gs.revision()
    gs.grant("bob@x.org", "finance", "use_hub", actor="test")
    with pytest.raises(Denied) as e:
        svc.apply_access_change(actor(ROOT), "cy@x.org", "finance", [("grant", "use_hub")],
                                expected_revision=stale)
    assert e.value.code == "conflict"


def test_changes_are_audited_with_surface_and_request(dep):
    dep.svc.apply_access_change(actor(DELEGATE, surface="api", via="api-key"), "ann@x.org",
                                "finance", [("grant", "ledger")], request_id="req-1")
    rows = dep.gs.read_access_audit(10, subject="ann@x.org")
    assert {r["action"] for r in rows} == {"grant"}
    assert all(r["surface"] == "api" and r["request_id"] == "req-1" and r["actor"] == DELEGATE
               for r in rows)


def test_store_failure_fails_closed(dep, monkeypatch):
    import hubzoid.access as pkg

    def broken(_):
        raise RuntimeError("db down")

    monkeypatch.setattr(pkg, "store_for", broken)
    with pytest.raises(Denied) as e:
        dep.svc.apply_access_change(actor(ROOT), "ann@x.org", "finance", [("grant", "use_hub")])
    assert e.value.status == 503
