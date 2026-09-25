"""Imports and rollback must preserve the same boundaries as individual grants."""

import copy

import pytest
from sqlalchemy import create_engine

from hubzoid import db, deployment
from hubzoid.access.store import GrantStore


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    yield GrantStore(engine)
    engine.dispose()


@pytest.mark.parametrize(
    "row",
    [
        ("*", "finance", "manage_access"),
        ("alice", "*", "use_hub"),
        ("alice", "", "ledger"),
    ],
)
def test_bulk_rejects_reserved_scope_atomically(store, row):
    with pytest.raises(ValueError):
        store.grant_many([("bob", "finance", "ledger"), row])
    assert store.list_grants() == []


def test_migration_cannot_escape_target_hubs(store):
    store.grant("existing", "finance", "ledger")
    before = store.list_grants()
    with pytest.raises(ValueError):
        store.apply_migration([("alice", "ops", "ledger")], [], ["finance"])
    assert store.list_grants() == before


@pytest.mark.parametrize("corrupt", ["authority", "public_admin", "attribute_scope"])
def test_invalid_rollback_leaves_state_untouched(store, corrupt):
    store.grant("alice", "finance", "ledger")
    snapshot = store.snapshot(["finance"])
    bad = copy.deepcopy(snapshot)
    if corrupt == "authority":
        bad["authority"]["finance"] = "false"
    elif corrupt == "public_admin":
        bad["grants"].append(("*", "finance", "manage_access"))
    else:
        bad["attrs"].append(["ops", "alice", "center", "other"])
    with pytest.raises(ValueError):
        store.restore(bad, actor="operator")
    assert store.snapshot(["finance"]) == snapshot


def test_deployment_rejects_split_configuration(tmp_path, monkeypatch):
    hub = tmp_path / "finance"
    hub.mkdir()
    deployment.save(
        tmp_path / "deployment.json",
        hubs=[dict(path=str(hub), key="finance", dbos_url="sqlite:///registered.db")],
        operational_url="sqlite:///shared.db",
        owui_url="http://shared-ui",
        owui_db="unused",
    )
    monkeypatch.setenv("OWUI_INTERNAL_URL", "http://different-ui")
    with pytest.raises(ValueError, match="OWUI_INTERNAL_URL"):
        deployment.owui_url(hub)
    with pytest.raises(ValueError, match="HUBZOID_DBOS_DB"):
        db.dbos_url(hub, {"HUBZOID_DBOS_DB": "sqlite:///different.db"})
    with pytest.raises(ValueError, match="not registered"):
        deployment.read(
            tmp_path / "other",
            {"HUBZOID_DEPLOYMENT": str(tmp_path / "deployment.json")},
        )


def test_visibility_restore_keeps_non_access_fields(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import json
    import httpx
    from hubzoid.access import owui
    from hubzoid.access.reconcile import restore_visibility

    writes = []
    current = dict(
        id="finance",
        name="Updated name",
        meta={"description": "kept"},
        params={},
        is_active=True,
    )

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=current)
        writes.append(json.loads(request.content))
        return httpx.Response(200, json={})

    @contextmanager
    def client(_):
        with httpx.Client(
            base_url="http://owui", transport=httpx.MockTransport(handle)
        ) as c:
            yield c

    monkeypatch.setattr(owui, "client_for", client)
    grants = [
        dict(principal_type="group", principal_id="legacy-team", permission="read")
    ]
    restore_visibility(tmp_path, dict(model_id="finance", access_grants=grants))
    assert writes == [{**current, "access_grants": grants}]


def test_cli_rollback_retries_visibility_failure(tmp_path, monkeypatch):
    import json
    from typer.testing import CliRunner
    from hubzoid.access import store_for, reconcile
    from hubzoid.cli import app

    gs = store_for(tmp_path)
    gs.grant("legacy", tmp_path.name, "use_hub")
    backup = gs.snapshot([tmp_path.name])
    backup["owui_visibility"] = dict(model_id="agent", access_grants=[])
    path = tmp_path / "backup.json"
    path.write_text(json.dumps(backup))
    gs.grant("new", tmp_path.name, "ledger")
    gs.set_authoritative(True, hub=tmp_path.name)
    calls = []

    def restore(hub, snapshot):
        calls.append(snapshot)
        if len(calls) == 1:
            raise RuntimeError("OWUI temporarily unavailable")

    monkeypatch.setattr(reconcile, "restore_visibility", restore)
    result = CliRunner().invoke(app, ["access", "rollback", str(path), str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "maintenance window open" in result.output
    assert not gs.is_authoritative(tmp_path.name)
    assert not gs.can("new", tmp_path.name, "ledger")
    result = CliRunner().invoke(app, ["access", "rollback", str(path), str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert gs.can("legacy", tmp_path.name, "use_hub")


def test_schema_cache_tracks_engine_lifetime():
    import gc
    import weakref
    from hubzoid import migrations
    from hubzoid.access import db_tables

    engine = create_engine("sqlite://")
    db_tables.ensure_access_tables(engine)
    assert engine in migrations._done
    ref = weakref.ref(engine)
    engine.dispose()
    del engine
    gc.collect()
    assert ref() is None
    other = create_engine("sqlite://")
    try:
        assert other not in migrations._done
        assert GrantStore(other).list_grants() == []
    finally:
        other.dispose()


def test_recreated_account_cannot_inherit_old_email_permissions(store):
    store.upsert_identity(email="person@example.com", owui_id="original")
    store.grant("person@example.com", "finance", "ledger")
    store.reconcile_accounts([])
    assert not store.can("person@example.com", "finance", "ledger")
    store.reconcile_accounts(
        [dict(id="replacement", email="person@example.com", role="user")]
    )
    assert store.is_suspended("person@example.com")
    assert store.list_grants("finance") == []
    store.suspend("person@example.com", actor="admin", suspended=False)
    assert not store.can("person@example.com", "finance", "ledger")
    store.grant("person@example.com", "finance", "ledger")
    assert store.can("person@example.com", "finance", "ledger")


def test_directory_pending_and_presignup_access(store):
    store.grant("future@example.com", "finance", "ledger")
    store.reconcile_accounts([])
    assert not store.is_suspended("future@example.com")
    store.reconcile_accounts(
        [dict(id="new", email="future@example.com", role="pending")]
    )
    assert not store.can("future@example.com", "finance", "ledger")
    store.reconcile_accounts([dict(id="new", email="future@example.com", role="user")])
    assert store.can("future@example.com", "finance", "ledger")


def test_chat_account_binding_denies_recycled_email(tmp_path):
    from fastapi import HTTPException
    from starlette.requests import Request
    from hubzoid.access import store_for
    from hubzoid.server import _enforce_use_hub

    gs = store_for(tmp_path)
    gs.upsert_identity(email="person@example.com", owui_id="original")
    gs.grant("person@example.com", tmp_path.name, "use_hub")
    gs.set_authoritative(True, hub=tmp_path.name)
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-openwebui-user-email", b"person@example.com"),
                (b"x-openwebui-user-id", b"replacement"),
            ],
        }
    )
    with pytest.raises(HTTPException) as error:
        _enforce_use_hub(request, tmp_path)
    assert error.value.status_code == 403


@pytest.mark.parametrize(
    "pages",
    [
        [{}],
        [{"users": [], "total": 1}],
        [
            {"users": [{"id": "a", "email": "a@example.com"}], "total": 2},
            {"users": [], "total": 1},
        ],
        [
            {"users": [{"id": "a", "email": "a@example.com"}], "total": 2},
            {"users": [{"id": "a", "email": "a@example.com"}], "total": 2},
        ],
    ],
)
def test_directory_refuses_incomplete_snapshots(pages):
    import httpx
    from hubzoid.access.owui import users

    iterator = iter(pages)
    with httpx.Client(
        base_url="http://owui",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=next(iterator))
        ),
    ) as client:
        with pytest.raises(ValueError):
            users(client)
