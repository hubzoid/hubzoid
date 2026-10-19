"""A hub's declared settings (settings.yaml, hub.settings.yaml), the
{{settings.KEY}} placeholders, the hub's own doctor checks (hub.manifest.yaml),
and their CLI and API."""
from __future__ import annotations

import json
import os
import textwrap

import pytest
from typer.testing import CliRunner

from hubzoid import config_secrets as cs
from hubzoid import secretbox
from hubzoid.access import store_for
from hubzoid.hubs import probes
from hubzoid.hubs import settings as hs
from hubzoid.hubs.yamlfile import HubFileError
from hubzoid.secretstore import layer, store
from tests import connectors_fakes as f

APP = "http://127.0.0.1:3599"
SAME = {"origin": APP}

DECLS = """\
version: 1
settings:
  - name: FACTORY_REPO
    description: Repository the factory works on
    required: true
    regex: '^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$'
    example: acme/widgets
  - name: FACTORY_LABEL
    default: factory
  - name: MAX_RETRIES
    type: integer
    minimum: 0
    maximum: 5
    env: FACTORY_MAX_RETRIES
  - name: CHANNELS
    type: string_list
  - name: GITHUB_TOKEN
    secret: true
    required: true
"""


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    saved = dict(os.environ)
    f.clean_env(monkeypatch)
    for key in ("FACTORY_MAX_RETRIES", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    cs.clear_cache()
    layer.disable()
    secretbox.reset_cache()
    hs.forget()
    yield
    hs.forget()
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def hub(tmp_path):
    h = f.make_hub(tmp_path, "factory")
    (h / "settings.yaml").write_text(DECLS)
    return h


def values(hub, text):
    (hub / "hub.settings.yaml").write_text(textwrap.dedent(text))


# ---------------------------------------------------------------------------
# Declarations and values
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad,why", [
    ("version: 1\nsettings:\n  - name: lower\n", "UPPER_CASE"),
    ("version: 1\nsettings:\n  - name: A\n  - name: A\n", "declared twice"),
    ("version: 1\nversion: 1\nsettings: []\n", "duplicate key"),
    ("version: 1\nsettings:\n  - &x {name: A}\n  - *x\n", "aliases"),
    ("version: 1\nsettings:\n  - {name: A, secret: true, default: x}\n", "cannot have a default"),
    ("version: 1\nsettings:\n  - {name: A, regex: '(?=x)'}\n", "regex is not valid"),
    ("version: 1\nsettings:\n  - {name: A, colour: red}\n", "Extra inputs"),
    ("version: 2\nsettings: []\n", "version"),
])
def test_bad_declarations_name_the_problem(tmp_path, bad, why):
    hub = f.make_hub(tmp_path, "h")
    (hub / "settings.yaml").write_text(bad)
    with pytest.raises(HubFileError) as exc:
        hs.declarations(hub)
    assert why in str(exc.value) and "settings.yaml" in str(exc.value)


def test_every_problem_is_reported_at_once(hub):
    values(hub, """\
        version: 1
        values:
          MAX_RETRIES: 9
          CHANNELS: 5
          GITHUB_TOKEN: ghp_literal
          UNKNOWN: x
        secrets:
          GITHUB_TOKEN_ref: ghp_literal
    """)
    _, problems = hs.validate(hub)
    codes = sorted((p.key, p.code) for p in problems)
    assert ("FACTORY_REPO", "setting_required") in codes
    assert ("MAX_RETRIES", "setting_invalid") in codes
    assert ("CHANNELS", "setting_invalid") in codes
    assert ("GITHUB_TOKEN", "secret_literal_forbidden") in codes
    assert ("UNKNOWN", "setting_unknown") in codes
    assert ("GITHUB_TOKEN_ref", "secret_literal_forbidden") in codes
    assert "ghp_literal" not in json.dumps([p.public() for p in problems])


def test_values_come_from_env_then_file_then_default(hub, monkeypatch):
    values(hub, "version: 1\nvalues:\n  FACTORY_REPO: acme/widgets\n  MAX_RETRIES: 2\n"
                "  CHANNELS: [email, slack]\n")
    monkeypatch.setenv("FACTORY_MAX_RETRIES", "4")
    got = {r.decl.name: (r.value, r.source) for r in hs.resolve(hub)}
    assert got["FACTORY_REPO"] == ("acme/widgets", "hub.settings.yaml")
    assert got["MAX_RETRIES"] == (4, "environment")
    assert got["FACTORY_LABEL"] == ("factory", "default")
    assert got["CHANNELS"] == (["email", "slack"], "hub.settings.yaml")
    values(hub, "version: 1\nvalues:\n  FACTORY_REPO: 'not a repo'\n")
    problem = next(p for p in hs.validate(hub)[1] if p.key == "FACTORY_REPO")
    assert "pattern" in problem.message and "acme/widgets" in problem.message


def test_a_secret_setting_is_satisfied_by_env_or_a_stored_secret(hub, monkeypatch):
    values(hub, "version: 1\nvalues:\n  FACTORY_REPO: acme/widgets\n")
    assert not hs.validate(hub, env={})[1] == []  # GITHUB_TOKEN missing
    assert hs.validate(hub, env={"GITHUB_TOKEN": "x"})[1] == []
    store.put(hub, scope="hub", target="factory", name="gh-app-key", value="v", actor="t")
    values(hub, "version: 1\nvalues:\n  FACTORY_REPO: acme/widgets\nsecrets:\n"
                "  GITHUB_TOKEN_ref: secret:hub/factory/gh-app-key\n")
    resolved, problems = hs.validate(hub, env={})
    assert problems == [] and next(r for r in resolved if r.decl.secret).available
    values(hub, "version: 1\nvalues:\n  FACTORY_REPO: acme/widgets\nsecrets:\n"
                "  GITHUB_TOKEN_ref: secret:hub/otherhub/gh-app-key\n")
    assert [p.code for p in hs.validate(hub, env={})[1]] == ["permission_denied"]


def test_secret_names_join_the_secrets_list(hub):
    from hubzoid.secretstore import report

    row = next(r for r in report.rows(hub) if r["name"] == "GITHUB_TOKEN")
    assert row["status"] == "missing" and row["expected"] == "setting"


# ---------------------------------------------------------------------------
# Placeholders, for every runtime through the loaders
# ---------------------------------------------------------------------------
def test_placeholders_fill_instructions_and_skills_once(hub):
    from hubzoid.loaders import agents, skills

    values(hub, "version: 1\nvalues:\n  FACTORY_REPO: acme/widgets\n  MAX_RETRIES: 3\n"
                "  CHANNELS: ['{{settings.FACTORY_LABEL}}']\n")
    (hub / "AGENTS.md").write_text("---\nname: factory\n---\nWork on {{settings.FACTORY_REPO}}, retry "
                                   "{{ settings.MAX_RETRIES }} times, channels {{settings.CHANNELS}}.\n")
    (hub / "skills").mkdir()
    (hub / "skills" / "triage.md").write_text("---\nname: triage\ndescription: d\n---\nLabel {{settings.FACTORY_LABEL}}.\n")
    text = agents.load_main(hub).instructions
    assert text.startswith("Work on acme/widgets, retry 3 times")
    assert '["{{settings.FACTORY_LABEL}}"]' in text  # no second pass
    assert skills.load_hub(hub)[0].body.strip() == "Label factory."


@pytest.mark.parametrize("token,why", [("{{settings.NOPE}}", "names no setting"),
                                       ("{{settings.GITHUB_TOKEN}}", "is a secret"),
                                       ("{{settings.FACTORY_REPO}}", "no valid value")])
def test_bad_placeholders_stop_the_hub_with_file_and_line(hub, token, why):
    from hubzoid.loaders import agents

    (hub / "AGENTS.md").write_text(f"---\nname: factory\n---\nline one\n{token}\n")
    with pytest.raises(HubFileError) as exc:
        agents.load_main(hub)
    assert why in str(exc.value) and "AGENTS.md: line 5" in str(exc.value)


def test_a_hub_without_settings_files_loads_as_before(tmp_path):
    from hubzoid.loaders import agents

    hub = f.make_hub(tmp_path, "plain")
    assert agents.load_main(hub).instructions.strip() == "You are plain."
    assert hs.effective(hub) == {} and hs.revision(hub) == 0


# ---------------------------------------------------------------------------
# Writing with a revision
# ---------------------------------------------------------------------------
def test_writes_guard_their_revision_and_notice_hand_edits(hub):
    doc = {"version": 1, "values": {"FACTORY_REPO": "acme/widgets"}, "secrets": {}}
    assert hs.write(hub, doc, expected_revision="absent") == 1
    with pytest.raises(hs.Conflict):
        hs.write(hub, doc, expected_revision="absent")
    (hub / "hub.settings.yaml").write_text("version: 1\nvalues:\n  FACTORY_REPO: acme/other\n")  # by hand
    assert hs.revision(hub) == 2
    with pytest.raises(hs.Conflict) as exc:
        hs.write(hub, doc, expected_revision=1)
    assert exc.value.current == 2
    assert hs.write(hub, doc, expected_revision=2) == 3


# ---------------------------------------------------------------------------
# The hub's own checks
# ---------------------------------------------------------------------------
def probe_file(hub, name, body):
    (hub / "doctor").mkdir(exist_ok=True)
    (hub / "doctor" / f"{name}.py").write_text(textwrap.dedent(body))


@pytest.mark.slow  # starts a process per check
def test_declared_checks_run_isolated_and_never_stop_each_other(hub):
    values(hub, "version: 1\nvalues:\n  FACTORY_REPO: acme/widgets\n")
    probe_file(hub, "repo", """\
        def check(ctx):
            return {"status": "ok", "summary": "repo " + ctx["settings"]["FACTORY_REPO"],
                    "details": {"token_available": ctx["available"]["GITHUB_TOKEN"]}}
    """)
    probe_file(hub, "boom", "def check(ctx):\n    raise RuntimeError('broken probe')\n")
    probe_file(hub, "slow", "import time\ndef check(ctx):\n    time.sleep(5)\n")
    (hub / "hub.manifest.yaml").write_text(textwrap.dedent("""\
        version: 1
        doctor:
          - {id: repo, entrypoint: doctor/repo.py:check, requires: [FACTORY_REPO], required: true}
          - {id: boom, entrypoint: doctor/boom.py:check, required: true}
          - {id: slow, entrypoint: doctor/slow.py:check, timeout_seconds: 1}
          - {id: token, entrypoint: doctor/repo.py:check, requires: [GITHUB_TOKEN]}
    """))
    got = {r.id: r for r in probes.run(hub)}
    assert got["repo"].status == "ok" and got["repo"].summary == "repo acme/widgets"
    assert got["repo"].details == {"token_available": False}
    assert got["boom"].status == "fail" and got["boom"].summary == "The check crashed with RuntimeError."
    assert got["slow"].status == "warn" and "1 seconds" in got["slow"].summary  # not required: warn
    assert got["token"].status == "info" and "GITHUB_TOKEN not set" in got["token"].summary


@pytest.mark.parametrize("entry", ["../outside.py:check", "/etc/x.py:check", "doctor/repo.py", "doctor/x.sh:check"])
def test_a_check_must_live_inside_the_hub(hub, entry):
    (hub / "hub.manifest.yaml").write_text(f"version: 1\ndoctor:\n  - {{id: x, entrypoint: '{entry}'}}\n")
    with pytest.raises(HubFileError):
        probes.manifest(hub)


def test_doctor_shows_settings_and_checks_only_for_hubs_that_declare_them(hub, tmp_path):
    from hubzoid import doctor

    assert doctor._hub_setup_checks(f.make_hub(tmp_path, "plain")) == []
    checks = {c.id: c for c in doctor._hub_setup_checks(hub)}
    assert checks["settings.files"].status == "ok"
    assert checks["settings.values"].status == "fail" and "FACTORY_REPO is required" in checks["settings.values"].summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_configure_checks_then_applies_with_a_revision(hub, tmp_path):
    from hubzoid.cli import app

    runner = CliRunner()
    candidate = tmp_path / "candidate.yaml"
    candidate.write_text("version: 1\nvalues:\n  FACTORY_REPO: nope\n")
    r = runner.invoke(app, ["hub", "configure", str(hub), "--file", str(candidate), "--json"])
    assert r.exit_code == 2 and json.loads(r.output)["detail"]["code"] == "validation_failed"
    candidate.write_text("version: 1\nvalues:\n  FACTORY_REPO: acme/widgets\nsecrets:\n  GITHUB_TOKEN_ref: env:GH\n")
    r = runner.invoke(app, ["hub", "configure", str(hub), "--file", str(candidate), "--json"], env={"GH": "x"})
    assert r.exit_code == 0 and json.loads(r.output)["applied"] is False
    assert not (hub / "hub.settings.yaml").exists()
    r = runner.invoke(app, ["hub", "configure", str(hub), "--file", str(candidate), "--apply",
                            "--expected-revision", "absent", "--json"], env={"GH": "x"})
    assert r.exit_code == 0 and json.loads(r.output)["revision"] == 1
    r = runner.invoke(app, ["hub", "configure", str(hub), "--file", str(candidate), "--apply",
                            "--expected-revision", "absent"], env={"GH": "x"})
    assert r.exit_code == 4
    listing = json.loads(runner.invoke(app, ["hub", "settings", str(hub), "--json"], env={"GH": "x"}).output)
    assert listing["revision"] == 1 and listing["problems"] == []


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def client(hub):
    from fastapi import FastAPI

    from hubzoid.hubs import routes

    app = FastAPI()
    routes.mount(app, hub)
    return f.client_for(app, APP)


def test_the_api_reads_validates_and_writes_settings(hub, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    tc = client(hub)
    body = tc.get("/api/hubs/factory/settings").json()
    assert body["revision"] == 0 and {s["name"] for s in body["settings"]} >= {"FACTORY_REPO", "GITHUB_TOKEN"}
    assert "value" not in next(s for s in body["settings"] if s["secret"])
    r = tc.post("/api/hubs/factory/settings/validate", json={"values": {"FACTORY_REPO": "bad repo"}})
    assert r.json()["valid"] is False
    r = tc.put("/api/hubs/factory/settings", json={"values": {"FACTORY_REPO": "bad repo"},
                                                   "expected_revision": "absent"}, headers=SAME)
    assert r.status_code == 422 and r.json()["detail"]["fields"][0]["key"] == "FACTORY_REPO"
    r = tc.put("/api/hubs/factory/settings", json={"values": {"FACTORY_REPO": "acme/widgets"},
                                                   "expected_revision": "absent"}, headers=SAME)
    assert r.status_code == 200 and r.json()["revision"] == 1 and r.json()["restart_required"]
    r = tc.put("/api/hubs/factory/settings", json={"values": {"FACTORY_REPO": "acme/x"},
                                                   "expected_revision": "absent"}, headers=SAME)
    assert r.status_code == 409 and r.json()["detail"]["current_revision"] == 1
    assert tc.get("/api/hubs/other/settings").json()["detail"]["code"] == "hub_not_found"


def test_only_organization_administrators_change_secret_references(hub, monkeypatch):
    f.accounts(monkeypatch, hub, {"alice@example.com": ("u-a", "admin"), "bob@example.com": ("u-b", "user")})
    gs = store_for(hub)
    gs.bootstrap(["alice@example.com"])
    gs.grant("bob@example.com", "factory", "manage_access", actor="test")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    tc = client(hub)
    bob, alice = {"x-test-user": "bob@example.com"}, {"x-test-user": "alice@example.com"}
    plain = {"values": {"FACTORY_REPO": "acme/widgets"}, "expected_revision": "absent"}
    assert tc.put("/api/hubs/factory/settings", json=plain, headers={**SAME, **bob}).status_code == 200
    with_ref = {"values": {"FACTORY_REPO": "acme/widgets"}, "secrets": {"GITHUB_TOKEN_ref": "env:GITHUB_TOKEN"},
                "expected_revision": 1}
    r = tc.put("/api/hubs/factory/settings", json=with_ref, headers={**SAME, **bob})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "permission_denied"
    assert tc.put("/api/hubs/factory/settings", json=with_ref, headers={**SAME, **alice}).status_code == 200


def test_the_doctor_route_runs_hub_checks_and_is_rate_limited(hub):
    tc = client(hub)
    r = tc.post("/api/hubs/factory/doctor", json={}, headers=SAME)
    assert r.status_code == 200 and {c["id"] for c in r.json()["checks"]} >= {"settings.files", "settings.values"}
    r = tc.post("/api/hubs/factory/doctor", json={}, headers=SAME)
    assert r.status_code == 429 and r.json()["detail"]["retry_after"] >= 1


# ---------------------------------------------------------------------------
# Review regressions
# ---------------------------------------------------------------------------
def test_an_invalid_reference_is_never_echoed(hub):
    values(hub, "version: 1\nvalues:\n  FACTORY_REPO: acme/widgets\nsecrets:\n"
                "  GITHUB_TOKEN_ref: 'ghp:abcdef1234567890'\n")
    resolved, problems = hs.validate(hub, env={})
    blob = json.dumps([r.public() for r in resolved]) + json.dumps([p.public() for p in problems])
    assert "abcdef1234567890" not in blob and "ghp" not in blob


@pytest.mark.slow  # starts processes
def test_a_crash_message_is_not_shown_and_a_hang_is_killed_with_its_children(hub, tmp_path):
    values(hub, "version: 1\nvalues:\n  FACTORY_REPO: acme/widgets\n")
    marker = tmp_path / "child-alive"
    probe_file(hub, "leak", "def check(ctx):\n    raise ValueError('token=sk-live-secret-123')\n")  # gitleaks:allow (fake)
    probe_file(hub, "hang", f"""\
        import subprocess, sys
        def check(ctx):
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3); open({str(marker)!r}, 'w').write('x')"])
            import time; time.sleep(30)
    """)
    probe_file(hub, "noisy", "def check(ctx):\n    print('x' * 200000)\n    return {'status': 'ok', 'summary': 'ok'}\n")
    # A token alone on a message's last line looks like a class name in a traceback.
    token = "'ghp' + '_' + 'Zq7' * 12"  # built at run time, so no token is in this file
    probe_file(hub, "multiline", f"def check(ctx):\n    raise RuntimeError('authentication failed\\n' + {token})\n")
    probe_file(hub, "bare", f"import os, sys\ndef check(ctx):\n    sys.stderr.write({token} + '\\n')\n"
                            "    sys.stderr.flush()\n    os._exit(3)\n")
    (hub / "hub.manifest.yaml").write_text(textwrap.dedent("""\
        version: 1
        doctor:
          - {id: leak, entrypoint: doctor/leak.py:check, required: true}
          - {id: hang, entrypoint: doctor/hang.py:check, timeout_seconds: 1, required: true}
          - {id: noisy, entrypoint: doctor/noisy.py:check, required: true}
          - {id: multiline, entrypoint: doctor/multiline.py:check, required: true}
          - {id: bare, entrypoint: doctor/bare.py:check, required: true}
    """))
    got = {r.id: r for r in probes.run(hub)}
    assert got["leak"].summary == "The check crashed with ValueError." and "sk-live" not in json.dumps(got["leak"].__dict__)
    assert got["multiline"].summary == "The check crashed with RuntimeError."
    assert got["bare"].summary == "The check crashed with an error."  # no exception: a fixed message
    assert "Zq7Zq7" not in json.dumps([got["multiline"].__dict__, got["bare"].__dict__])
    assert got["hang"].status == "fail" and "1 seconds" in got["hang"].summary
    assert got["noisy"].status == "fail" and "64 KiB" in got["noisy"].summary
    import time

    time.sleep(3.5)
    assert not marker.exists()  # the hung check's child was killed with it


def test_an_unknown_check_id_is_refused(hub):
    from hubzoid.cli import app

    (hub / "hub.manifest.yaml").write_text("version: 1\ndoctor: []\n")
    r = CliRunner().invoke(app, ["hub", "doctor", str(hub), "--check", "typo", "--json"])
    assert r.exit_code == 2 and "No check named typo" in r.output


def test_writes_are_bounded_and_running_processes_keep_their_snapshot(hub):
    big = {"version": 1, "values": {"FACTORY_REPO": "acme/widgets"}, "secrets": {}}
    big["values"].update({f"X{i}": "y" * 16000 for i in range(70)})
    with pytest.raises(HubFileError):
        hs.write(hub, big, expected_revision="absent")
    values(hub, "version: 1\nvalues:\n  FACTORY_REPO: acme/widgets\n")
    assert hs.effective(hub)["FACTORY_REPO"] == "acme/widgets"
    values(hub, "version: 1\nvalues:\n  FACTORY_REPO: acme/changed\n")
    assert hs.effective(hub)["FACTORY_REPO"] == "acme/widgets"  # until the process restarts
    hs.forget()
    assert hs.effective(hub)["FACTORY_REPO"] == "acme/changed"


def test_concurrent_writers_cannot_both_win(hub):
    import threading

    hs.write(hub, {"version": 1, "values": {"FACTORY_REPO": "acme/a"}, "secrets": {}}, expected_revision="absent")
    outcomes = []

    def writer(repo):
        try:
            hs.write(hub, {"version": 1, "values": {"FACTORY_REPO": repo}, "secrets": {}}, expected_revision=1)
            outcomes.append("ok")
        except hs.Conflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=writer, args=(f"acme/{n}",)) for n in "bc"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["conflict", "ok"]


def test_values_can_be_saved_before_every_required_setting_is_set(hub, tmp_path):
    from hubzoid.cli import app

    candidate = tmp_path / "c.yaml"
    candidate.write_text("version: 1\nvalues:\n  FACTORY_LABEL: triage\n")
    r = CliRunner().invoke(app, ["hub", "configure", str(hub), "--file", str(candidate), "--apply",
                                 "--expected-revision", "absent", "--json"], env={})
    body = json.loads(r.output)
    assert r.exit_code == 0 and body["applied"] and {p["key"] for p in body["still_needed"]} == {"FACTORY_REPO", "GITHUB_TOKEN"}
    rows = {s["name"]: s for s in client(hub).get("/api/hubs/factory/settings").json()["settings"]}
    assert rows["FACTORY_REPO"]["status"] == "missing" and rows["FACTORY_LABEL"]["status"] == "set"
