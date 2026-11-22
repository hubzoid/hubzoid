"""Channel CLI validates input, preserves revisions, renders state and maps failures."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from hubzoid.channels import cli, config, doctor, replies, store
from hubzoid.channels.errors import ChannelError
from tests.test_channels_contract import _user, applied, ev, fake as fake, hub_dir as hub_dir, send, yaml_for

runner = CliRunner()


def invoke(hub_dir, command, *args, as_json=True):
    return runner.invoke(cli.channels_app, [command, str(hub_dir), *map(str, args), *(["--json"] if as_json else [])])


@pytest.mark.parametrize("as_json", [True, False])
def test_cli_validate_apply_export_list_and_toggle(hub_dir, as_json):
    file = hub_dir / "candidate.yaml"
    file.write_text(yaml_for())
    out = invoke(hub_dir, "validate", "--file", file, as_json=as_json)
    assert out.exit_code == 0 and (json.loads(out.stdout)["valid"] if as_json else "is valid" in out.stdout)
    out = invoke(hub_dir, "apply", "--file", file, "--expected-revision", "absent", as_json=as_json)
    assert out.exit_code == 0 and (json.loads(out.stdout)["created"] if as_json else "applied: revision 1" in out.stdout)
    for command, args, expected in [("list", [], "fake-main"), ("export", ["fake-main"], "secret_ref"),
                                    ("pause", ["fake-main", "--expected-revision", 1], "disabled"),
                                    ("resume", ["fake-main", "--expected-revision", 2], "enabled")]:
        out = invoke(hub_dir, command, *args, as_json=as_json)
        assert out.exit_code == 0, out.stdout
        assert expected in out.stdout
    assert config.active(hub_dir, "fake-main").config.enabled


@pytest.mark.parametrize("as_json", [True, False])
def test_cli_reports_invalid_config_and_revision(hub_dir, as_json):
    file = hub_dir / "invalid.yaml"
    file.write_text(yaml_for() + "unknown: true\n")
    out = invoke(hub_dir, "validate", "--file", file, as_json=as_json)
    assert out.exit_code == 2 and (json.loads(out.stdout)["detail"]["fields"][0]["field"] == "/unknown"
                                   if as_json else "/unknown (line" in out.stdout)
    out = invoke(hub_dir, "apply", "--file", file, "--expected-revision", "absent", as_json=as_json)
    assert out.exit_code == 2 and "not valid" in out.stdout
    file.write_text(yaml_for())
    out = invoke(hub_dir, "apply", "--file", file, "--expected-revision", "oops", as_json=as_json)
    assert out.exit_code == 2 and "expected-revision is a number" in out.stdout
    out = invoke(hub_dir / "missing", "list", as_json=as_json)
    assert out.exit_code == 2 and "not a hub folder" in out.stdout


@pytest.mark.parametrize("status,expected", [(400, 2), (403, 3), (409, 4), (503, 5), (500, 1)])
def test_cli_maps_channel_status_to_exit_code(hub_dir, monkeypatch, status, expected):
    def fail(*a):
        raise ChannelError("refused", "specific reason", status=status)
    monkeypatch.setattr(config, "active", fail)
    out = invoke(hub_dir, "doctor", "fake-main")
    assert out.exit_code == expected
    assert json.loads(out.stdout)["detail"] == {"code": "refused", "message": "specific reason"}


def test_cli_work_item_error_and_actor_fallback(hub_dir, monkeypatch):
    from hubzoid.work_items.errors import WorkItemError
    def fail(*a):
        raise WorkItemError("forbidden", "Denied", status=403)
    monkeypatch.setattr(config, "active", fail)
    out = invoke(hub_dir, "doctor", "fake-main")
    assert out.exit_code == 3 and json.loads(out.stdout)["detail"]["code"] == "forbidden"
    monkeypatch.setattr(cli.getpass, "getuser", lambda: (_ for _ in ()).throw(OSError("unavailable")))
    assert cli._actor() == "cli:operator"
    assert cli._expected(None, True) is None


@pytest.mark.parametrize("as_json", [True, False])
def test_cli_thread_views_subscriptions_links_and_delivery_views(hub_dir, as_json):
    conn = applied(hub_dir)
    _user(hub_dir, "ana@example.org")
    row = send(conn, ev()).events[0]
    thread = store.get_thread(hub_dir, row["thread_id"])
    tid = thread["id"]
    for command, args, expected in [
        ("threads", ["fake-main"], tid), ("deliveries", ["fake-main"], row["id"]),
        ("inspect", ["fake-main", "--thread", tid], "fake:t1"),
        ("subscribe", ["fake-main", "--thread", tid, "--expected-revision", thread["revision"]], "subscribed"),
        ("unsubscribe", ["fake-main", "--thread", tid], "subscribed"),
        ("link", ["fake-main", "--external-id", "U1", "--account", "ana@example.org"], "ana@example.org"),
        ("links", ["fake-main"], "U1"),
    ]:
        out = invoke(hub_dir, command, *args, as_json=as_json)
        assert out.exit_code == 0, out.stdout
        assert expected in out.stdout
    lid = store.list_links(hub_dir, conn.hub, "fake")[0]["id"]
    out = invoke(hub_dir, "unlink", "fake-main", lid, as_json=as_json)
    assert out.exit_code == 0 and (json.loads(out.stdout)["revoked"] if as_json else "removed" in out.stdout)
    assert store.list_links(hub_dir, conn.hub, "fake") == []
    out = invoke(hub_dir, "outbox", "fake-main", as_json=as_json)
    assert out.exit_code == 0 and (json.loads(out.stdout) == {"items": []} if as_json else "operation" in out.stdout)


@pytest.mark.parametrize("ready", [True, False])
def test_cli_doctor_formats_recovery_and_exit_status(hub_dir, monkeypatch, ready):
    conn = SimpleNamespace(id="fake-main")
    monkeypatch.setattr(config, "active", lambda *a: conn)
    captured = []
    def run(c, inputs):
        captured.append(inputs)
        return {"ready": ready, "checks": [{"id": "probe", "status": "ok" if ready else "fail",
                                               "message": "Result", "recovery": "Fix setting"}]}
    monkeypatch.setattr(doctor, "run", run)
    out = invoke(hub_dir, "doctor", "fake-main", "--actor", "U1", "--workspace", "W1",
                 "--input", " value = yes ", as_json=False)
    assert out.exit_code == (0 if ready else 1)
    assert ("Ready." if ready else "Fix setting") in out.stdout
    assert captured == [{"value": "yes", "actor": "U1", "workspace_id": "W1"}]


def test_cli_reconcile_passes_explicit_duplicate_risk_and_evidence(hub_dir, monkeypatch):
    applied(hub_dir)
    captured = []
    def resolve(conn, op_id, **kw):
        captured.append((op_id, kw))
        return {"id": op_id, "state": "succeeded"}
    monkeypatch.setattr(replies, "resolve", resolve)
    out = invoke(hub_dir, "reconcile", "fake-main", "op1", "--decision", "retry", "--evidence", "confirmed",
                 "--accept-duplicate-risk")
    assert out.exit_code == 0 and json.loads(out.stdout)["state"] == "succeeded"
    assert captured[0][0] == "op1"
    assert captured[0][1]["evidence"] == {"note": "confirmed"}
    assert captured[0][1]["acknowledge_duplicate_risk"] is True


def test_cli_empty_list_and_unapplied_file(hub_dir):
    out = invoke(hub_dir, "list", as_json=False)
    assert out.exit_code == 0 and "No channels" in out.stdout
    (hub_dir / "channels").mkdir()
    (hub_dir / "channels" / "new.yaml").write_text(yaml_for(id="new"))
    out = invoke(hub_dir, "list", as_json=False)
    assert out.exit_code == 0 and "is not applied yet" in out.stdout
