"""`hubzoid knowledge`: collections applied from a manifest, synced, queried
and cited from the CLI, with their refusals."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from hubzoid.cli import app
from tests.chat_helpers import make_hub

POLICY = ("# Order exceptions\n\nAn order exception above 500 units must be escalated to the operations lead "
          "within one working day.\n\nSmall exceptions are recorded in the weekly log.")


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


def kn(*args, as_json=True):
    r = CliRunner().invoke(app, ["knowledge", *args, *(["--json"] if as_json else [])], catch_exceptions=False)
    return r.exit_code, (json.loads(r.output) if as_json else r.output)


def manifest(tmp_path, body: str):
    path = tmp_path / "collection.yaml"
    path.write_text(body)
    return path


def test_collection_apply_sync_query_cite_export_delete(hub, tmp_path):
    code, out = kn("collections", "list", str(hub))
    assert code == 0 and out["items"] == []

    text = json.dumps(POLICY)
    path = manifest(tmp_path, f"id: policies\ntitle: Policies\nsources:\n  - id: rules\n    kind: text\n"
                              f"    title: Rules\n    text: {text}\n")
    code, made = kn("collections", "apply", "--file", str(path), "--expected-revision", "absent", str(hub))
    assert code == 0 and made["revision"] == 1, made
    code, out = kn("collections", "apply", "--file", str(path), "--expected-revision", "absent", str(hub))
    assert code == 4 and out["detail"]["code"]

    code, got = kn("collections", "get", "policies", "--hub", str(hub))
    assert code == 0 and got["manifest"]["title"] == "Policies"
    code, out = kn("collections", "export", "policies", "--hub", str(hub), as_json=False)
    assert code == 0 and "title: Policies" in out and "kind: text" in out

    code, job = kn("sync", "policies", "--hub", str(hub))
    assert code == 0 and job["state"] == "ready" and job["counters"]["passages"] >= 1
    code, again = kn("jobs", "get", job["job_id"], "--hub", str(hub))
    assert code == 0 and again["state"] == "ready"
    code, waited = kn("jobs", "wait", job["job_id"], "--hub", str(hub), "--timeout", "5")
    assert code == 0 and waited["state"] == "ready"

    code, q = kn("query", "When must an order exception be escalated?", "--hub", str(hub))
    assert code == 0 and q["results"] and "operations lead" in q["results"][0]["excerpt"]
    code, cit = kn("citation", q["results"][0]["citation_id"], "--hub", str(hub))
    assert code == 0 and cit["title"] == "Rules" and "500 units" in cit["passage"]
    code, out = kn("citation", "not-a-citation", "--hub", str(hub))
    assert code != 0 and out["detail"]["code"] == "citation_unavailable"  # never says whether it exists

    code, out = kn("collections", "delete", "policies", "--hub", str(hub))
    assert code == 0 and out == {"deleted": "policies"}
    code, out = kn("collections", "get", "policies", "--hub", str(hub))
    assert code != 0 and out["detail"]["code"] == "not_found"


def test_bad_manifests_and_unknown_jobs_are_refused(hub, tmp_path):
    path = manifest(tmp_path, "id: x\nsources: [unclosed\n")
    code, out = kn("collections", "apply", "--file", str(path), "--expected-revision", "absent", str(hub))
    assert code == 2 and out["detail"]["code"] == "invalid_config" and str(path) in out["detail"]["message"]
    path = manifest(tmp_path, "id: x\ntitle: X\nsources:\n  - id: s\n    kind: telepathy\n")
    code, out = kn("collections", "apply", "--file", str(path), "--expected-revision", "absent", str(hub))
    assert code != 0 and out["detail"]["code"]
    code, out = kn("jobs", "get", "job-missing", "--hub", str(hub))
    assert code != 0 and out["detail"]["code"] == "not_found"
    code, out = kn("sync", "no-such-collection", "--hub", str(hub))
    assert code != 0 and out["detail"]["code"] == "not_found"
