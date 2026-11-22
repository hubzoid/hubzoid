"""`hubzoid stations` and `hubzoid handoffs`: validate and doctor the isolated
agents, and read a work item's station calls and handoffs, with refusals.
No model runs: the definitions are files, the work item is admitted directly
and the login probe is faked."""
from __future__ import annotations

import json
import textwrap

import pytest
from typer.testing import CliRunner

from hubzoid.cli import app
from hubzoid.work_items import service

OWNER = "admin@localhost"
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary"],
          "properties": {"summary": {"type": "string"}}}
WORKFLOW = textwrap.dedent('''
    from hubzoid import hub, workflow
    from hubzoid.work_items import Route

    @workflow(work_items={"plan": Route(stages=("analysis", "review"), actions=())})
    def desk():
        return hub.call_agent("x", agent="analyst", stage="analysis")
''')


def station_file(name, *, tools="[]", extra=""):
    return (f"---\nname: {name}\ndescription: {name} station\nisolated: true\nengine: claude-local\n"
            f"tools: {tools}\nresponse_schema: schemas/result.json\n{extra}---\nDo the analysis.\n")


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    for var in ("WEBUI_AUTH", "DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "desk"
    (root / "workflows" / "desk").mkdir(parents=True)
    (root / "AGENTS.md").write_text("---\nname: Desk\nmodel: claude-local\n---\nA desk.\n")
    (root / "workflows" / "desk" / "main.py").write_text(WORKFLOW)
    folder = root / "agents" / "analyst"
    (folder / "schemas").mkdir(parents=True)
    (folder / "AGENTS.md").write_text(station_file("analyst", tools="[save_handoff]"))
    (folder / "schemas" / "result.json").write_text(json.dumps(SCHEMA))
    return root


def st(*args, as_json=True, group="stations"):
    r = CliRunner().invoke(app, [group, *args, *(["--json"] if as_json else [])], catch_exceptions=False)
    return r.exit_code, (json.loads(r.output) if as_json else r.output)


def test_validate_passes_then_names_a_bad_tool(hub_dir):
    code, out = st("validate", str(hub_dir))
    assert code == 0 and out["valid"] is True and "analyst" in out["stations"]
    code, text = st("validate", str(hub_dir), as_json=False)
    assert code == 0 and "ok" in text

    (hub_dir / "agents" / "analyst" / "AGENTS.md").write_text(station_file("analyst", tools="[shell_everything]"))
    code, out = st("validate", str(hub_dir))
    assert code == 2 and out["valid"] is False
    assert any(c["status"] != "pass" and "shell_everything" in c["message"] for c in out["checks"])
    code, text = st("validate", str(hub_dir), as_json=False)
    assert code == 2 and "fail" in text


def test_doctor_reports_each_station_and_its_login(hub_dir, monkeypatch):
    from hubzoid import factory_claude

    monkeypatch.setattr(factory_claude, "claude_login", lambda: None)
    code, out = st("doctor", str(hub_dir))
    assert code == 0, out
    analyst = out["stations"]["analyst"]
    assert analyst["engine"] == "claude-local" and analyst["tools"] == ["save_handoff"]
    assert analyst["structured_output"] == "native" and analyst["credentials_available"] is False
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-unique-7f3a9c")
    code, out = st("doctor", str(hub_dir))
    assert out["stations"]["analyst"]["credentials_available"] is True
    code, text = st("doctor", str(hub_dir), as_json=False)
    assert "analyst claude-local" in text and "login available" in text
    assert "unique-7f3a9c" not in text and "unique-7f3a9c" not in json.dumps(out)  # never prints a credential


def test_calls_and_handoffs_of_a_work_item(hub_dir):
    item, _ = service.admit(hub_dir, workflow="desk", key="k", requester=OWNER)
    code, out = st("calls", "list", str(hub_dir), "--work-item", item["id"])
    assert code == 0 and out["calls"] == []
    code, text = st("calls", "list", str(hub_dir), "--work-item", item["id"], as_json=False)
    assert code == 0 and "No station calls yet." in text
    code, out = st("calls", "show", str(hub_dir), "--work-item", item["id"], "--call", "call-missing")
    assert code == 2 and out["detail"]["code"] == "not_found"
    code, out = st("calls", "list", str(hub_dir), "--work-item", "wi-missing")
    assert code == 2 and out["detail"]["code"] == "not_found"

    code, out = st("list", str(hub_dir), "--work-item", item["id"], group="handoffs")
    assert code == 0 and out.get("handoffs", []) == []
    code, out = st("read", str(hub_dir), "--work-item", item["id"], "--id", "h-missing", group="handoffs")
    assert code == 2 and out["detail"]["code"] == "not_found"
