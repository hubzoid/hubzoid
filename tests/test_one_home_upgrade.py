"""`hubzoid upgrade` and the op_0080 cleanup: nothing kept only in the
database is dropped before it is written into its file."""
from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import create_engine, inspect, text
from typer.testing import CliRunner

from hubzoid import cli, migrations


def _to(engine, revision: str) -> None:
    """Bring a store to one revision (as a 2.0 development database is)."""
    from alembic.runtime.environment import EnvironmentContext

    cfg, script = migrations._script("operational")
    with EnvironmentContext(cfg, script, fn=lambda rev, ctx: script._upgrade_revs(revision, rev),
                            destination_rev=revision) as env:
        with engine.connect() as conn:
            env.configure(connection=conn, version_table=migrations.STORES["operational"])
            with env.begin_transaction():
                env.run_migrations()
            conn.commit()


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "HUBZOID_DEPLOYMENT_FILE", "HUBZOID_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_SECRET_KEY", "x" * 43 + "=")
    hub = tmp_path / "ops"
    (hub / ".hubzoid").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: Ops\n---\nbody")
    return hub


def _seed(hub):
    eng = create_engine(f"sqlite:///{hub / '.hubzoid' / 'hub.db'}")
    _to(eng, "op_0071")
    now = time.time()
    conf = {"version": 1, "identity": {"name": "Example Ops"}}
    with eng.begin() as c:
        c.execute(text("INSERT INTO hz_branding_revisions (id, deployment_id, digest, configuration_json, "
                       "theme_json, assets_json, validator_version, created_at) VALUES ('r1', 'd', 'sha256:1', "
                       ":c, 'null', '[]', 'v', :t)"), {"c": json.dumps(conf), "t": now})
        c.execute(text("INSERT INTO hz_branding_active (deployment_id, revision_id, generation) "
                       "VALUES ('d', 'r1', 1)"))
        c.execute(text("INSERT INTO hz_authoring_versions (id, hub_id, path, sha256, content, published_by, "
                       "published_at) VALUES ('v1', 'ops', 'AGENTS.md', 's', 'old text', 'u', :t)"), {"t": now})
    return eng


def test_the_cleanup_refuses_until_the_upgrade_wrote_everything_out(hub):
    eng = _seed(hub)
    with pytest.raises(RuntimeError, match="hubzoid upgrade"):
        migrations.upgrade(eng, "operational")
    assert "hz_branding_revisions" in inspect(eng).get_table_names()   # nothing dropped

    result = CliRunner().invoke(cli.app, ["upgrade", str(hub)])
    assert result.exit_code == 0, result.output
    doc = (hub / "hubzoid.yaml").read_text()
    assert "Example Ops" in doc and "brand:" in doc
    assert list((hub / ".hubzoid" / "exports" / "workspace-versions" / "ops").glob("*AGENTS.md"))
    tables = set(inspect(create_engine(f"sqlite:///{hub / '.hubzoid' / 'hub.db'}")).get_table_names())
    assert "hz_branding_revisions" not in tables and "hz_authoring_versions" not in tables
    assert "hz_branding_legal" in tables                              # each person's stays


def test_a_dry_run_changes_nothing(hub):
    eng = _seed(hub)
    result = CliRunner().invoke(cli.app, ["upgrade", str(hub), "--dry-run"])
    assert result.exit_code == 0 and "would:" in result.output
    assert not (hub / "hubzoid.yaml").exists()
    assert "hz_branding_revisions" in inspect(eng).get_table_names()


def test_a_fresh_database_needs_no_upgrade(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'new.db'}")
    migrations.upgrade(eng, "operational")
    assert migrations.current(eng, "operational") == "op_0080"


def test_a_repository_with_its_own_agents_md_uses_the_gateway_manifest(tmp_path, monkeypatch):
    # A folder of hubs often has an AGENTS.md for coding agents: with a
    # gateway's data folder named, its hubs come from the manifest.
    from hubzoid import deployment

    for key in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "HUBZOID_DEPLOYMENT_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_SECRET_KEY", "x" * 43 + "=")
    root = tmp_path / "repo"
    (root / "sales").mkdir(parents=True)
    (root / "AGENTS.md").write_text("Notes for coding agents.\n")
    (root / "sales" / "AGENTS.md").write_text("---\nname: Sales\n---\nbody")
    gw = root / "gateway-data"
    deployment.save(gw / "deployment.json", hubs=[dict(key="sales", name="Sales", path=str(root / "sales"),
                                                       model_id="sales", slug="sales")],
                    operational_url=f"sqlite:///{gw / 'op.db'}", public_url="https://hubs.example.org",
                    auth=True, name="Acme")
    result = CliRunner().invoke(cli.app, ["upgrade", str(root), "--data-dir", str(gw)])
    assert result.exit_code == 0, result.output
    doc = (root / "hubzoid.yaml").read_text()
    assert "- sales" in doc and "public_url: https://hubs.example.org" in doc and "name: Acme" in doc
