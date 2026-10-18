"""The chat parity ledger (hubzoid.parity): validation, coverage, privacy,
and digest-bound review and freeze through the service, API and CLI."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from hubzoid.parity import store
from hubzoid.parity.validate import Bundle, ParityError, check, ineffective, load
from tests.chat_helpers import ORIGIN, as_people, build_app, make_hub, who

SDK = Path(__file__).resolve().parents[1]


def ob(i: int, **over) -> dict:
    base = {"id": f"HZP-{i:06d}", "title": f"Outcome {i}", "outcome": f"People can do thing {i}",
            "authority_context": "signed-in person", "configuration_context": "default",
            "source_occurrences": [{"source": "ref", "path": f"src/a{i}.svelte", "line_start": 10}],
            "owner_issue": "HUB-72", "consumer_issues": [], "native_destination": "chat",
            "default_semantics": "on", "migration": {"kind": "preserve", "reason": "required"},
            "acceptance": [{"id": f"HZP-{i:06d}-a1", "runner": "playwright", "path": "portal/tests/e2e/x.spec.ts",
                            "selector": f"HZP-{i:06d}", "fixture": "synthetic", "expected": "works"}],
            "status": "mapped", "aliases": []}
    base.update(over)
    return base


def bundle(**over) -> Bundle:
    data = {
        "manifest": {"version": 1, "ledger": "l", "sources": "s", "providers": "p", "deployments": [],
                     "source_release": "0.12.3", "report_dir": "r"},
        "ledger": [ob(1), ob(2)],
        "sources": {"version": 1, "sources": [{"id": "ref", "repository": "x", "release": "1", "license": "MIT",
                                               "upstream_verified": True, "identity_kind": "upstream_commit",
                                               "upstream_commit": "a" * 40,
                                               "files": {"src/a1.svelte": hashlib.sha256(b"ref").hexdigest()}}],
                    "locators": [{"id": "L1", "source": "ref", "path": "src/a1.svelte", "line": 10,
                                  "kind": "appendix_row"}]},
        "providers": [], "deployments": {}}
    data.update(over)
    return Bundle(data["manifest"], data["ledger"], data["sources"], data["providers"], data["deployments"])


def inventory(**over) -> dict:
    inv = json.loads((SDK / "docs/parity/input/dep-example.json").read_text())
    inv.update(over)
    return inv


def test_shipped_ledger_validates_and_covers_every_locator():
    r = check(load(SDK, "docs/parity/parity.yaml"))
    assert r["valid"], r["errors"][:3]
    assert r["counts"]["obligations"] > 500 and r["counts"]["unmapped_locators"] == 0
    assert r["status"] == "needs_evidence"  # honest: evidence gaps remain


def test_absent_false_and_unknown_differ():
    inv = inventory()
    b = bundle(deployments={"dep-example": inv},
               manifest={**bundle().manifest, "deployments": [{"id": "dep-example", "inventory": "x"}]})
    r = check(b)
    kinds = r["counts"]["settings_by_evidence"]["dep-example"]
    assert kinds["absent"] == 1 and kinds["unknown"] == 1 and kinds["observed"] >= 2
    assert any("setting(s) unknown" in x["message"] for x in r["blockers"])
    bad = inventory(settings=[{"key": "X", "evidence_kind": "unknown"}])
    r = check(bundle(deployments={"dep-example": bad}))
    assert r["errors"] and "unknown_reason" in r["errors"][0]["message"]


def test_wrong_memory_flag_is_not_effective_false():
    b = bundle(deployments={"dep-example": inventory()})
    rows = {r["key"]: r for r in ineffective(b)}
    assert rows["ENABLE_MEMORY"]["declared"] is False and rows["ENABLE_MEMORY"]["effective"] is True


def test_gateway_override_provenance():
    inv = inventory()
    scopes = {r["key"]: r.get("policy_scope") for r in inv["settings"]}
    assert scopes["WEBUI_SECRET_KEY"] == "gateway_inherited"
    assert check(bundle(deployments={"dep-example": inv}))["valid"]


def test_duplicate_alias_preserves_distinct_outcomes():
    same = check(bundle(ledger=[ob(1), ob(2, outcome="People can do thing 1")]))
    assert any(e["code"] == "duplicate_obligation" for e in same["errors"])
    other_context = check(bundle(ledger=[ob(1), ob(2, outcome="People can do thing 1",
                                                   configuration_context="admin locked")]))
    assert other_context["valid"]


def test_every_locator_accounted_for():
    s = copy.deepcopy(bundle().sources)
    s["locators"].append({"id": "L2", "source": "ref", "path": "src/other.svelte", "line": 3, "kind": "setting"})
    r = check(bundle(sources=s))
    assert any(b["code"] == "coverage_gap" and b["locators"] == ["L2"] for b in r["blockers"])
    s["locators"][-1]["classification"] = "Open WebUI process wiring, no user outcome"
    assert not any(b["code"] == "coverage_gap" for b in check(bundle(sources=s))["blockers"])


def test_source_hash_drift_blocks_freeze(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.svelte").write_text("one")
    import hashlib

    s = copy.deepcopy(bundle().sources)
    s["sources"][0]["files"] = {"src/a.svelte": hashlib.sha256(b"one").hexdigest()}
    assert not check(bundle(sources=s), source_root=tmp_path)["blockers"]
    assert check(bundle(sources=s), require_sources=True)["blockers"][0]["code"] == "source_unverified"
    (tmp_path / "src" / "a.svelte").write_text("two")
    assert check(bundle(sources=s), source_root=tmp_path)["blockers"][0]["code"] == "source_changed"


def test_parent_git_head_is_not_upstream_identity():
    s = copy.deepcopy(bundle().sources)
    s["sources"][0]["identity_kind"] = "parent_repo_head"
    assert not check(bundle(sources=s))["valid"]
    loose = copy.deepcopy(bundle().sources)
    loose["sources"][0]["upstream_commit"] = "abc"  # verified claims need an immutable identity
    assert not check(bundle(sources=loose))["valid"]


def test_missing_selected_provider_adapter_blocks():
    r = check(bundle(providers=[{"id": "p1", "family": "search", "reference": "x", "customer_required": True,
                                 "disposition": "key_provider"}]))
    assert any("no adapter owner" in b["message"] for b in r["blockers"])
    unselected = check(bundle(providers=[{"id": "p2", "family": "search", "reference": "x",
                                          "customer_required": True, "disposition": "deferred"}]))
    assert not unselected["blockers"]


def test_selected_scope_disposition():
    gap = ob(3, status="evidence_missing", source_occurrences=[])
    assert check(bundle(ledger=[ob(1), gap]))["blockers"]
    later = ob(3, status="evidence_missing", source_occurrences=[], release_scope="non_release",
               disposition_reason="Not a 2.0 key provider (B037).")
    r = check(bundle(ledger=[ob(1), later]))
    assert not r["blockers"] and r["counts"]["non_release"] == 1
    no_reason = ob(3, release_scope="non_release")
    assert not check(bundle(ledger=[ob(1), no_reason]))["valid"]


def test_f4_only_omits_community_destination():
    wrong = ob(3, migration={"kind": "approved_omission", "reason": "x", "founder_decision": "F4",
                             "omitted_destination": "folders"})
    assert not check(bundle(ledger=[ob(1), wrong]))["valid"]
    none = ob(3, migration={"kind": "approved_omission", "reason": "x"})
    assert not check(bundle(ledger=[ob(1), none]))["valid"]
    right = ob(3, migration={"kind": "approved_omission", "reason": "x", "founder_decision": "F4",
                             "omitted_destination": "community"})
    assert check(bundle(ledger=[ob(1), right]))["valid"]


def test_secret_field_never_logged():
    inv = inventory(settings=[{"key": "OPENAI_API_KEY", "declared_value": "sk-live-abc123",
                               "evidence_kind": "observed"}])
    r = check(bundle(deployments={"dep-example": inv}))
    err = next(e for e in r["errors"] if e["code"] == "sensitive_field")
    assert "sk-live" not in json.dumps(r) and "OPENAI_API_KEY" in err["message"]
    for value in ("postgres://user:hunter2@db/x", "https://h/x?token=abc", {"nested": {"url": "s3://k:v@b"}}):
        inv = inventory(settings=[{"key": "DATABASE_URL", "declared_value": value, "evidence_kind": "observed"}])
        r = check(bundle(deployments={"dep-example": inv}))
        assert any(e["code"] == "sensitive_field" for e in r["errors"]), value
        assert "hunter2" not in json.dumps(r["errors"])


def test_symlink_and_reserved_path_rejected(tmp_path):
    (tmp_path / "parity.yaml").write_text("version: 1\nledger: ../x.jsonl\nsources: s\nproviders: p\n"
                                          "source_release: '1'\n")
    with pytest.raises(ParityError) as exc:
        load(tmp_path)
    assert exc.value.code == "unsafe_path"
    (tmp_path / "parity.yaml").write_text("version: 1\nledger: Keys/x.jsonl\nsources: s\nproviders: p\n"
                                          "source_release: '1'\n")
    with pytest.raises(ParityError) as exc:
        load(tmp_path)
    assert exc.value.code == "unsafe_path"
    outside = tmp_path.parent / "outside.jsonl"
    outside.write_text("")
    (tmp_path / "link.jsonl").symlink_to(outside)
    (tmp_path / "parity.yaml").write_text("version: 1\nledger: link.jsonl\nsources: s\nproviders: p\n"
                                          "source_release: '1'\n")
    with pytest.raises(ParityError):
        load(tmp_path)
    (tmp_path / "Keys").mkdir()
    (tmp_path / "Keys" / "secret.json").write_text("{}")
    (tmp_path / "ok.jsonl").symlink_to(tmp_path / "Keys" / "secret.json")  # in-root link to a credential
    (tmp_path / "parity.yaml").write_text("version: 1\nledger: ok.jsonl\nsources: s\nproviders: p\n"
                                          "source_release: '1'\n")
    with pytest.raises(ParityError) as exc:
        load(tmp_path)
    assert exc.value.code == "unsafe_path"
    s = copy.deepcopy(bundle().sources)
    s["sources"][0]["files"] = {"../../.env": "0" * 64}
    assert any(e["code"] == "unsafe_path" for e in check(bundle(sources=s), source_root=tmp_path)["errors"])


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def refs(tmp_path):
    """The inspected reference file the test bundle's source records."""
    root = tmp_path / "refs"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a1.svelte").write_bytes(b"ref")
    return root


def test_review_bound_to_exact_digest(hub, refs):
    svc = store.ParityService(hub)
    clean = svc.create(bundle(), actor="owner")
    assert clean["status"] == "needs_review"
    with pytest.raises(ParityError):
        svc.review(clean["id"], digest="sha256:00", expected_revision=1, decision="approve", actor="owner")
    rv = svc.review(clean["id"], digest=clean["digest"], expected_revision=1, decision="approve", actor="owner")
    with pytest.raises(ParityError) as exc:  # the reference files were not re-hashed
        svc.freeze(clean["id"], digest=clean["digest"], review_id=rv["review_id"], expected_revision=2,
                   actor="owner")
    assert exc.value.code == "missing_evidence"
    frozen = svc.freeze(clean["id"], digest=clean["digest"], review_id=rv["review_id"], expected_revision=2,
                        source_root=refs, actor="owner")
    assert svc.get(clean["id"])["status"] == "frozen" and frozen["digest"] == clean["digest"]
    blocked = svc.create(bundle(ledger=[ob(1), ob(5, status="evidence_missing")]), actor="owner")
    assert blocked["status"] == "blocked"
    rv2 = svc.review(blocked["id"], digest=blocked["digest"], expected_revision=1, decision="approve", actor="o")
    with pytest.raises(ParityError) as exc:
        svc.freeze(blocked["id"], digest=blocked["digest"], review_id=rv2["review_id"], expected_revision=2,
                   source_root=refs, actor="o")
    assert exc.value.code == "missing_evidence"
    later = svc.create(bundle(ledger=[ob(1), ob(2), ob(3)]), actor="owner", base_id=clean["id"])
    assert svc.diff(clean["id"], later["id"])["added"] == ["HZP-000003"]


def test_unsigned_review_cannot_freeze(hub, refs):
    svc = store.ParityService(hub)
    c = svc.create(bundle(), actor="o")
    rv = svc.review(c["id"], digest=c["digest"], expected_revision=1, decision="approve", actor="o", signed=False)
    with pytest.raises(ParityError):
        svc.freeze(c["id"], digest=c["digest"], review_id=rv["review_id"], expected_revision=2, source_root=refs,
                   actor="o")


def test_rejection_supersedes_earlier_approval(hub, refs):
    svc = store.ParityService(hub)
    c = svc.create(bundle(), actor="o")
    ok = svc.review(c["id"], digest=c["digest"], expected_revision=1, decision="approve", actor="o")
    svc.review(c["id"], digest=c["digest"], expected_revision=2, decision="reject", actor="o")
    for rev in (2, 3):
        with pytest.raises(ParityError) as exc:
            svc.freeze(c["id"], digest=c["digest"], review_id=ok["review_id"], expected_revision=rev,
                       source_root=refs, actor="o")
        assert exc.value.code in ("revision_conflict", "review_superseded")


def test_cross_deployment_denied(hub):
    made = store.ParityService(hub).create(bundle(), actor="o")
    other = store.ParityService(hub)
    other.deployment_id = "dep_other"
    with pytest.raises(ParityError):
        other.get(made["id"])


def test_revoked_reviewer_cannot_freeze(hub, monkeypatch):
    from hubzoid.access import store_for

    client = TestClient(build_app())
    raw = bundle().canonical()
    made = client.post("/api/parity/candidates", headers=ORIGIN, json={"bundle": raw}).json()
    store_for(hub).grant("ana@example.org", "*", "manage_access", actor="test")
    as_people(monkeypatch)
    review = client.post(f"/api/parity/candidates/{made['id']}/reviews", headers=who("ana"),
                         json={"digest": made["digest"], "expected_revision": 1, "decision": "approve"})
    assert review.status_code == 201, review.text
    store_for(hub).revoke("ana@example.org", "*", "manage_access", actor="test")
    assert client.get(f"/api/parity/candidates/{made['id']}", headers=who("ana")).status_code == 403
    r = client.post(f"/api/parity/candidates/{made['id']}/freeze", headers=who("ana"),
                    json={"digest": made["digest"], "review_id": review.json()["review_id"],
                          "expected_revision": 2})
    assert r.status_code == 403


def test_cli_and_api_give_the_same_digest(hub):
    client = TestClient(build_app())
    out = CliRunner().invoke(__import__("hubzoid.cli", fromlist=["app"]).app,
                             ["parity", "validate", str(SDK), "--file", "docs/parity/parity.yaml", "--json"])
    assert out.exit_code == 6  # valid, evidence still missing
    cli_digest = json.loads(out.output)["digest"]
    api = client.post("/api/parity/validations", headers=ORIGIN,
                      json={"bundle": load(SDK, "docs/parity/parity.yaml").canonical()}).json()
    assert api["digest"] == cli_digest
