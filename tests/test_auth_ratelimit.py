"""Sign-in rate limits: per address and per email, 10 failures in 15 minutes
lock for 15 minutes, success clears, and the counters are shared between
processes through the database."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from hubzoid.auth import ratelimit, routes, sessions, users
from hubzoid.auth.schema import engine_for

ENV = ("HUBZOID_UI", "WEBUI_AUTH", "HUBZOID_PUBLIC_URL", "WEBUI_URL", "HUBZOID_ALLOWED_ORIGINS",
       "HUBZOID_ADMIN_EMAIL", "WEBUI_ADMIN_EMAIL", "HUBZOID_DEPLOYMENT", "DATABASE_URL",
       "HUBZOID_AUTH_MAX_FAILURES", "ENABLE_SIGNUP")
ORIGIN = {"origin": "http://testserver"}
PASSWORD = "correct horse battery"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    d = tmp_path / "sales"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: Sales\n---\nHelp.\n")
    sessions.reset_cache()
    return d


def client(hub, ip="203.0.113.7") -> TestClient:
    app = FastAPI()
    routes.mount(app, hub)
    return TestClient(app, client=(ip, 40000))


def attempt(c, email="ana@example.com", password="wrong password!"):
    return c.post("/api/auth/login", json={"email": email, "password": password}, headers=ORIGIN)


def test_ten_failures_lock_the_email(hub):
    users.create(hub, email="ana@example.com", password=PASSWORD)
    c = client(hub)
    codes = [attempt(c).status_code for _ in range(9)]
    assert codes == [401] * 9
    tenth = attempt(c)
    assert tenth.status_code == 429
    body = tenth.json()["detail"]
    assert body["code"] == "rate_limited" and 800 < body["retry_after"] <= 900
    assert tenth.headers["retry-after"] == str(body["retry_after"])
    # Locked: even the right password is refused, from another address too.
    other = client(hub, ip="198.51.100.9")
    assert attempt(other, password=PASSWORD).status_code == 429


def test_ten_failures_lock_the_address_across_emails(hub):
    c = client(hub)
    for i in range(10):
        attempt(c, email=f"nobody{i}@example.com")
    users.create(hub, email="bo@example.com", password=PASSWORD)
    assert attempt(c, email="bo@example.com", password=PASSWORD).status_code == 429
    # The same account from another address is fine.
    assert attempt(client(hub, ip="198.51.100.9"), email="bo@example.com",
                   password=PASSWORD).status_code == 200


def test_success_clears_the_counters(hub):
    users.create(hub, email="ana@example.com", password=PASSWORD)
    c = client(hub)
    for _ in range(9):
        attempt(c)
    assert attempt(c, password=PASSWORD).status_code == 200
    for _ in range(9):
        assert attempt(c).status_code == 401  # a fresh window of ten


def test_the_lock_ends_after_fifteen_minutes(hub):
    users.create(hub, email="ana@example.com", password=PASSWORD)
    c = client(hub)
    for _ in range(10):
        attempt(c)
    assert attempt(c, password=PASSWORD).status_code == 429
    later = time.time() - 16 * 60
    with engine_for(hub).begin() as conn:
        conn.execute(text("UPDATE hz_auth_attempts SET locked_until=:t, window_start=:t"),
                     {"t": later})
    assert attempt(c, password=PASSWORD).status_code == 200


def test_expired_window_resets_the_count(hub):
    """Old failures stop counting: an expired window starts again at one."""
    now = time.time()
    for _ in range(9):
        ratelimit.record_failure(hub, email="ana@example.com", now=now - 20 * 60)
    assert ratelimit.record_failure(hub, email="ana@example.com", now=now) == 0
    with engine_for(hub).connect() as conn:
        assert conn.execute(text("SELECT failures FROM hz_auth_attempts")).scalar() == 1


def test_loopback_addresses_are_not_counted_per_address(hub):
    """Behind the edge without a forwarded address every browser shares
    127.0.0.1; ten mistakes anywhere must not lock everyone out."""
    assert ratelimit.keys(ip="127.0.0.1", email="a@example.com") == ["email:a@example.com"]
    assert ratelimit.keys(ip="::1") == []
    assert ratelimit.keys(ip="testclient") == []
    assert ratelimit.keys(ip="203.0.113.7") == ["ip:203.0.113.7"]
    for i in range(12):
        ratelimit.record_failure(hub, ip="127.0.0.1", email=f"x{i}@example.com")
    assert ratelimit.retry_after(hub, ip="127.0.0.1", email="fresh@example.com") == 0


def test_limit_setting(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_AUTH_MAX_FAILURES", "3")
    for _ in range(2):
        assert ratelimit.record_failure(hub, email="a@example.com") == 0
    assert ratelimit.record_failure(hub, email="a@example.com") > 0


def test_concurrent_failures_are_all_counted(hub):
    ratelimit.retry_after(hub, email="warm@example.com")  # migrate before the threads start
    threads = [threading.Thread(target=ratelimit.record_failure, args=(hub,),
                                kwargs={"email": "race@example.com"}) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with engine_for(hub).connect() as conn:
        assert conn.execute(text("SELECT failures FROM hz_auth_attempts WHERE key=:k"),
                            {"k": "email:race@example.com"}).scalar() == 8


_OTHER_PROCESS = """
import sys
from pathlib import Path
from hubzoid.auth import ratelimit
for _ in range(10):
    ratelimit.record_failure(Path(sys.argv[1]), ip="203.0.113.50", email="shared@example.com")
print("done")
"""


def test_counters_are_shared_between_processes(hub):
    """Bridges of a gateway share the store: failures seen by one process lock
    the account for all of them."""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    out = subprocess.run([sys.executable, "-c", _OTHER_PROCESS, str(hub)], env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.stdout.strip() == "done", out.stderr[-2000:]
    assert ratelimit.retry_after(hub, email="shared@example.com") > 0
    assert ratelimit.retry_after(hub, ip="203.0.113.50") > 0
    users.create(hub, email="shared@example.com", password=PASSWORD)
    assert attempt(client(hub), email="shared@example.com", password=PASSWORD).status_code == 429


def test_postgres_upsert(postgres_url, tmp_path, monkeypatch):
    import uuid

    from sqlalchemy import create_engine

    name = "hz_rl_" + uuid.uuid4().hex[:10]
    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    admin.dispose()
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", postgres_url.rsplit("/", 1)[0] + "/" + name)
    hub = tmp_path / "pg"
    hub.mkdir()
    for _ in range(9):
        assert ratelimit.record_failure(hub, ip="203.0.113.1", email="pg@example.com") == 0
    assert ratelimit.record_failure(hub, ip="203.0.113.1", email="pg@example.com") > 0
    assert ratelimit.retry_after(hub, email="pg@example.com") > 0
    ratelimit.record_success(hub, ip="203.0.113.1", email="pg@example.com")
    assert ratelimit.retry_after(hub, ip="203.0.113.1", email="pg@example.com") == 0
