"""Get started cards: a hub's get-started.yaml, the `card` a check may return,
the built-in model card, GET /api/hubs/<key>/get-started and the agent card
flag."""
from __future__ import annotations

import subprocess
import textwrap
import threading
import time
import types

import pytest

from hubzoid import factory_claude, factory_codex
from hubzoid.hubs import get_started, probes
from hubzoid.hubs.yamlfile import HubFileError
from hubzoid.logins import Login
from tests import connectors_fakes as f

APP = "http://127.0.0.1:3599"
URL = "/api/hubs/factory/get-started"
CLAUDE_TEAM = Login("claude-local", True, "subscription", "team", 2)
CODEX_PLUS = Login("codex-local", True, "subscription", "plus", 1)

MANIFEST = """\
version: 1
doctor:
  - {id: code, entrypoint: doctor/code.py:check}
  - {id: docker, entrypoint: doctor/docker.py:check, required: true}
  - {id: unused, entrypoint: doctor/unused.py:check}
"""
CARDS = """\
version: 1
cards:
  - {id: model, title: Model, check: model}
  - {id: code, title: Code, check: code}
  - {id: builds, title: Builds, check: docker, detail: Plans and builds run in Docker}
  - {id: results, title: Results, line: Reviewed branches, detail: After you approve the plan}
"""


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    f.clean_env(monkeypatch)
    for name in ("MODEL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def hub(tmp_path):
    h = f.make_hub(tmp_path, "factory")
    (h / "hub.manifest.yaml").write_text(MANIFEST)
    return h


def check_file(hub, name, body):
    (hub / "doctor").mkdir(exist_ok=True)
    (hub / "doctor" / f"{name}.py").write_text(textwrap.dedent(body))


def cards(hub, text):
    (hub / "get-started.yaml").write_text(textwrap.dedent(text))


def client(hub):
    from fastapi import FastAPI

    from hubzoid.hubs import routes

    app = FastAPI()
    routes.mount(app, hub)
    tc = f.client_for(app, APP)
    tc.headers["sec-fetch-site"] = "same-origin"  # as the web app's own fetch
    return tc


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------
def test_a_valid_file_loads_and_no_file_is_none(hub, tmp_path):
    cards(hub, CARDS)
    parsed = get_started.load(hub)
    assert parsed.title == "Get started" and [c.id for c in parsed.cards] == ["model", "code", "builds", "results"]
    assert parsed.cards[3].check is None and parsed.cards[3].line == "Reviewed branches"
    assert get_started.load(f.make_hub(tmp_path, "plain")) is None


@pytest.mark.parametrize("body,words", [
    ("  - {id: a, title: A, line: x, colour: red}", ["card 'a'", "colour"]),
    ("  - {id: a, title: A, check: model, line: x}", ["card 'a'", "a check or a line"]),
    ("  - {id: a, title: A}", ["card 'a'", "a check or a line"]),
    ("  - {id: a, title: A, check: factory.gihub}", ["card 'a'", "factory.gihub"]),
    ("  - {id: a, title: A, check: ''}", ["card 'a': check"]),
    ("".join(f"  - {{id: c{i}, title: C, line: x}}\n" for i in range(7)), ["at most 6"]),
    ("  - {id: a, title: A, line: x}\n  - {id: a, title: B, line: y}", ["share an id"]),
])
def test_an_invalid_file_says_why_in_one_line(hub, body, words):
    cards(hub, "version: 1\ncards:\n" + body + "\n")
    with pytest.raises(HubFileError) as exc:
        get_started.load(hub)
    message = str(exc.value)
    assert message.startswith("get-started.yaml: ") and "\n" not in message and str(hub) not in message
    assert all(w in message for w in words), message


def test_doctor_reports_only_an_invalid_file(tmp_path):
    from hubzoid import doctor

    plain = f.make_hub(tmp_path, "plain")
    cards(plain, "version: 1\ncards:\n  - {id: model, title: Model, check: model}\n")
    assert doctor._hub_setup_checks(plain) == []
    cards(plain, "version: 1\ncards:\n  - {id: model, title: Model, check: code}\n")
    [row] = doctor._hub_setup_checks(plain)
    assert row.id == "get_started" and row.status == "fail" and "code" in row.summary


# ---------------------------------------------------------------------------
# A check's card
# ---------------------------------------------------------------------------
@pytest.mark.slow  # starts a process per check
def test_a_check_may_return_a_card_kept_short(hub):
    check_file(hub, "code", """\
        def check(ctx):
            return {"status": "ok", "summary": "Found", "card": {"line": "x" * 70, "detail": "d", "colour": "red"}}
    """)
    check_file(hub, "docker", 'def check(ctx):\n    return {"status": "ok", "summary": "Up", "card": "text"}\n')
    got = {r.id: r for r in probes.run(hub, only=["code", "docker"])}
    assert got["code"].card == {"line": "x" * 59 + "…", "detail": "d"}
    assert got["docker"].card is None


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------
@pytest.mark.slow  # starts a process per check
def test_a_manager_gets_each_cards_status_line_and_fix(hub, monkeypatch):
    monkeypatch.setattr(factory_claude, "claude_login", lambda: CLAUDE_TEAM)
    monkeypatch.setattr(factory_codex, "codex_login", lambda: CODEX_PLUS)
    cards(hub, CARDS)
    check_file(hub, "code", """\
        def check(ctx):
            return {"status": "ok", "summary": "Repository found",
                    "card": {"line": "~/code/acme-api", "detail": "Branch main, clean"}}
    """)
    check_file(hub, "docker", """\
        def check(ctx):
            return {"status": "fail", "summary": "Docker is not running on this machine, so plans and builds wait",
                    "recovery": "Start Docker Desktop."}
    """)
    check_file(hub, "unused", "def check(ctx):\n    open('ran', 'w').close()\n    return {}\n")
    tc = client(hub)
    body = tc.get("/api/hubs/factory/get-started").json()
    assert body["title"] == "Get started" and body["ready"] is False and body["checked_at"].endswith("Z")
    got = {c["id"]: c for c in body["cards"]}
    assert got["model"] == {"id": "model", "title": "Model", "status": "ok", "line": "Claude Code, Team plan",
                            "detail": "Codex also signed in (Plus plan)", "fix": None}
    assert got["code"] == {"id": "code", "title": "Code", "status": "ok", "line": "~/code/acme-api",
                           "detail": "Branch main, clean", "fix": None}
    assert got["builds"]["status"] == "fail" and got["builds"]["fix"] == "Start Docker Desktop."
    assert got["builds"]["line"] == "Docker is not running on this machine, so plans and builds…"
    assert got["builds"]["detail"] == "Plans and builds run in Docker"  # the card's own
    assert got["results"] == {"id": "results", "title": "Results", "status": None, "line": "Reviewed branches",
                              "detail": "After you approve the plan", "fix": None}
    assert not (hub / "ran").exists()  # only the checks the cards name

    check_file(hub, "docker", 'def check(ctx):\n    return {"status": "ok", "summary": "Docker running"}\n')
    assert tc.get(URL).json() == body  # cached
    fresh = tc.get(URL + "?refresh=1").json()  # Check again always looks again, even right after a run
    assert fresh["ready"] is True and fresh["checked_at"] >= body["checked_at"]


def test_the_model_card_follows_the_sign_in_check(hub, monkeypatch):
    from hubzoid.hubs import routes

    monkeypatch.setattr(routes, "GET_STARTED_EVERY", 0)
    monkeypatch.setattr(factory_codex, "codex_login", lambda: CODEX_PLUS)
    cards(hub, "version: 1\ntitle: First steps\ncards:\n  - {id: model, title: Model, check: model}\n")
    tc = client(hub)
    body = tc.get("/api/hubs/factory/get-started").json()
    [card] = body["cards"]
    assert body["title"] == "First steps" and body["ready"] is False
    assert (card["status"], card["line"]) == ("fail", "Claude Code isn't signed in.")
    assert card["detail"] == "Codex is signed in (Plus plan)" and "claude auth login" in card["fix"]
    monkeypatch.setenv("MODEL", "openai/gpt-5")
    monkeypatch.setenv("OPENAI_API_KEY", "x")  # gitleaks:allow
    [card] = tc.get("/api/hubs/factory/get-started").json()["cards"]  # the cache is 0 seconds here
    assert (card["status"], card["line"], card["detail"]) == ("ok", "openai/gpt-5", None)


@pytest.mark.real_logins  # fakes the `claude` CLI itself, so its kept answer is the real one
def test_the_model_card_asks_each_cli_once_and_check_again_asks_anew(hub, monkeypatch):
    from hubzoid.hubs import routes

    factory_claude.clear_login_cache()
    monkeypatch.setattr(factory_claude.shutil, "which", lambda name: "/bin/claude" if name == "claude" else None)
    status, claude_runs, codex_runs = {"out": '{"loggedIn": false}'}, [], []

    def run(cmd, **kw):
        claude_runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, status["out"], "")

    monkeypatch.setattr(factory_claude, "subprocess", types.SimpleNamespace(**{**vars(subprocess), "run": run}))
    monkeypatch.setattr(factory_codex, "codex_login", lambda: codex_runs.append(1) or CODEX_PLUS)
    cards(hub, "version: 1\ncards:\n  - {id: model, title: Model, check: model}\n")
    tc = client(hub)
    try:
        assert tc.get(URL).json()["cards"][0]["status"] == "fail"
        status["out"] = '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max"}'  # signed in
        [card] = tc.get(URL + "?refresh=1").json()["cards"]
        assert (card["status"], card["line"]) == ("ok", "Claude Code, Max plan")
        assert (len(claude_runs), len(codex_runs)) == (2, 2)  # once per build
        monkeypatch.setenv("MODEL", "codex-local")
        assert tc.get(URL + "?refresh=1").json()["cards"][0]["line"] == "Codex, Plus plan"
        assert len(codex_runs) == 3
    finally:
        factory_claude.clear_login_cache()


def test_only_managers_and_only_agents_with_the_file(hub, monkeypatch):
    f.accounts(monkeypatch, hub, {"alice@example.com": ("u-a", "admin"), "bob@example.com": ("u-b", "user")})
    from hubzoid.access import store_for

    gs = store_for(hub)
    gs.bootstrap(["alice@example.com"])
    gs.grant("bob@example.com", "factory", "use_hub", actor="test")
    tc = client(hub)
    alice, bob = {"x-test-user": "alice@example.com"}, {"x-test-user": "bob@example.com"}
    assert tc.get(URL, headers=alice).status_code == 404
    cards(hub, "version: 1\ncards:\n  - {id: model, title: Model, check: nope}\n")
    # Refused exactly like the agent's other Settings routes.
    assert tc.get(URL).status_code == tc.get("/api/hubs/factory/settings/schema").status_code == 401
    for path in (URL, "/api/hubs/factory/settings/schema"):
        assert tc.get(path, headers=bob).json()["detail"]["code"] == "not_found"
        assert tc.get(path.replace("factory", "other"), headers=alice).json()["detail"]["code"] == "hub_not_found"
    r = tc.get(URL, headers=alice)
    assert r.status_code == 200 and r.json()["cards"] == [] and "nope" in r.json()["error"]
    assert str(hub) not in r.text
    cards(hub, "version: 1\ncards:\n  - {id: results, title: Results, line: Reviewed branches}\n")
    # An error is not kept: the fixed file's cards show at once.
    assert tc.get(URL, headers=alice).json()["cards"][0]["line"] == "Reviewed branches"


def test_another_site_cannot_make_a_managers_browser_run_the_checks(hub, monkeypatch):
    ran = []
    monkeypatch.setattr(get_started, "build", lambda hub_dir, **kw: ran.append(1) or {"cards": []})
    cards(hub, "version: 1\ncards:\n  - {id: model, title: Model, check: model}\n")
    tc = client(hub)
    r = tc.get(URL, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "cross_origin" and not ran
    del tc.headers["sec-fetch-site"]  # an older browser: its Referer decides, as for writes
    assert tc.get(URL).status_code == 403 and not ran
    assert tc.get(URL, headers={"referer": f"{APP}/chat"}).status_code == 200 and ran == [1]


def test_requests_while_the_checks_run_get_that_run(hub, monkeypatch):
    from hubzoid.hubs import routes

    cards(hub, "version: 1\ncards:\n  - {id: results, title: Results, line: Reviewed branches}\n")
    tc = client(hub)
    asked, runs, doctor = [], [], []
    present = get_started.present

    def counted(hub_dir):
        asked.append(1)
        return present(hub_dir)

    def build(hub_dir, **kw):
        runs.append(1)
        deadline = time.monotonic() + 5
        while len(asked) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)  # the second request joins this run
        doctor.append(tc.post("/api/hubs/factory/doctor", json={}, headers={"origin": APP}).status_code)
        return {"title": "Get started", "ready": True, "cards": [], "checked_at": f"run {len(runs)}"}

    monkeypatch.setattr(get_started, "present", counted)
    monkeypatch.setattr(get_started, "build", build)
    out = []
    threads = [threading.Thread(target=lambda: out.append(tc.get(URL + "?refresh=1").json())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(runs) == 1 and out == [out[0], out[0]] and out[0]["checked_at"] == "run 1"
    assert doctor == [429]  # doctor never runs the same checks at once


def test_the_agent_card_says_when_there_are_cards(hub):
    from hubzoid.webapp_gateway import agent_card

    assert "get_started" not in agent_card(hub)
    cards(hub, CARDS)
    assert agent_card(hub)["get_started"] is True
