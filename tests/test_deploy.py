"""`hubzoid deploy init|verify` (HUB-37): provider files for a hosted hub, and
a check of a deployment from outside. The public health check says nothing
about the hub."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from hubzoid import cli
from hubzoid.deploy import scaffold, verify

ROOT = Path(__file__).resolve().parents[1]


def _hub(tmp_path: Path) -> Path:
    hub = tmp_path / "desk"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: desk\ndescription: d\n---\nHelp.\n")
    return hub


@pytest.mark.parametrize("provider", ["railway", "maritime"])
def test_init_writes_the_provider_files_and_never_replaces_without_force(tmp_path, provider):
    hub = _hub(tmp_path)
    written = scaffold.write(hub, provider, image="ghcr.io/hubzoid/hubzoid:9.9.9")
    assert {w.path: w.action for w in written} == {
        "Dockerfile": "created", ".dockerignore": "created",
        f"deploy/{provider}/README.md": "created", f"deploy/{provider}/env.example": "created"}
    dockerfile = (hub / "Dockerfile").read_text()
    assert "FROM ghcr.io/hubzoid/hubzoid:9.9.9\nCOPY . /hub\nENV HUBZOID_DATA_DIR=/data\n" in dockerfile
    ignored = (hub / ".dockerignore").read_text().splitlines()
    assert {".env", ".hubzoid/", "restricted/.env", "output/", ".git/"} <= set(ignored)
    assert "HUBZOID_AUTH=true" in (hub / "deploy" / provider / "env.example").read_text()
    assert {w.action for w in scaffold.write(hub, provider, image="ghcr.io/hubzoid/hubzoid:9.9.9")} == {"unchanged"}
    (hub / "Dockerfile").write_text("FROM mine\n")
    with pytest.raises(scaffold.ScaffoldError, match="Dockerfile"):
        scaffold.write(hub, provider)
    assert (hub / "Dockerfile").read_text() == "FROM mine\n"                       # nothing written
    assert "replaced" in {w.action for w in scaffold.write(hub, provider, force=True)}


def test_init_refuses_a_folder_that_is_not_a_hub_and_an_unknown_provider(tmp_path):
    with pytest.raises(scaffold.ScaffoldError, match="not a hub"):
        scaffold.write(tmp_path, "railway")
    with pytest.raises(scaffold.ScaffoldError, match="Unknown provider"):
        scaffold.write(_hub(tmp_path), "heroku")
    r = CliRunner().invoke(cli.app, ["deploy", "init", "heroku", str(tmp_path / "desk"), "--json"])
    assert r.exit_code == 2 and json.loads(r.output)["detail"]["code"] == "invalid_request"


@pytest.mark.parametrize("provider", ["railway", "maritime"])
def test_the_docs_pages_are_the_templates(provider):
    page = (ROOT / "docs" / "deploy" / f"{provider}.md").read_text()
    rendered = scaffold.render(provider, image="ghcr.io/hubzoid/hubzoid:<version>")[f"deploy/{provider}/README.md"]
    assert page.split("-->\n\n", 1)[1] == rendered      # edit the template, then regenerate the page
    assert "Not yet verified" in rendered


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
def _client(answers: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        status, body = answers.get(request.url.path, (404, {"detail": "not found"}))
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


GOOD = {"/healthz": (200, {"status": "ok"}),
        "/api/auth/session": (200, {"mode": "accounts", "authenticated": False,
                                    "public_url": "https://hub.example.com"}),
        "/api/conversations": (401, {"detail": "unauthenticated"})}


def _verify(answers, url="https://hub.example.com", **kw):
    return {r.id: r for r in verify.run(url, client=_client(answers), attempts=1, **kw)}


def test_verify_passes_a_ready_deployment():
    results = _verify(GOOD)
    assert [r.id for r in results.values()] == ["https", "health", "sign_in", "data", "public_url"]
    assert all(r.status == "ok" for r in results.values())


@pytest.mark.parametrize("change,check", [
    ({"/healthz": (200, {"status": "ok", "hub": "desk", "model": "claude-local"})}, "health"),
    ({"/api/auth/session": (200, {"mode": "local", "public_url": "https://hub.example.com"})}, "sign_in"),
    ({"/api/conversations": (200, {"items": []})}, "data"),
    ({"/api/auth/session": (200, {"mode": "accounts", "public_url": "https://old.example.com"})}, "public_url"),
    ({"/api/auth/session": (200, {"mode": "accounts", "public_url": None})}, "public_url"),
])
def test_verify_names_what_is_not_ready(change, check):
    results = _verify({**GOOD, **change})
    assert results[check].status == "fail" and results[check].fix


def test_verify_plain_http_and_an_unreachable_address():
    assert _verify(GOOD, "http://hub.example.com")["https"].status == "fail"
    assert _verify(GOOD, "http://hub.example.com", allow_http=True)["https"].status == "warn"

    def down(request):
        raise httpx.ConnectError("refused")

    results = {r.id: r for r in verify.run("https://hub.example.com", attempts=1,
                                           client=httpx.Client(transport=httpx.MockTransport(down)))}
    assert results["health"].status == "fail" and "No answer" in results["health"].summary
    with pytest.raises(ValueError, match="address only"):
        verify.origin("https://user:pw@hub.example.com/app")


def test_the_public_health_check_says_nothing_about_the_hub(monkeypatch):
    from starlette.testclient import TestClient

    from hubzoid import edge

    real_client, up = httpx.AsyncClient, {"ok": True}

    def bridge(request):
        if not up["ok"]:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"status": "ok", "hub": "desk", "agent": "Desk", "model": "claude-local"})

    monkeypatch.setattr(edge.httpx, "AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(bridge)))
    with TestClient(edge.build_edge_app(default_base="http://bridge", routes=[])) as client:
        r = client.get("/healthz")
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        up["ok"] = False
        r = client.get("/healthz")
        assert r.status_code == 503 and r.json() == {"status": "unavailable"}


def test_the_session_names_the_public_address(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.chat_helpers import build_app, make_hub

    make_hub(tmp_path, monkeypatch, HUBZOID_PUBLIC_URL="https://hub.example.com")
    with TestClient(build_app()) as client:
        assert client.get("/api/auth/session").json()["public_url"] == "https://hub.example.com"
