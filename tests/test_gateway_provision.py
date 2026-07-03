"""Tests for hubzoid.gateway_provision — per-hub Open WebUI seeding.

The provisioner drives OWUI's REST API (the version-stable layer; the DB
schema changed across OWUI releases) to give each gateway hub its own model
entry: picker name, description, quick-start suggestions, avatar, and a
per-team group with read access. Tested against a stateful fake OWUI built
on httpx.MockTransport — request shapes mirror OWUI 0.9.6's routers.

Safety contract under test:
  * idempotent — second boot updates, never duplicates
  * updates never send access_grants (admin ACL edits survive re-boots)
  * per-hub failures skip that hub, never abort the rest
  * bad credentials raise ProvisionError (caller logs and moves on)
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from hubzoid import gateway_provision as gwp


# ---------------------------------------------------------------------------
# Stateful fake OWUI (0.9.6 API shapes)
# ---------------------------------------------------------------------------
class FakeOWUI:
    def __init__(self, *, users=None, fail_paths=()):
        self.users = dict(users or {})       # email -> password
        self.groups: dict[str, dict] = {}     # id -> {id, name, description}
        self.models: dict[str, dict] = {}     # id -> model row (form shape)
        self.grants: dict[str, list] = {}     # model id -> access_grants as sent
        self.requests: list[tuple[str, str]] = []   # (method, path) log
        self.fail_paths = set(fail_paths)     # exact paths that return 500
        self._seq = 0

    def _id(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if path in self.fail_paths:
            return httpx.Response(500, json={"detail": "boom"})
        body = json.loads(request.content) if request.content else {}

        if path == "/api/v1/auths/signin":
            if self.users.get(body.get("email")) == body.get("password"):
                return httpx.Response(200, json={"token": "tok", "role": "admin"})
            return httpx.Response(400, json={"detail": "bad creds"})

        if path == "/api/v1/auths/signup":
            if self.users:  # not the first user -> normal (non-admin) signup
                return httpx.Response(403, json={"detail": "signup disabled"})
            self.users[body["email"]] = body["password"]
            return httpx.Response(200, json={"token": "tok", "role": "admin"})

        if path == "/api/v1/groups/" and request.method == "GET":
            return httpx.Response(200, json=list(self.groups.values()))

        if path == "/api/v1/groups/create":
            gid = self._id("g")
            self.groups[gid] = {"id": gid, "name": body["name"],
                                "description": body.get("description", "")}
            return httpx.Response(200, json=self.groups[gid])

        if path == "/api/v1/models/model" and request.method == "GET":
            mid = request.url.params.get("id")
            if mid in self.models:
                return httpx.Response(200, json=self.models[mid])
            return httpx.Response(404, json={"detail": "not found"})

        if path == "/api/v1/models/create":
            self.models[body["id"]] = body
            self.grants[body["id"]] = body.get("access_grants") or []
            return httpx.Response(200, json=body)

        if path == "/api/v1/models/model/update":
            if body["id"] not in self.models:
                return httpx.Response(401, json={"detail": "not found"})
            self.models[body["id"]] = body
            # Mirrors OWUI: grants replaced ONLY when access_grants is not None.
            if body.get("access_grants") is not None:
                self.grants[body["id"]] = body["access_grants"]
            return httpx.Response(200, json=body)

        return httpx.Response(404, json={"detail": f"unhandled {path}"})


def _specs(tmp_path, *, logo=False):
    logo_path = None
    if logo:
        logo_path = tmp_path / "logo.png"
        logo_path.write_bytes(b"PNGBYTES")
    return [
        gwp.HubSpec(model_id="irs-agent", name="IRS Agent", group="irs",
                    suggestions=("How do I file?", "What is TDS?"),
                    description="Tax helper", logo=logo_path),
        gwp.HubSpec(model_id="gpms-agent", name="GPMS", group="gpms"),
    ]


def _provision(fake, hubs, *, allow_bootstrap=True):
    client = httpx.Client(transport=fake.transport(), base_url="http://gw:8080")
    return gwp.provision(base_url="http://gw:8080", email="a@x.com",
                         password="pw", hubs=hubs, client=client,
                         allow_bootstrap=allow_bootstrap)


# ---------------------------------------------------------------------------
# create path
# ---------------------------------------------------------------------------
def test_provision_creates_groups_and_models(tmp_path):
    fake = FakeOWUI(users={"a@x.com": "pw"})
    _provision(fake, _specs(tmp_path))

    # One group per hub.
    assert sorted(g["name"] for g in fake.groups.values()) == ["gpms", "irs"]

    # One model row per hub, meta carrying the per-hub identity.
    irs = fake.models["irs-agent"]
    assert irs["name"] == "IRS Agent"
    assert irs["meta"]["description"] == "Tax helper"
    assert irs["meta"]["suggestion_prompts"] == [
        {"content": "How do I file?"}, {"content": "What is TDS?"},
    ]

    # Private ACL: the hub's group got read access.
    gid = next(g["id"] for g in fake.groups.values() if g["name"] == "irs")
    assert fake.grants["irs-agent"] == [
        {"principal_type": "group", "principal_id": gid, "permission": "read"},
    ]


def test_provision_embeds_logo_as_data_uri(tmp_path):
    fake = FakeOWUI(users={"a@x.com": "pw"})
    _provision(fake, _specs(tmp_path, logo=True))
    url = fake.models["irs-agent"]["meta"]["profile_image_url"]
    assert url == "data:image/png;base64," + base64.b64encode(b"PNGBYTES").decode()


def test_provision_omits_empty_fields(tmp_path):
    """A hub with no suggestions/description/logo gets a bare model row —
    no empty keys that would shadow OWUI defaults, and no error."""
    fake = FakeOWUI(users={"a@x.com": "pw"})
    _provision(fake, _specs(tmp_path))
    meta = fake.models["gpms-agent"]["meta"]
    assert "suggestion_prompts" not in meta
    assert "profile_image_url" not in meta
    assert "description" not in meta


# ---------------------------------------------------------------------------
# update path (second boot): idempotent, ACL never clobbered
# ---------------------------------------------------------------------------
def test_provision_second_run_updates_not_duplicates(tmp_path):
    fake = FakeOWUI(users={"a@x.com": "pw"})
    _provision(fake, _specs(tmp_path))
    creates_before = sum(1 for m, p in fake.requests if p == "/api/v1/models/create")

    _provision(fake, _specs(tmp_path))
    creates_after = sum(1 for m, p in fake.requests if p == "/api/v1/models/create")
    assert creates_after == creates_before          # no duplicate creates
    assert len(fake.groups) == 2                    # no duplicate groups


def test_provision_update_never_sends_access_grants(tmp_path):
    """Admin-edited ACLs must survive re-boots: updates omit access_grants
    (OWUI preserves grants when the field is null)."""
    fake = FakeOWUI(users={"a@x.com": "pw"})
    _provision(fake, _specs(tmp_path))
    # Admin tightens the ACL by hand between boots.
    fake.grants["irs-agent"] = [{"principal_type": "user", "principal_id": "u9",
                                 "permission": "read"}]

    _provision(fake, _specs(tmp_path))
    assert fake.models["irs-agent"].get("access_grants") is None
    assert fake.grants["irs-agent"] == [{"principal_type": "user",
                                         "principal_id": "u9",
                                         "permission": "read"}]


def test_provision_update_preserves_admin_meta_keys(tmp_path):
    """Meta is merged, not replaced: keys the admin set in OWUI's model editor
    (e.g. capabilities) survive; hubzoid only refreshes its own fields."""
    fake = FakeOWUI(users={"a@x.com": "pw"})
    _provision(fake, _specs(tmp_path))
    fake.models["irs-agent"]["meta"]["capabilities"] = {"vision": True}

    _provision(fake, _specs(tmp_path))
    meta = fake.models["irs-agent"]["meta"]
    assert meta["capabilities"] == {"vision": True}
    assert meta["description"] == "Tax helper"


# ---------------------------------------------------------------------------
# auth + failure posture
# ---------------------------------------------------------------------------
def test_provision_bootstraps_first_admin_via_signup(tmp_path):
    """Fresh gateway DB (no users): signin fails, signup creates the first
    user, which OWUI auto-promotes to admin."""
    fake = FakeOWUI(users={})
    _provision(fake, _specs(tmp_path))
    assert "a@x.com" in fake.users
    assert "irs-agent" in fake.models


def test_provision_bad_credentials_raise(tmp_path):
    fake = FakeOWUI(users={"a@x.com": "OTHER"})   # wrong pw, signup refused
    with pytest.raises(gwp.ProvisionError):
        _provision(fake, _specs(tmp_path))


def test_provision_established_gateway_never_signs_up(tmp_path):
    """allow_bootstrap=False (data dir already had a webui.db): a failed signin
    must raise a clear credential error WITHOUT attempting signup — otherwise a
    typo'd password quietly creates a stray login-capable account."""
    fake = FakeOWUI(users={"a@x.com": "OTHER"})
    with pytest.raises(gwp.ProvisionError, match="sign in"):
        _provision(fake, _specs(tmp_path), allow_bootstrap=False)
    assert ("POST", "/api/v1/auths/signup") not in fake.requests


def test_probe_error_takes_neither_create_nor_update_path(tmp_path):
    """A transient probe failure (500) must NOT be read as 'model missing':
    the create path re-sends access_grants and could clobber an admin's
    hand-tightened ACL. Skip the hub this boot instead."""
    fake = FakeOWUI(users={"a@x.com": "pw"}, fail_paths={"/api/v1/models/model"})
    actions = _provision(fake, [gwp.HubSpec(model_id="irs-agent", name="IRS", group="irs")])
    assert "irs-agent" not in fake.models
    assert ("POST", "/api/v1/models/create") not in fake.requests
    assert any("skip" in a.lower() for a in actions)


def test_provision_update_removes_stale_identity_fields(tmp_path):
    """AGENTS.md is the source of truth for identity: a description or
    suggestions REMOVED from the hub disappear from OWUI on next boot
    (admin-owned keys like capabilities still survive — see the merge test)."""
    fake = FakeOWUI(users={"a@x.com": "pw"})
    _provision(fake, _specs(tmp_path))       # seeds description + suggestions
    fake.models["irs-agent"]["meta"]["capabilities"] = {"vision": True}

    bare = [gwp.HubSpec(model_id="irs-agent", name="IRS Agent", group="irs")]
    _provision(fake, bare)
    meta = fake.models["irs-agent"]["meta"]
    assert "description" not in meta
    assert "suggestion_prompts" not in meta
    assert meta["capabilities"] == {"vision": True}


def test_provision_one_bad_hub_does_not_block_others(tmp_path):
    """A per-hub failure (here: group create 500s for the first hub) skips
    that hub and still provisions the rest."""
    fake = FakeOWUI(users={"a@x.com": "pw"}, fail_paths={"/api/v1/groups/create"})
    # First hub needs a group create (fails); second hub's group pre-exists.
    fake.groups["g0"] = {"id": "g0", "name": "gpms", "description": ""}

    actions = _provision(fake, _specs(tmp_path))
    assert "irs-agent" not in fake.models       # skipped
    assert "gpms-agent" in fake.models          # still provisioned
    assert any("skip" in a.lower() for a in actions)
