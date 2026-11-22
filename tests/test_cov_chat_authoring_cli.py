"""`hubzoid workspace`: draft, review, publish and roll back a hub file, list
files and versions, preview OpenAPI tools and render prompts, from the CLI."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from hubzoid.cli import app
from tests.chat_helpers import make_hub


@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, monkeypatch)
    monkeypatch.setenv("HUBZOID_IN_APP_EDITING", "true")  # the Workspace is a Beta switch
    return hub


def ws(*args, as_json=True):
    r = CliRunner().invoke(app, ["workspace", *args, *(["--json"] if as_json else [])], catch_exceptions=False)
    return r.exit_code, (json.loads(r.output) if as_json else r.output)


def test_files_and_versions(hub):
    code, files = ws("files", str(hub))
    assert code == 0 and "AGENTS.md" in [f["path"] for f in files["items"]]
    code, versions = ws("versions", "AGENTS.md", "--hub", str(hub))
    assert code == 0 and isinstance(versions["items"], list)


@pytest.mark.xfail(strict=True, raises=AttributeError,
                   reason="BUG: c9b070b6 removed AuthoringService's draft API; the `workspace drafts` commands remain")
def test_drafts_create_makes_a_draft(hub):
    code, draft = ws("drafts", "create", "AGENTS.md", "--hub", str(hub))
    assert code == 0 and draft["path"] == "AGENTS.md"


def test_openapi_preview_and_prompts(hub, tmp_path):
    spec = tmp_path / "api.yaml"
    spec.write_text(
        "openapi: 3.0.3\ninfo: {title: Orders, version: '1'}\nservers: [{url: 'https://orders.example.com'}]\n"
        "paths:\n  /orders/{id}:\n    get:\n      operationId: getOrder\n      summary: Read one order\n"
        "      parameters: [{name: id, in: path, required: true, schema: {type: string}}]\n"
        "      responses: {'200': {description: ok}}\n")
    code, out = ws("openapi-preview", "--file", str(spec), str(hub))
    assert code == 0 and "getOrder" in json.dumps(out)
    code, out = ws("openapi-preview", "--file", str(spec), "--select", "noSuchOperation", str(hub))
    assert code != 0 or "noSuchOperation" not in json.dumps(out.get("tools", out))

    code, prompts = ws("prompts", "list", str(hub))
    assert code == 0 and isinstance(prompts["items"], list)
    code, out = ws("prompts", "render", "no-such-prompt", "--hub", str(hub))
    assert code != 0 and out["detail"]["code"] == "not_found"
