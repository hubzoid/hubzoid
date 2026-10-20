"""Regressions for the 436c76b product/reliability review, using real Hubzoid contexts."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from tests import gitzoid_harness as h
from tests.test_gitzoid_workflows import REPO, add_comment, edit_head, store, sweep


@pytest.fixture
def hub(tmp_path, monkeypatch):
    h.isolate(monkeypatch)
    for name in ("GITHUB_APP_ID", "GITHUB_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY"):
        monkeypatch.delenv(name, raising=False)
    return h.make_hub(tmp_path)


def test_preview_activation_and_gate_changes_publish_without_new_analysis(hub):
    first = sweep(hub)
    assert first.calls
    h.write_settings(hub, {"review": {"post_to_github": True, "status_check": "gate"}})
    live = sweep(hub)
    assert not live.calls
    assert all(r["republished"] for r in live.value["reviews"])
    assert next(r for r in live.value["reviews"] if r["pr"].endswith("#102"))["status"] == "failure"
    assert sweep(hub).value["reviews"] == []


def test_effective_context_and_profile_changes_trigger_review(hub):
    sweep(hub)
    h.write_settings(hub, {"context": "Customer exports require sign-in."})
    assert sweep(hub).calls
    profiles = h.run_local(h.wf(hub, "repo_profiles"), hub_dir=hub, trigger="schedule")
    assert profiles.value["built"]
    reviewed = sweep(hub)
    assert any("fictional shop" in c["prompt"] for c in reviewed.calls)
    gz = h.lib(hub)
    saved = store(hub).profile(REPO)
    assert "app/auth.py" in gz.profiles.sensitive_paths(saved)
    assert gz.profiles.relevant_change([{"filename": "app/auth.py"}], saved)


def test_missing_patch_does_not_hide_later_readable_file_or_pass_gate(hub, monkeypatch):
    gz = h.lib(hub)
    cls = gz.sample.SampleGitHub
    original = cls.pull_files
    def files(self, repo, number):
        rows = original(self, repo, number)
        return [{**f, "patch": None} for f in rows] if number == 104 else rows
    monkeypatch.setattr(cls, "pull_files", files)
    h.write_settings(hub, {"review": {"status_check": "gate"}})
    result = sweep(hub)
    clean = next(r for r in result.value["reviews"] if r["pr"].endswith("#104"))
    assert clean["status"] == "failure"
    view = store(hub).view(REPO, 104)
    assert view["reviewed_files"] == 0 and view["left_out"]
    clean_text = gz.review.clean_summary(view)
    assert "**Partial review.**" in clean_text and "GitHub sent no patch" in clean_text
    text, left = gz.diff.format_diff([{"filename": "big.py", "patch": None},
                                    {"filename": "small.py", "patch": "@@ -0,0 +1 @@\n+x"}], 5000)
    assert "small.py" in text and left == ["big.py"]


def test_text_edit_and_missing_model_finding_do_not_resolve(hub):
    sweep(hub)
    edit_head(hub, "101-retry-supplier-sync", "app/sync.py", "while True:", "while True:  # retry")
    def misses(prompt, system, response_model):
        if response_model.__name__ == "Review":
            return {"summary": "No new findings", "findings": []}
        if response_model.__name__ == "Resolutions":
            return {"items": []}
        return h.scripted_llm(prompt, system, response_model)
    sweep(hub, llm=h.scripted(misses))
    assert store(hub).findings_for(REPO, 101, ("open",))
    assert not store(hub).findings_for(REPO, 101, ("fixed",))


def test_pat_owner_commands_work_and_author_cannot_defer_blocker(hub, monkeypatch):
    gz = h.lib(hub)
    monkeypatch.setattr(gz.sample.SampleGitHub, "login", lambda self: "maya")
    h.write_settings(hub, {"review": {"status_check": "gate"}})
    sweep(hub)
    add_comment(hub, "101-retry-supplier-sync", id=980, author="maya", body="@gitzoid pause")
    assert sweep(hub).value["commands"][0]["outcome"] == "pause"
    finding = store(hub).findings_for(REPO, 102, ("open",))[0]
    author = store(hub).review(REPO, 102)["author"]
    add_comment(hub, "102-admin-order-export", id=981, author=author, body="@gitzoid defer later",
                kind="review", in_reply_to=finding["comment_id"])
    result = sweep(hub)
    assert any("cannot defer" in c["outcome"] for c in result.value["commands"])
    assert store(hub).finding(finding["id"])["state"] == "open"


def test_backlog_modes_can_be_changed_without_a_push(hub):
    h.write_settings(hub, {"review": {"backlog": "new"}})
    first = sweep(hub)
    assert not first.calls and first.value["skipped"]
    h.write_settings(hub, {"review": {"backlog": "all", "max_prs_per_run": 2}})
    assert len(sweep(hub).value["reviews"]) == 2
    assert len(sweep(hub).value["reviews"]) == 2
    assert len(sweep(hub).value["reviews"]) == 1
    assert not sweep(hub).calls


def test_audit_resumes_past_file_budget_and_windows_cover_large_file(hub):
    h.write_settings(hub, {"audit": {"max_files_per_repo": 1}})
    run = h.wf(hub, "deep_audit")
    first = h.run_local(run, hub_dir=hub, trigger="schedule")
    assert first.value["unfinished"] and not store(hub).audit(REPO)["last_full"]
    seen = set(store(hub).audit(REPO)["done"])
    for _ in range(30):
        result = h.run_local(run, hub_dir=hub, trigger="schedule")
        current = store(hub).audit(REPO)
        seen.update(current["done"])
        if current["last_full"]:
            break
    assert current["last_full"] and len(seen) > 1
    root = hub / "raw_data" / "sample" / "repo"
    root.mkdir(exist_ok=True)
    big = root / "large.py"
    text = "print('evidence')\n" * 5000
    big.write_text(text)
    gz = h.lib(hub)
    units = gz.audit.windows(root, ["large.py"], [])
    assert len(units) > 1
    reconstructed = "".join("".join(text.splitlines(keepends=True)[start:end])
                            for _, start, end in map(gz.audit.unit, units))
    assert reconstructed == text


def test_dependency_assessment_invalidated_by_code_and_version(hub):
    gz = h.lib(hub)
    item = {"dep": {"name": "gunicorn", "version": "20", "ecosystem": "PyPI", "file": "requirements.txt"},
            "key": "GHSA-test", "severity": "high", "kev": False, "summary": "request parsing",
            "assessment_fp": "old"}
    st = store(hub)
    gz.security.ignore(st, REPO, item, "prior unreachable evidence", "2026-10-12")
    assert not gz.security.needs_assessment(st, REPO, item)
    assert gz.security.needs_assessment(st, REPO, {**item, "assessment_fp": "new-code"})
    assert gz.security.needs_assessment(st, REPO, {**item, "dep": {**item["dep"], "version": "21"}})
    assert not gz.security.unused(item["dep"], {("PyPI", "gunicorn")}, [])


def test_ci_permission_failure_is_unknown_and_pat_login_is_supported(tmp_path):
    gz = h.template_lib()
    def handle(request):
        return httpx.Response(200, json={"login": "maya"}) if request.url.path == "/user" else httpx.Response(403, json={"message": "scope"})
    gh = gz.github.GitHub("test-token", transport=httpx.MockTransport(handle))
    assert gh.login() == "maya"
    assert gh.ci_status("a/b", "sha").startswith("unavailable")


def test_app_auth_refreshes_installation_tokens_and_uses_bot_identity():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    gz = h.template_lib()
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    paths, tokens = [], []
    def handle(request):
        paths.append(request.url.path)
        if request.url.path == "/app":
            return httpx.Response(200, json={"slug": "team-gitzoid"})
        if request.url.path.endswith("/access_tokens"):
            tokens.append(request.headers["Authorization"])
            return httpx.Response(201, json={"token": f"installation-{len(tokens)}",
                                            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        return httpx.Response(200, json={"default_branch": "main"})
    transport = httpx.MockTransport(handle)
    auth = gz.github.AppAuth("123", "456", pem, transport=transport)
    gh = gz.github.GitHub("", app_auth=auth, transport=transport)
    assert gh.login() == "team-gitzoid[bot]"
    gh.default_branch("a/b")
    assert len(tokens) == 1
    auth.expires = 0
    gh.default_branch("a/b")
    assert len(tokens) == 2 and "/user" not in paths
    assert "installation-2" in gh.client.headers["Authorization"]


def test_review_events_survive_later_row_changes_and_exclude_automation(hub):
    sweep(hub)
    st = store(hub)
    events = st.review_runs("2000", "2100", [REPO])
    assert len(events) == 5
    st.save_review(REPO, 101, state="closed")
    assert st.review_runs("2000", "2100", [REPO]) == events
    gz = h.lib(hub)
    pr = {"author": "author", "reviews": [
        {"author": "app[bot]", "author_type": "Bot", "state": "COMMENTED", "submitted_at": "2026-10-01"},
        {"author": "pat-owner", "body": "GitZoid found 1 issue(s)", "state": "COMMENTED", "submitted_at": "2026-10-02"},
        {"author": "reviewer", "state": "APPROVED", "submitted_at": "2026-10-03"}]}
    assert gz.metrics.first_review(pr) == "2026-10-03"


def test_setup_preserves_custom_rules_budgets_and_has_no_network_side_effects(hub):
    spec = importlib.util.spec_from_file_location("setup_gitzoid", hub / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    configured = module.configure(hub, ["a/b", "a/c"], publish=True, backlog="all", bots=True)
    assert configured["review"]["post_to_github"] and not configured["review"]["skip_bots"]
    assert configured["limits"]["model_calls_per_day"] == 400
    assert configured["digest_groups"]["team"]["monthly_reports"] == ["code", "delivery"]
    assert (hub / "workflows/settings.yaml.before-setup").is_file()
    h.write_settings(hub, {"repos": [{"name": "a/c", "model": "custom-model", "context": "Sensitive exports"}, "a/b"]})
    again = module.configure(hub, ["a/c", "a/d"], publish=False)
    assert again["security_groups"]["team"]["repos"] == ["a/c"]
    assert again["repos"][0] == {"name": "a/c", "model": "custom-model", "context": "Sensitive exports"}
    assert again["digest_groups"]["new-repos"]["monthly_reports"] == ["code", "delivery"]


def test_setup_check_and_reports_have_honest_scope(hub):
    check = h.run_local(h.wf(hub, "setup_check"), hub_dir=hub, trigger="operator")
    assert check.value["ready"] and not check.calls
    report = h.run_local(h.wf(hub, "code_report"), hub_dir=hub, trigger="tool", inputs={"period": "last_week"})
    html = report.artifacts[0]["html"]
    assert "Evidence and scope" in html and "Metadata only" in html
    assert "PRs changing test files" in html
    assert "table-scroll" in html and "tabindex" in html


def test_report_code_evidence_is_cached_and_synthesis_limit_is_visible(hub, monkeypatch):
    gz = h.lib(hub)
    gh = gz.sample.SampleGitHub(hub / "raw_data/sample")
    gh.name = "github"
    gh.pull_files = lambda repo, number: [
        {"filename": "app/a.py", "patch": "@@ -1 +1 @@\n-old\n+new"},
        {"filename": "app/b.py", "patch": None}]
    monkeypatch.setattr(gz.runtime, "make_github", lambda *args, **kwargs: gh)
    first = h.run_local(h.wf(hub, "code_report"), hub_dir=hub, trigger="tool", inputs={"period": "last_week"})
    assert any(c["response_model"] == "ChangeEvidence" for c in first.calls)
    assert "file patches unavailable" in first.artifacts[0]["html"]
    assert "The shown code" in first.artifacts[0]["html"]
    second = h.run_local(h.wf(hub, "delivery_report"), hub_dir=hub, trigger="tool", inputs={"period": "last_week"})
    assert all(c["response_model"] != "ChangeEvidence" for c in second.calls)
    assert any("bounded operation" in c["prompt"] for c in second.calls)


def test_all_ignored_review_has_a_view_and_does_not_call_model(hub, monkeypatch):
    gz = h.lib(hub)
    original = gz.rules.repo_controls
    monkeypatch.setattr(gz.rules, "repo_controls", lambda *a: {**original(*a), "ignore": ["**/*", "*"]})
    first = sweep(hub)
    assert not first.calls
    assert store(hub).view(REPO, 101)["ignored"]
    h.write_settings(hub, {"review": {"post_to_github": True}})
    second = sweep(hub)
    assert not second.calls and all(r.get("republished") for r in second.value["reviews"])


def test_optional_age_cutoff_and_queued_state_are_visible(hub):
    import yaml
    path = hub / "raw_data/sample/pulls/101-retry-supplier-sync/pull.yaml"
    data = yaml.safe_load(path.read_text())
    data["created_at"] = "2025-01-01T00:00:00Z"
    path.write_text(yaml.safe_dump(data))
    h.write_settings(hub, {"review": {"max_age_days": 60, "max_prs_per_run": 1}})
    first = sweep(hub)
    assert store(hub).review(REPO, 101)["state"] == "skipped"
    assert any(r["state"] == "waiting" and "Queued" in r["note"] for r in store(hub).reviews())
    assert first.value["deferred"]
    h.write_settings(hub, {"review": {"max_age_days": 0, "max_prs_per_run": 10}})
    assert any(r["pr"].endswith("#101") for r in sweep(hub).value["reviews"])


def test_low_severity_is_an_effective_opt_in():
    gz = h.template_lib()
    finding = {"path": "a.py", "line": 1, "severity": "low", "confidence": "high", "category": "tests", "body": "Missing edge-case test"}
    assert not gz.review.gate([finding], {("a.py", 1)}, "high", 8)
    assert gz.review.gate([finding], {("a.py", 1)}, "low", 8)
