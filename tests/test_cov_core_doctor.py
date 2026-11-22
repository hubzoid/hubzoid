"""Doctor names unavailable stores and settings without network or secret disclosure."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hubzoid import datadir, db, doctor, migrations, runtime, settings
from tests.test_doctor import _by_id, hub  # noqa: F401


def test_hub_checks_keep_independent_loader_failures_visible(hub, monkeypatch):
    from hubzoid import access, scheduling
    from hubzoid.access import resolver
    from hubzoid.connectors import registry
    from hubzoid.loaders import tools_local
    from hubzoid.workflows import observe

    monkeypatch.setattr(runtime, "build", lambda path: SimpleNamespace(name="fixture"))
    monkeypatch.setattr(tools_local, "load_all", Mock(side_effect=ValueError("bad tool")))
    monkeypatch.setattr(scheduling, "load_tasks", Mock(side_effect=OSError("schedules unreadable")))
    monkeypatch.setattr(observe, "definitions", lambda path: [
        {"name": "nightly", "source": "main.py", "error": None, "schedule": None, "timezone": "UTC"}])
    monkeypatch.setattr(registry, "problems", Mock(side_effect=OSError("unreadable")))
    monkeypatch.setattr(access, "load_restricted", Mock(side_effect=ValueError("restricted broken")))
    monkeypatch.setattr(resolver, "hub_roster", Mock(side_effect=ValueError("roster broken")))
    checks = _by_id(doctor._hub_checks(hub))
    assert checks["schedule.tasks"].summary == "Schedules could not be read: schedules unreadable"
    assert checks["access.restricted"].summary == "Restricted tools do not load: restricted broken"
    assert checks["identity.resolver"].summary == "Identity roster does not load: roster broken"
    assert checks["workflows.definitions"].detail == ["nightly (manual, UTC)"]


def test_data_directory_checks_writeability_and_old_copy(hub, tmp_path, monkeypatch):
    external = tmp_path / "external"
    monkeypatch.setattr(datadir, "configured", lambda: external)
    result = doctor._data_dir(hub)
    assert result.status == "fail" and "not a folder this process can write" in result.summary
    external.mkdir()
    old = hub / ".hubzoid"
    old.mkdir()
    (old / "hub.db").write_bytes(b"old")
    result = doctor._data_dir(hub)
    assert result.status == "warn" and "older copy" in result.summary


@pytest.mark.parametrize(("current", "known", "status", "message"), [
    (None, True, "info", "Unversioned"), ("old", True, "warn", "Schema at old"),
    ("future", False, "fail", "unknown revision future"),
])
def test_schema_readiness_distinguishes_old_unversioned_and_future_stores(hub, tmp_path, monkeypatch,
                                                                           current, known, status, message):
    path = tmp_path / "database.db"
    path.touch()
    monkeypatch.setattr(db, "operational_url", lambda hub: f"sqlite:///{path}")
    monkeypatch.setattr(db, "resolve_url", lambda hub: f"sqlite:///{path}")
    monkeypatch.setattr(migrations, "current", lambda *a: current)
    monkeypatch.setattr(migrations, "head", lambda store: "head")
    revision = Mock(return_value=object(), side_effect=None if known else ValueError("unknown"))
    monkeypatch.setattr(migrations, "_script", lambda store: (None, SimpleNamespace(get_revision=revision)))
    results = doctor._schema(hub)
    assert len(results) == 2
    assert all(r.status == status and message in r.summary for r in results)


def test_schema_misconfiguration_and_unreachable_store_are_distinct(hub, tmp_path, monkeypatch):
    path = tmp_path / "database.db"
    path.touch()
    monkeypatch.setattr(db, "operational_url", Mock(side_effect=ValueError("manifest conflict")))
    monkeypatch.setattr(db, "resolve_url", lambda hub: f"sqlite:///{path}")
    monkeypatch.setattr(migrations, "current", Mock(side_effect=RuntimeError("schema unreadable")))
    results = _by_id(doctor._schema(hub))
    assert results["db.operational"].summary == "Database is misconfigured: manifest conflict"
    assert results["db.hub"].summary == "Database unreachable: RuntimeError: schema unreadable"


def test_network_filesystem_prefers_longest_mount_and_skips_failed_db_url(hub, monkeypatch):
    # Fixed paths, not the temporary hub: they resolve the same on macOS and Linux.
    table = "invalid\nroot / nfs rw\nroot /private apfs rw\nshare /mnt/share nfs4 rw\n"
    assert doctor.network_filesystem(Path("/private/data/hub"), table) is None       # the longest mount wins
    assert doctor.network_filesystem(Path("/mnt/share/hub"), table) == "nfs4"
    monkeypatch.setattr(db, "operational_url", Mock(side_effect=ValueError("conflict")))
    monkeypatch.setattr(db, "dbos_url", lambda hub: "sqlite:////mount/workflows.db")
    monkeypatch.setattr(doctor, "network_filesystem", lambda path: "nfs4")
    result = doctor._sqlite_on_network(hub)
    assert result.status == "fail" and "workflow database is SQLite on a network filesystem (nfs4" in result.summary


def test_sqlite_probe_failure_does_not_hide_other_checks(hub, monkeypatch):
    monkeypatch.setattr(db, "dbos_url", Mock(side_effect=ValueError("conflict")))
    assert doctor._sqlite(hub) is None
    monkeypatch.setattr(settings, "load", Mock(side_effect=ValueError("invalid settings")))
    assert doctor._runner_checks(hub) == []
    assert doctor._subagent_checks(hub) == []


def test_model_credential_check_reports_provider_exception(hub, monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "validate_environment", Mock(side_effect=RuntimeError("unknown provider")))
    result = doctor._model(hub)
    assert result.status == "warn" and "could not check credentials (unknown provider)" in result.summary


def test_diagnostic_orchestrator_retains_failures_and_hides_secret_exception_values(hub, monkeypatch):
    monkeypatch.setattr(doctor, "_config_checks", lambda *a, **kw: ([], {}, False))
    monkeypatch.setattr(settings, "load", lambda *a, **kw: SimpleNamespace())
    for name in ("_hub_checks", "_schema", "_eval_checks", "_auth", "_app_checks"):
        monkeypatch.setattr(doctor, name, lambda *a, **kw: [])
    for name in ("_versions", "_data_dir", "_sqlite", "_sqlite_on_network", "_google_merge", "_exposure",
                 "_legacy_management_flag", "_mcp_access_group"):
        monkeypatch.setattr(doctor, name, lambda *a, name=name: doctor.Check(name, "info", "fixture"))
    for name in ("_model", "_store_checks", "_hub_setup_checks", "_secret_checks"):
        monkeypatch.setattr(doctor, name, Mock(side_effect=RuntimeError("synthetic-private-value")))
    results = _by_id(doctor.run(hub, fetch_secrets=False))
    assert results["model.credentials"].status == "warn"
    assert results["db.read"].status == "fail"
    assert results["settings.files"].summary == "Hub settings could not be checked (RuntimeError)"
    assert results["secrets.store"].summary == "Stored secrets could not be checked (RuntimeError)"
    assert "synthetic-private-value" not in results["secrets.store"].summary
