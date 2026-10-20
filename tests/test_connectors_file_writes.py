"""Settings and hand-edited YAML operate on the same connector definitions."""
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml
from dotenv import dotenv_values
from sqlalchemy import text

from hubzoid.connectors import engine, registry, tokens
from tests import connectors_fakes as f
from tests.test_connectors_registry import APP, SAME, body, detail, hub, tc  # noqa: F401


def create(tc, **extra):
    return tc.post("/portal/api/connectors?hub=sales", json=body(**extra), headers=SAME)


def test_create_in_settings_writes_yaml_and_offers_without_granting(hub, tc):
    r = create(tc, tool_allowlist=["search"])
    assert r.status_code == 201, r.text
    c = r.json()["connector"]
    assert c["agents"] == ["sales"] and c["revision"]
    path = hub / "connectors/gmail.yaml"
    doc = yaml.safe_load(path.read_text())
    assert doc["hubs"] == ["sales"] and doc["tools"] == ["search"]
    assert doc["sign_in"] == "oauth"
    from hubzoid.access import store_for
    assert not any(p == "connector_gmail" for _, _, p in store_for(hub).list_grants("sales"))
    # The running registry must notice edits made outside Settings.
    doc["name"] = "Mail changed by hand"
    path.write_text(yaml.safe_dump(doc))
    listing = tc.get("/portal/api/connectors").json()["connectors"]
    assert listing[0]["name"] == "Mail changed by hand"


def test_secrets_live_in_env_and_work_without_restart(hub, tc):
    (hub / ".env").write_text("# operator settings\nUNCHANGED='yes'\n")
    secret = "a secret with 'quotes' and $dollars"
    r = create(tc, client_id="client", client_secret=secret)
    assert r.status_code == 201, r.text
    assert secret not in r.text
    doc = yaml.safe_load((hub / "connectors/gmail.yaml").read_text())
    assert doc["client_secret"].startswith("env:")
    assert secret not in (hub / "connectors/gmail.yaml").read_text()
    assert dotenv_values(hub / ".env")[doc["client_secret"][4:]] == secret
    assert "# operator settings\nUNCHANGED='yes'\n" in (hub / ".env").read_text()
    assert (hub / ".env").stat().st_mode & 0o077 == 0
    assert registry.client_secret(hub, "gmail") == secret
    r = tc.patch("/portal/api/connectors/gmail", json={"name": "Updated"}, headers=SAME)
    assert r.status_code == 200 and registry.client_secret(hub, "gmail") == secret
    assert tc.patch("/portal/api/connectors/gmail", json={"client_secret": "replacement"}, headers=SAME).status_code == 200
    assert registry.client_secret(hub, "gmail") == "replacement"
    assert tc.patch("/portal/api/connectors/gmail", json={"client_secret": None}, headers=SAME).status_code == 200
    assert registry.client_secret(hub, "gmail") is None


def test_shared_key_cannot_follow_a_changed_server(hub, tc):
    r = create(tc, auth_type="shared", shared_secret="first-key")
    assert r.status_code == 201, r.text
    assert registry.shared_headers(hub, "gmail") == {"Authorization": "Bearer first-key"}
    r = tc.patch("/portal/api/connectors/gmail", json={"url": "https://other.example/mcp"}, headers=SAME)
    assert r.status_code == 422 and detail(r)["code"] == "shared_key_required"
    assert registry.get(hub, "gmail").url == body()["url"]
    r = tc.patch("/portal/api/connectors/gmail",
                 json={"url": "https://other.example/mcp", "shared_secret": "new-key"}, headers=SAME)
    assert r.status_code == 200
    assert registry.shared_headers(hub, "gmail") == {"Authorization": "Bearer new-key"}


def test_stale_ui_edit_and_invalid_manual_file_are_not_overwritten(hub, tc):
    assert create(tc).status_code == 201
    c = tc.get("/portal/api/connectors").json()["connectors"][0]
    path = hub / "connectors/gmail.yaml"
    manual = path.read_text().replace("name: Gmail", "name: Manual")
    path.write_text(manual)
    r = tc.patch("/portal/api/connectors/gmail",
                 json={"name": "UI", "expected_revision": c["revision"]}, headers=SAME)
    assert r.status_code == 409 and detail(r)["code"] == "conflict"
    assert path.read_text() == manual
    path.write_text("name: [broken")
    r = tc.patch("/portal/api/connectors/gmail", json={"enabled": False}, headers=SAME)
    assert r.status_code == 422
    assert path.read_text() == "name: [broken"


@pytest.mark.parametrize("change", [
    {"id": "../escape"}, {"url": "ftp://bad"}, {"enabled": "yes"},
    {"auth_type": "shared", "shared_secret": "bad\nheader"},
    {"client_secret": "missing-client-id"}, {"unexpected": True},
    {"name": 123},
])
def test_invalid_create_leaves_no_definition_or_secret(hub, tc, change):
    r = create(tc, **change)
    assert r.status_code == 422, r.text
    assert not list((hub / "connectors").glob("*.yaml"))
    assert not (hub / ".env").exists()


def test_unknown_agent_create_does_not_leave_a_connector(hub, tc):
    r = tc.post("/portal/api/connectors?hub=unknown", json=body(), headers=SAME)
    assert r.status_code == 404
    assert registry.list_all(hub) == []


def test_yml_manual_files_are_edited_in_place_and_ids_do_not_collide(hub, tc):
    f.define(hub, body())
    old = hub / "connectors/gmail.yaml"
    path = old.with_suffix(".yml")
    old.rename(path)
    assert create(tc, id="gmail").status_code == 409
    r = tc.patch("/portal/api/connectors/gmail", json={"enabled": False}, headers=SAME)
    assert r.status_code == 200
    assert yaml.safe_load(path.read_text())["enabled"] is False
    assert not old.exists()


@pytest.mark.parametrize("target", ["connectors", ".env", "connectors/gmail.yaml"])
def test_symlink_targets_cannot_be_written(hub, tc, tmp_path, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    untouched = outside / "untouched"
    untouched.write_text("original")
    path = hub / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside if target == "connectors" else untouched)
    r = create(tc, client_id="client", client_secret="value", id="gmail")
    assert r.status_code in (409, 422), r.text
    assert untouched.read_text() == "original"
    assert list(outside.iterdir()) == [untouched]


def test_concurrent_creates_preserve_all_secrets(hub):
    def add(n):
        return registry.create(hub, body(id=f"mail_{n}", client_id="client",
                                         client_secret=f"secret-{n}"), actor=f.OWNER)
    with ThreadPoolExecutor(max_workers=4) as pool:
        created = list(pool.map(add, range(4)))
    assert len(created) == 4
    assert [registry.client_secret(hub, f"mail_{n}") for n in range(4)] == [
        f"secret-{n}" for n in range(4)]


def test_ui_secret_stays_literal_after_dotenv_is_loaded_on_restart(hub, tc, monkeypatch):
    from dotenv import load_dotenv

    value = "before${MISSING_CONNECTOR_VAR}after"
    assert create(tc, auth_type="shared", shared_secret=value).status_code == 201
    env_values = dotenv_values(hub / ".env", interpolate=False)
    with monkeypatch.context() as restart:
        for key in env_values:
            restart.setenv(key, "beforeafter")
        load_dotenv(hub / ".env", override=True)
        assert registry.shared_headers(hub, "gmail") == {"Authorization": f"Bearer {value}"}


def test_create_uses_deployment_folder_and_all_hubs_see_secret(hub, tc, tmp_path):
    # f.make_hub puts sales inside tmp_path; sibling hub uses the same deployment.
    (tmp_path / "hubzoid.yaml").write_text(
        "version: 1\nhubs: [sales, support]\n")
    other = f.make_hub(tmp_path, "support")
    assert create(tc, auth_type="shared", shared_secret="team-key").status_code == 201
    assert (tmp_path / "connectors/gmail.yaml").is_file()
    assert not (hub / "connectors/gmail.yaml").exists()
    assert registry.shared_headers(other, "gmail") == {"Authorization": "Bearer team-key"}


def test_stale_delete_keeps_file_and_connections(hub, tc):
    c = create(tc).json()["connector"]
    tokens.store(hub, user_id="person", email="person@example.org", connector_id="gmail",
                 token={"v": 1, "kind": "oauth", "access_token": "AT", "url": body()["url"]})
    path = hub / "connectors/gmail.yaml"
    manual = path.read_text().replace("name: Gmail", "name: Manual")
    path.write_text(manual)
    r = tc.delete("/portal/api/connectors/gmail",
                  params={"expected_revision": c["revision"]}, headers=SAME)
    assert r.status_code == 409
    assert path.read_text() == manual
    assert tokens.token_record(hub, "person", "gmail") is not None


def test_changed_sign_in_removes_old_tokens_flows_and_dynamic_client(hub, tc):
    assert create(tc).status_code == 201
    redirect = APP + "/oauth/connectors/gmail/callback"
    registry.save_registration(hub, "gmail", redirect, {"client_id": "dynamic-client"})
    tokens.store(hub, user_id="person", email="person@example.org", connector_id="gmail",
                 token={"v": 1, "kind": "oauth", "access_token": "AT", "url": body()["url"]})
    with engine(hub).begin() as conn:
        conn.execute(text("INSERT INTO hz_connector_flows "
                          "(state, user_id, connector_id, payload_enc, created_at, expires_at) "
                          "VALUES ('pending', 'person', 'gmail', 'fixture', 0, 9999999999)"))
    response = tc.patch("/portal/api/connectors/gmail", json={"auth_type": "none"}, headers=SAME)
    assert response.status_code == 200
    assert tokens.token_record(hub, "person", "gmail") is None
    assert registry.registration(hub, "gmail", redirect) is None
    with engine(hub).connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM hz_connector_flows")).scalar() == 0


def test_admin_and_same_origin_are_still_required(hub, tc, monkeypatch):
    with monkeypatch.context() as auth:
        f.accounts(auth, hub, {"user@example.org": ("user", "user")})
        r = tc.post("/portal/api/connectors", json=body(),
                    headers={**SAME, "x-test-user": "user@example.org"})
        assert r.status_code == 403
    r = tc.post("/portal/api/connectors", json=body(), headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert registry.list_all(hub) == []


def test_withdraw_and_delete_persist_and_clean_up(hub, tc):
    assert create(tc).status_code == 201
    from hubzoid.access import store_for
    store_for(hub).grant("person@example.org", "sales", "connector_gmail", actor=f.OWNER)
    assert tc.delete("/portal/api/connectors/gmail/agents/sales", headers=SAME).status_code == 204
    assert registry.agents_of(hub, "gmail") == []
    assert not any(p == "connector_gmail" for _, _, p in store_for(hub).list_grants("sales"))
    assert tc.put("/portal/api/connectors/gmail/agents/sales", headers=SAME).status_code == 204
    tokens.store(hub, user_id="person", email="person@example.org", connector_id="gmail",
                 token={"v": 1, "kind": "oauth", "access_token": "AT", "url": body()["url"]})
    assert tc.patch("/portal/api/connectors/gmail", json={"url": "https://other.example/mcp"}, headers=SAME).status_code == 200
    assert tokens.token_record(hub, "person", "gmail") is None
    assert tc.delete("/portal/api/connectors/gmail", headers=SAME).status_code == 204
    assert not (hub / "connectors/gmail.yaml").exists()
    assert registry.get(hub, "gmail") is None
