"""`hubzoid sandbox`: config, profiles list and validate, doctor, list and
show, in JSON and for people, with refusals. Nothing here starts Docker; the
doctor's provider check is faked where it would reach the engine."""
from __future__ import annotations

import json
import textwrap

import pytest
from typer.testing import CliRunner

from hubzoid.cli import app

DEPLOYMENT = "version: 1\nsandbox:\n  enabled: true\n  default_provider: local\n  providers:\n    local:\n      kind: docker\n"
PROFILE = textwrap.dedent("""\
    version: 1
    description: Work box
    image:
      base: hubzoid-default
    resources: {cpus: 1, memory_mib: 512, pids: 128}
    network:
      setup: {mode: deny_all}
    lifecycle: {idle_stop_seconds: 60, max_lifetime_seconds: 600}
""")


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    for var in ("WEBUI_AUTH", "DATABASE_URL", "HUBZOID_OPERATIONAL_DB"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "boxhub"
    (root / "sandboxes").mkdir(parents=True)
    (root / "AGENTS.md").write_text("---\nname: Box\nmodel: claude-local\n---\nA hub.\n")
    return root


def sb(*args, as_json=True):
    r = CliRunner().invoke(app, ["sandbox", *args, *(["--json"] if as_json else [])], catch_exceptions=False)
    return r.exit_code, (json.loads(r.output) if as_json else r.output)


def test_without_sandbox_yaml_sandboxes_are_off(hub_dir):
    code, out = sb("config", str(hub_dir))
    assert code == 0 and out["present"] is False and out["digest"] is None
    code, out = sb("profiles", "list", str(hub_dir))
    assert code == 0 and out == {"profiles": []}
    code, text = sb("profiles", "list", str(hub_dir), as_json=False)
    assert "No profiles in sandboxes/." in text
    code, out = sb("profiles", "validate", str(hub_dir))
    assert code == 0 and out["checks"][0]["message"] == "No sandbox.yaml: sandboxes are off."


def test_profiles_are_listed_and_validated(hub_dir):
    (hub_dir / "sandbox.yaml").write_text(DEPLOYMENT)
    (hub_dir / "sandboxes" / "work.yaml").write_text(PROFILE)
    (hub_dir / "sandboxes" / "broken.yaml").write_text("version: 1\nimage: {base: hubzoid-default}\nsurprise: 1\n")

    code, out = sb("config", str(hub_dir))
    assert code == 0 and out["present"] is True and out["effective"]["enabled"] is True

    code, out = sb("profiles", "list", str(hub_dir))
    by_id = {p["id"]: p for p in out["profiles"]}
    assert by_id["work"]["valid"] is True and by_id["work"]["description"] == "Work box"
    assert by_id["broken"]["valid"] is False and by_id["broken"]["error"]
    code, text = sb("profiles", "list", str(hub_dir), as_json=False)
    assert "work  valid sha256:" in text

    code, out = sb("profiles", "validate", str(hub_dir))
    assert code == 2 and out["valid"] is False
    statuses = {c["message"].split(":")[0]: c["status"] for c in out["checks"]}
    assert statuses["sandbox.yaml is valid"] == "pass" and statuses["work"] == "pass"
    assert any(c["status"] == "fail" for c in out["checks"])
    code, text = sb("profiles", "validate", str(hub_dir), as_json=False)
    assert code == 2 and "work: sha256:" in text and "setup offline" in text

    (hub_dir / "sandboxes" / "broken.yaml").unlink()
    code, out = sb("profiles", "validate", str(hub_dir))
    assert code == 0 and out["valid"] is True


def test_a_bad_sandbox_yaml_is_named(hub_dir):
    (hub_dir / "sandbox.yaml").write_text("version: 7\n")
    code, out = sb("config", str(hub_dir))
    assert code == 2 and out["detail"]["code"]
    code, out = sb("profiles", "validate", str(hub_dir))
    assert code == 2 and out["checks"][0]["status"] == "fail"


class _Docker:
    """Stands in for the Docker provider: reachable or not, nothing prepared."""

    def __init__(self, up: bool):
        self.up = up

    def check(self):
        from hubzoid.work_items.errors import WorkItemError

        if not self.up:
            raise WorkItemError("provider_unavailable", "Docker is not reachable.", status=503)
        return {"server_version": "27.0", "runtime": "runc", "runtimes": ["runc"], "isolation": "default",
                "disk_limit": "none"}

    def has_image(self, name):
        return False


def test_doctor_when_docker_is_down_and_when_an_image_is_missing(hub_dir, monkeypatch):
    from hubzoid.sandbox import service

    (hub_dir / "sandbox.yaml").write_text(DEPLOYMENT)
    (hub_dir / "sandboxes" / "work.yaml").write_text(PROFILE)
    monkeypatch.setattr(service, "provider", lambda binding: _Docker(up=False))
    code, report = sb("doctor", str(hub_dir))
    assert code == 5 and report["valid"] is False
    down = next(c for c in report["checks"] if c["id"] == "provider:local")
    assert down["status"] == "fail" and down["summary"] == "Docker is not reachable."
    assert "Start Docker" in down["recovery_action"]

    monkeypatch.setattr(service, "provider", lambda binding: _Docker(up=True))
    code, report = sb("doctor", str(hub_dir))
    assert code == 0 and report["valid"] is True
    assert report["providers"]["local"]["server_version"] == "27.0"
    work = next(c for c in report["checks"] if c["id"] == "profile:work")
    assert work["status"] == "warn" and "not prepared" in work["summary"]
    assert report["profiles"]["work"]["prepared"] is False
    code, text = sb("doctor", str(hub_dir), as_json=False)
    assert "pass Docker 27.0 reachable" in text and "warn" in text and "profiles prepare . work" in text


def test_list_and_show_with_no_sandboxes(hub_dir):
    (hub_dir / "sandbox.yaml").write_text(DEPLOYMENT)
    code, out = sb("list", str(hub_dir))
    assert code == 0 and out.get("sandboxes", out.get("items", [])) == []
    code, out = sb("show", str(hub_dir), "sbx-missing")
    assert code == 2 and out["detail"]["code"] == "not_found"
