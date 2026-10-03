"""The connector store on PostgreSQL, the shared store of a real gateway:
registry writes and audit, the dynamic-client compare-and-set, token upserts,
the refresh lease under concurrent turns, single-use authorizations and the
account lookup. Skipped when PostgreSQL is not installed locally.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import text

from hubzoid import connectors
from hubzoid.access import store_for
from hubzoid.access.identity import Identity
from hubzoid.connectors import http as net
from hubzoid.connectors import oauth_flow, per_user, registry, tokens
from tests import connectors_fakes as f

pytestmark = pytest.mark.skipif(not (shutil.which("initdb") and shutil.which("pg_ctl")),
                                reason="local PostgreSQL binaries are not installed")


@pytest.fixture(scope="module")
def pg_url(tmp_path_factory):
    pytest.importorskip("psycopg")
    root = tmp_path_factory.mktemp("connectors-postgres")
    port = f.free_port()
    subprocess.run([shutil.which("initdb"), "-D", str(root / "data"), "-U", "hz_test", "-A", "trust",
                    "--no-locale", "--encoding=UTF8"], check=True, capture_output=True, text=True)
    subprocess.run([shutil.which("pg_ctl"), "-D", str(root / "data"), "-l", str(root / "server.log"),
                    "-o", f"-h 127.0.0.1 -p {port} -k ''", "-w", "start"],
                   check=True, capture_output=True, text=True)
    try:
        yield f"postgresql+psycopg://hz_test@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run([shutil.which("pg_ctl"), "-D", str(root / "data"), "-m", "immediate", "-w",
                        "stop"], check=True, capture_output=True, text=True)


@pytest.fixture
def hub(tmp_path, monkeypatch, pg_url):
    f.clean_env(monkeypatch)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", pg_url)
    hub = f.make_hub(tmp_path, "sales")
    with connectors.engine(hub).begin() as conn:
        for table in ("hz_connector_flows", "hz_connector_tokens", "hz_connectors", "hz_users",
                      "hz_access_audit", "hz_grants", "hz_meta"):
            conn.execute(text(f"DELETE FROM {table}"))
    import hubzoid.access as access

    access._stores.clear()
    assert connectors.engine(hub).dialect.name == "postgresql"
    return hub


def test_registry_and_audit_on_postgres(hub):
    c = registry.create(hub, {"name": "Gmail", "url": "https://gmail-mcp.example.org/mcp",
                              "client_id": "app", "client_secret": "s3cret"}, actor="ada@example.org")
    assert c.id == "gmail" and c.has_client_secret
    assert registry.create(hub, {"name": "Gmail", "url": "https://x.example.org/mcp"},
                           actor="ada@example.org").id == "gmail_2"
    updated, reset = registry.update(hub, "gmail", {"url": "https://moved.example.org/mcp"},
                                     actor="ada@example.org")
    assert reset and updated.url == "https://moved.example.org/mcp"
    assert registry.client_secret(hub, "gmail") == "s3cret"
    assert registry.delete(hub, "gmail_2", actor="ada@example.org")
    rows = store_for(hub).read_access_audit(10, hubs=["*"])
    assert [r["action"] for r in rows] == ["connector_delete", "connector_update",
                                           "connector_create", "connector_create"]


def test_one_dynamic_client_wins_a_registration_race(hub):
    registry.create(hub, {"name": "Mail", "url": "https://mail.example.org/mcp"}, actor="t")
    redirect = "https://hub.example.org/oauth/connectors/mail/callback"
    results = []

    def register(i):
        client = {"client_id": f"client-{i}", "issuer": "https://as.example.org", "scope": None,
                  "auth_method": "none", "source": "dynamic", "client_secret_expires_at": 0}
        results.append(registry.save_registration(hub, "mail", redirect, client)["client_id"])

    threads = [threading.Thread(target=register, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1  # everyone uses the client that was stored first
    assert registry.registration(hub, "mail", redirect)["client_id"] == results[0]


def test_token_upsert_refresh_lease_and_accounts_on_postgres(hub, monkeypatch):
    calls = []
    lock = threading.Lock()

    def token_endpoint(request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        with lock:
            calls.append(form["refresh_token"])
        time.sleep(0.3)
        n = len(calls) + 1
        return httpx.Response(200, json={"access_token": f"at-{n}", "token_type": "Bearer",
                                         "expires_in": 3600, "refresh_token": f"rt-{n}"})

    monkeypatch.setattr(net, "_transport", httpx.MockTransport(token_endpoint))
    f.accounts(monkeypatch, hub, {"x@example.org": ("u-x", "user")})
    registry.create(hub, {"name": "Mail", "url": "https://mail.example.org/mcp"}, actor="t")
    f.grant_connector(hub, "mail", "x@example.org")
    record = {"v": 1, "kind": "oauth", "access_token": "at-1", "refresh_token": "rt-1",
              "expires_at": time.time() - 1, "token_endpoint": "https://as.example.org/token",
              "resource": "https://mail.example.org/mcp", "client": {"client_id": "c"},
              "url": "https://mail.example.org/mcp"}
    tokens.store(hub, user_id="u-x", email="x@example.org", connector_id="mail", token=record)
    tokens.store(hub, user_id="u-x", email="x@example.org", connector_id="mail", token=record)
    assert tokens.get(hub, "u-x", "mail").version == 1  # the second store updated the row

    results = []
    threads = [threading.Thread(target=lambda: results.append(
        tokens.access_token_for(hub, "u-x", "mail"))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["at-2"] * 6 and calls == ["rt-1"]

    assert tokens.user_id_for(hub, "X@example.org") == "u-x"
    (srv,) = per_user.per_user_servers(hub, Identity.make("x@example.org", surface="web"))
    assert srv.headers == {"Authorization": "Bearer at-2"}


def test_an_authorization_is_consumed_once_on_postgres(hub):
    registry.create(hub, {"name": "Mail", "url": "https://mail.example.org/mcp"}, actor="t")
    state = "state-value-for-a-single-use-test"
    with connectors.engine(hub).begin() as conn:
        conn.execute(text("INSERT INTO hz_connector_flows (state, user_id, connector_id, "
                          "payload_enc, return_to, created_at, expires_at) VALUES "
                          "(:s, 'u', 'mail', 'x', NULL, 0, :e)"),
                     {"s": oauth_flow._digest(state), "e": time.time() + 600})
    won = []
    threads = [threading.Thread(target=lambda: won.append(oauth_flow._consume(hub, state)))
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for w in won if w is not None) == 1
