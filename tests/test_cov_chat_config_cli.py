"""`hubzoid chat config|theme|preferences|capabilities`: each command's output,
in JSON and for people, and its refusals."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from hubzoid.cli import app
from tests.chat_helpers import make_hub


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


def cli(*args, as_json=True):
    r = CliRunner().invoke(app, ["chat", *args, *(["--json"] if as_json else [])], catch_exceptions=False)
    return r.exit_code, (json.loads(r.output) if as_json else r.output)


def test_config_schema_get_and_sources(hub):
    code, schema = cli("config", "schema", str(hub))
    assert code == 0 and schema["type"] == "object" and "properties" in schema

    code, active = cli("config", "get", str(hub))
    assert code == 0 and active["file"]["exists"] is False and "sources" not in active
    assert active["digest"] and isinstance(active["config"], dict)

    code, full = cli("config", "get", str(hub), "--sources")
    assert code == 0 and full["sources"] and full["effective"]
    assert all({"value", "source"} <= set(item) for item in full["sources"].values())

    code, text = cli("config", "get", str(hub), "--effective", as_json=False)
    assert code == 0 and "chat/config revision" in text and "product defaults apply" in text


def test_config_validate_accepts_a_good_file_and_names_a_bad_one(hub, tmp_path):
    good = tmp_path / "good.yaml"
    good.write_text("version: 1\n")
    code, out = cli("config", "validate", str(hub), "--file", str(good))
    assert code == 0 and out["valid"] is True and out["digest"] and out["warnings"] == []
    code, text = cli("config", "validate", str(hub), "--file", str(good), as_json=False)
    assert code == 0 and "valid" in text

    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nno_such_setting: true\n")
    code, out = cli("config", "validate", str(hub), "--file", str(bad))
    assert code != 0 and out["detail"]["code"]

    code, text = cli("config", "get", str(tmp_path / "absent"), as_json=False)
    assert code == 2 and "is not a hub folder" in text


def test_theme_validate_and_inspect(hub, tmp_path):
    code, out = cli("theme", "validate", str(hub), "--file", str(tmp_path / "missing.json"))
    assert code == 2 and "does not exist" in out["detail"]["message"]
    broken = tmp_path / "broken.json"
    broken.write_text("{nope")
    code, out = cli("theme", "validate", str(hub), "--file", str(broken))
    assert code == 2 and out["detail"]["code"] == "validation_failed" and "not valid JSON" in out["detail"]["message"]
    minimal = tmp_path / "theme.json"
    minimal.write_text("{}")
    code, out = cli("theme", "validate", str(hub), "--file", str(minimal))
    assert code == 0 and out["valid"] is True and out["diagnostics"] == []
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"palettes": "not a mapping"}))
    code, out = cli("theme", "validate", str(hub), "--file", str(wrong))
    assert code != 0 and out["detail"]["code"]

    code, light = cli("theme", "inspect", str(hub))
    assert code == 0 and light["mode"] == "light" and light["tokens"] and light["typography"]
    code, out = cli("theme", "inspect", str(hub), "--mode", "sepia")
    assert code == 2 and "--mode is one of" in out["detail"]["message"]


def test_preferences_round_trip_and_refusals(hub):
    code, prefs = cli("preferences", "get", str(hub))
    assert code == 0 and "values" in prefs and "revision" in prefs
    code, out = cli("preferences", "set", str(hub), "--text-scale", "1.1")
    assert code == 2 and out["detail"]["pointer"] == "/values/text_scale" and 1.25 in out["detail"]["allowed"]
    code, out = cli("preferences", "set", str(hub), "--mode", "dark", "--text-scale", "1.25")
    assert code == 0, out
    code, prefs2 = cli("preferences", "get", str(hub))
    assert prefs2["values"]["mode"] == "dark" and prefs2["values"]["text_scale"] == 1.25
    assert prefs2["revision"] != prefs["revision"]
    code, out = cli("preferences", "set", str(hub), "--mode", "light",
                    "--expected-revision", str(prefs["revision"]))
    assert code == 4 and out["detail"]["code"]
    code, out = cli("preferences", "set", str(hub), "--mode", "neon")
    assert code != 0 and out["detail"]["code"]
    code, out = cli("preferences", "get", str(hub), "--user", "nobody@example.org")
    assert code != 0 and out["detail"]["code"] == "not_found"


def test_capabilities_describe_this_install(hub):
    code, caps = cli("capabilities", str(hub))
    assert code == 0 and isinstance(caps, dict) and caps
