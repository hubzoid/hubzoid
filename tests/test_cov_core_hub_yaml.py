"""Hub YAML rejects unreadable, oversized, aliased and malformed declarations."""
from __future__ import annotations

import pytest

from hubzoid.hubs import yamlfile


@pytest.mark.parametrize(("content", "message"), [
    (b"a: 1\na: 2\n", "line 2: duplicate key 'a'"),
    (b"a: &x [1]\nb: *x\n", "line 2: aliases are not allowed"),
    (b"\xff", "not UTF-8 text"), (b"[1,2]", "top level must be a mapping"),
    (b"a: [", "line 1"), (b"a: !custom value", "could not determine a constructor"),
])
def test_invalid_hub_yaml_names_file_and_problem(tmp_path, content, message):
    path = tmp_path / "settings.yaml"
    path.write_bytes(content)
    with pytest.raises(yamlfile.HubFileError) as exc:
        yamlfile.load(path)
    assert str(path) in str(exc.value) and message in str(exc.value)


def test_missing_and_oversized_hub_yaml(tmp_path, monkeypatch):
    path = tmp_path / "settings.yaml"
    with pytest.raises(yamlfile.HubFileError, match="cannot be read.*FileNotFoundError"):
        yamlfile.load(path)
    monkeypatch.setattr(yamlfile, "MAX_BYTES", 10)
    path.write_bytes(b"x" * 11)
    with pytest.raises(yamlfile.HubFileError, match="larger than 1 MiB"):
        yamlfile.load(path)


def test_empty_mapping_and_line_lookup(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("")
    assert yamlfile.load(path) == {}
    path.write_text("version: 1\nsettings:\n  - name: VALUE\n    default: example\n")
    assert yamlfile.load(path)["settings"][0]["default"] == "example"
    assert yamlfile.line_of(path, "VALUE") == 3
    assert yamlfile.line_of(path, "unknown") is None
    assert yamlfile.line_of(tmp_path / "absent", "anything") is None
