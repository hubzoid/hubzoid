"""The secret store's HTTP routes (hubzoid.secretstore.routes): the
deployment list for organization administrators (names and metadata, never
a value), removing a hub or deployment secret with a revision check (a bad or
stale revision refused), a deployment key a person types belongs in .env,
and everyone else is refused: a person without access gets 404 for the hub's
secrets and 403 for the deployment's and for any removal. Uses the hub and
client of tests/test_secretstore.py."""
from __future__ import annotations

import json

from tests import connectors_fakes as f
from tests.test_secretstore import SAME, VALUE, client, hub, isolated_env, put  # noqa: F401


def test_the_deployment_list_names_secrets_without_values(hub):  # noqa: F811
    put(hub, "ERP_TOKEN", scope="deployment")
    r = client(hub).get("/api/secrets")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert [s["name"] for s in body["secrets"]] == ["ERP_TOKEN"] and body["applies"] == "after_restart"
    assert VALUE not in json.dumps(body)


def test_removing_a_secret_checks_its_revision(hub):  # noqa: F811
    put(hub, "ERP_TOKEN")
    put(hub, "SHARED_TOKEN", scope="deployment")
    tc = client(hub)
    for bad, why in (("abc", "is a number"), ("0", "starts at 1")):
        r = tc.delete(f"/api/hubs/deskhub/secrets/ERP_TOKEN?expected_revision={bad}", headers=SAME)
        assert r.status_code == 422 and why in r.json()["detail"]["message"]
    stale = tc.delete("/api/hubs/deskhub/secrets/ERP_TOKEN?expected_revision=7", headers=SAME)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "revision_conflict"
    gone = tc.delete("/api/hubs/deskhub/secrets/ERP_TOKEN?expected_revision=1", headers=SAME)
    assert gone.status_code == 200 and gone.json()["deleted"]["name"] == "ERP_TOKEN"
    assert gone.json()["restart_required"] is True and VALUE not in gone.text
    dep = tc.delete("/api/secrets/SHARED_TOKEN", headers=SAME)
    assert dep.status_code == 200 and dep.json()["deleted"]["name"] == "SHARED_TOKEN"
    again = tc.delete("/api/secrets/SHARED_TOKEN?expected_revision=absent", headers=SAME)
    assert again.status_code == 404 and again.json()["detail"]["code"] == "not_found"
    typed = tc.put("/api/secrets/OPENAI_API_KEY", json={"value": VALUE}, headers=SAME)
    assert typed.status_code == 409 and "the deployment's .env" in typed.json()["detail"]["message"]


def test_everyone_else_is_refused(hub, monkeypatch):  # noqa: F811
    put(hub, "ERP_TOKEN")
    f.accounts(monkeypatch, hub, {"bob@example.com": ("u_bob", "user")})
    tc = client(hub)
    bob = {**SAME, "x-test-user": "bob@example.com"}
    assert tc.get("/api/hubs/deskhub/secrets", headers=bob).status_code == 404       # as if it did not exist
    r = tc.get("/api/secrets", headers=bob)
    assert r.status_code == 403 and "organization administrators" in r.json()["detail"]["message"]
    r = tc.delete("/api/hubs/deskhub/secrets/ERP_TOKEN", headers=bob)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "permission_denied"
    assert tc.get("/api/secrets").status_code == 401                                # nobody signed in
