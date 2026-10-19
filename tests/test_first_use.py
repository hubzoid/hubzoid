"""First-use identity: what a bridge started without the gateway's environment finds."""
from __future__ import annotations

from hubzoid import deployment


def test_deployment_label_matches_bridge_without_loading_environment(tmp_path, monkeypatch):
    (tmp_path / 'AGENTS.md').write_text('---\nname: My Helpful Agent\n---\nHelp the team.\n')
    (tmp_path / '.env').write_text('MODEL_LABEL=example-agent\nDO_NOT_IMPORT=yes\n')
    monkeypatch.delenv('DO_NOT_IMPORT', raising=False)
    import os
    assert deployment.hubs(tmp_path)[0]['model_id'] == 'example-agent'
    assert 'DO_NOT_IMPORT' not in os.environ


def test_bridges_without_the_gateway_environment_find_owner_and_public_url(tmp_path, monkeypatch):
    """Under `gateway --no-bridges`, bridges and CLI commands do not inherit the
    gateway's environment. The manifest carries the owner (so the Console works
    after an upgrade) and the public address (so report links from a CLI-run
    job point at the site the gateway serves)."""
    from hubzoid import artifacts
    from hubzoid.access import session

    hub = tmp_path / "sales"
    hub.mkdir()
    deployment.save(tmp_path / "deployment.json",
                    hubs=[dict(key="sales", name="Sales", path=str(hub), model_id="sales")],
                    operational_url=f"sqlite:///{tmp_path / 'op.db'}",
                    owui_db=str(tmp_path / "webui.db"), owner="Owner@Example.org ",
                    public_url="https://hub.example.org/")
    for key in ("HUBZOID_ADMIN_EMAIL", "HUBZOID_PUBLIC_URL", "HUBZOID_DEPLOYMENT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    assert session.configured_owner(hub) == "owner@example.org"
    assert artifacts.viewer_url("a1", hub) == "https://hub.example.org/portal/artifacts/a1"
    monkeypatch.setenv("HUBZOID_ADMIN_EMAIL", "env@example.org")   # the environment wins
    assert session.configured_owner(hub) == "env@example.org"
