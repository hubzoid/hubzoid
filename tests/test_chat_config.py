"""Chat configuration, preferences and the shared theme (hubzoid.chat.config,
config_routes, theme): one schema and apply across file, CLI and API."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from hubzoid.chat import config as cfg
from hubzoid.chat import theme
from tests.chat_helpers import ORIGIN, as_people, build_app, make_hub, who


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def client(hub):
    return TestClient(build_app())


def _put(client, config, expected, key=None, headers=ORIGIN):
    h = dict(headers)
    if key:
        h["Idempotency-Key"] = key
    return client.put("/api/chat/config", headers=h,
                      json={"version": 1, "expected_revision": expected, "config": config})


DARK = {"presentation": {"appearance": {"default_mode": "dark"}}}


def test_config_defaults_without_files(client):
    r = client.get("/api/chat/config?sources=true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 0 and body["file"] == {"path": "chat/config.yaml", "exists": False}
    assert body["config"]["presentation"]["locale"] == {"default": "en", "allowed": ["en"]}
    assert body["sources"]["/presentation/appearance/default_mode"] == {"value": "dark",
                                                                        "source": "default"}
    assert r.headers["X-Request-ID"]


def test_unknown_key_names_line_and_nearest(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\npresentation:\n  apperance:\n    default_mode: dark\n")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.parse_file(path)
    assert exc.value.code == "unknown_key"
    assert exc.value.extra["line"] == 3 and "appearance" in exc.value.extra["nearest"]


def test_yaml_aliases_tags_and_duplicates_rejected(tmp_path):
    for text in ("version: 1\na: &x 1\nb: *x\n", "version: !!python/object 1\n",
                 "version: 1\nversion: 1\n"):
        path = tmp_path / "c.yaml"
        path.write_text(text)
        with pytest.raises(cfg.ConfigError) as exc:
            cfg.parse_file(path)
        assert exc.value.code == "invalid_yaml"


def test_unknown_namespace_and_planned_feature():
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.validate_document({"version": 1, "voices": {}})
    assert exc.value.code == "unknown_key"
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.validate_document({"version": 1, "voice": {}})
    assert exc.value.code == "capability_unavailable"
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.validate_document({"version": 2})
    assert exc.value.code == "unsupported_schema"


def test_default_must_be_allowed():
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.validate_document({"version": 1, "presentation": {"appearance": {
            "default_mode": "oled", "allowed_modes": ["light", "dark"]}}})
    assert exc.value.code == "validation_failed"


def test_atomic_apply_conflict_and_create_only(client):
    first = _put(client, DARK, "absent")
    assert first.status_code == 200 and first.json()["revision"] == 1
    again = _put(client, DARK, "absent")
    assert again.status_code == 409 and again.json()["detail"]["current_revision"] == 1
    a = _put(client, {"presentation": {"appearance": {"default_mode": "light"}}}, 1)
    b = _put(client, {"presentation": {"appearance": {"default_mode": "oled"}}}, 1)
    assert a.status_code == 200 and b.status_code == 409
    assert b.json()["detail"]["code"] == "revision_conflict"
    assert client.get("/api/chat/config").json()["config"]["presentation"]["appearance"][
        "default_mode"] == "light"


def test_idempotency_replay_and_payload_mismatch(client):
    one = _put(client, DARK, "absent", key="k-1")
    two = _put(client, DARK, "absent", key="k-1")
    assert one.json()["revision"] == two.json()["revision"] == 1 and two.json()["replayed"]
    other = _put(client, {"presentation": {"timezone": "Asia/Kolkata"}}, "absent", key="k-1")
    assert other.status_code == 409 and other.json()["detail"]["code"] == "idempotency_key_reused"


def test_preferences_lock_allowed_and_revision(client):
    prefs = client.get("/api/chat/preferences").json()
    assert prefs["revision"] == 0 and prefs["effective"]["mode"]["source"] == "default"
    ok = client.put("/api/chat/preferences", headers=ORIGIN,
                    json={"expected_revision": 0, "values": {"mode": "oled", "text_scale": 1.25}})
    assert ok.status_code == 200 and ok.json()["effective"]["mode"] == {
        "value": "oled", "source": "account", "locked": False,
        "allowed": ["light", "dark", "oled", "system"]}
    stale = client.put("/api/chat/preferences", headers=ORIGIN,
                       json={"expected_revision": 0, "values": {"mode": "dark"}})
    assert stale.status_code == 409
    _put(client, {"presentation": {"preferences": {"mode": False},
                                   "appearance": {"default_mode": "dark"}}}, "absent")
    locked = client.put("/api/chat/preferences", headers=ORIGIN,
                        json={"expected_revision": 1, "values": {"mode": "light"}})
    assert locked.status_code == 422 and locked.json()["detail"]["code"] == "policy_locked"
    eff = client.get("/api/chat/preferences").json()["effective"]["mode"]
    assert eff["value"] == "dark" and eff["locked"]  # the stored oled no longer wins


def test_no_public_policy_leak(client, monkeypatch):
    as_people(monkeypatch)
    boot = client.get("/api/chat/bootstrap")  # nobody signed in
    assert boot.status_code == 200
    assert set(boot.json()) == {"config_revision", "locale", "appearance", "preferences",
                                "timezone", "theme_revision"}
    assert client.get("/api/chat/config").status_code == 401


def test_teammate_cannot_read_or_apply(client, monkeypatch):
    as_people(monkeypatch)
    assert client.get("/api/chat/config", headers=who("ana")).status_code == 403
    r = _put(client, DARK, "absent", headers=who("ana"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "permission_denied"
    # but her own preferences are hers
    assert client.get("/api/chat/preferences", headers=who("ana")).status_code == 200


def test_schema_references_resolve_from_the_root():
    from hubzoid.chat import config as cfg

    doc = cfg.schema()
    refs = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("$ref"), str):
                refs.append(node["$ref"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc["properties"])
    assert refs and all(r.split("/", 2)[2] in doc["$defs"] for r in refs)


def test_theme_contrast_pairs_and_shape():
    with pytest.raises(theme.ThemeError) as exc:
        theme.resolve({"version": 1, "palettes": {"light": {"action": "#FFE0D0"}}})
    assert exc.value.code == "contrast_failure"
    assert any(f["mode"] == "light" and f["foreground"] == "actionText"
               and f["background"] == "action" for f in exc.value.extra["failures"])
    for bad in ({"version": 1, "palettes": {"light": {"action": "rgba(0,0,0,.5)"}}},
                {"version": 1, "palettes": {"light": {"--x": "#000000"}}},
                {"version": 1, "typography": {"baseSize": 40}}):
        with pytest.raises(theme.ThemeError):
            theme.resolve(bad)
    good = theme.resolve({"version": 1, "palettes": {"dark": {"link": "#FF9A70"}}})
    assert good["palettes"]["oled"]["link"] == "#FF9A70"  # dark overrides reach OLED
    assert good["palettes"]["oled"]["canvas"] == "#000000"


def test_theme_route_serves_css_for_both_surfaces(client):
    body = client.get("/api/chat/theme").json()
    assert body["revision"] == "builtin"
    dark = body["css"]["dark"]
    assert dark["--hz-accent"] == "#E5572A" and dark["--paper"] == "#222224"
    assert dark["--hz-accent-active"] == body["palettes"]["dark"]["actionActive"]


def test_cli_apply_matches_api(hub, client, tmp_path):
    from hubzoid.cli import app as cli_app

    (hub / "chat").mkdir()
    (hub / "chat" / "config.yaml").write_text(
        "version: 1\npresentation:\n  appearance:\n    default_mode: light\n")
    runner = CliRunner()
    diff = runner.invoke(cli_app, ["chat", "config", "diff", str(hub), "--json"])
    assert diff.exit_code == 0, diff.output
    assert json.loads(diff.output)["changes"][0]["pointer"] == "/presentation/appearance/default_mode"
    missing = runner.invoke(cli_app, ["chat", "config", "apply", str(hub), "--json"])
    assert missing.exit_code == 2
    applied = runner.invoke(cli_app, ["chat", "config", "apply", str(hub), "--expected-revision",
                                      "absent", "--json"])
    assert applied.exit_code == 0, applied.output
    api = client.get("/api/chat/config").json()
    assert api["revision"] == 1 and api["digest"] == json.loads(applied.output)["digest"]
    assert api["file"]["matches_active"] is True
    conflict = runner.invoke(cli_app, ["chat", "config", "apply", str(hub), "--expected-revision",
                                       "0", "--json"])
    assert conflict.exit_code == 4
    assert json.loads(conflict.output)["detail"]["code"] == "revision_conflict"
