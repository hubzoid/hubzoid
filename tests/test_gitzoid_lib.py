"""The GitZoid template's library (hubzoid/templates/gitzoid/workflows/gitzoid/lib):
plain code, no hub, no network, no model."""
from __future__ import annotations

import io
import json
import tarfile
from datetime import datetime, timezone

import httpx
import pytest

from tests.gitzoid_harness import template_lib

gz = template_lib()


# ------------------------------------------------------------------ settings

def test_settings_defaults_and_helpers():
    s = gz.settings.load({"repos": ["acme/web", {"name": "acme/api", "severity_floor": "medium", "commands": False,
                                                 "status_check": "gate"}],
                          "security_groups": {"plat": {"repos": ["acme/web"]}}})
    assert s.source == "sample" and s.review.status_check == "report"
    assert s.floor_for("acme/web") == "high" and s.floor_for("acme/api") == "medium"
    assert s.commands_for("acme/web") and not s.commands_for("acme/api")
    assert s.status_for("acme/api") == "gate"
    assert set(s.security_scope()) == {"plat", "unassigned"}


@pytest.mark.parametrize("raw, words", [
    ({"repos": ["a/b"], "security_groups": {"x": {"repos": ["a/b"]}, "y": {"repos": ["a/b"]}}}, "at most one"),
    ({"repos": ["a/b"], "digest_groups": {"x": {"repos": ["c/d"]}}}, "not under repos"),
    ({"reveiw": {}}, "reveiw"),
    ({"repos": ["not-a-repo"]}, "owner/name"),
    ({"review": {"status_check": "block"}}, "status_check"),
    ({"repos": ["a/b"], "security_groups": {"unassigned": {"repos": ["a/b"]}}}, "reserved"),
])
def test_settings_errors_name_the_key(raw, words):
    with pytest.raises(gz.settings.SettingsError, match=words):
        gz.settings.load(raw)


# --------------------------------------------------------------------- rules

def test_review_md_controls():
    c = gz.rules.parse_review_md("---\nskip: false\nseverity_floor: medium\nignore: [docs/**]\nfocus: auth\n"
                                 "skip_title: [WIP]\nbase_branches: [main]\npaths: {'app/**': check money}\n---\nBe strict.")
    assert c["severity_floor"] == "medium" and c["ignore"] == ["docs/**"] and c["focus"] == ["auth"]
    assert c["skip_title"] == ["wip"] and c["path_instructions"] == {"app/**": "check money"}
    assert c["instructions"] == "Be strict."
    assert gz.rules.parse_review_md("---\n: : bad\n---\nbody")["instructions"] == "body"


def test_skip_reasons():
    rc = gz.settings.load({}).review
    pr = {"author": "ana", "author_type": "User", "draft": False, "labels": [], "title": "Add x", "base_ref": "main"}
    assert gz.rules.skip_reason(pr, rc, {}) is None
    assert gz.rules.skip_reason({**pr, "draft": True}, rc, {}) == "draft"
    assert "bot" in gz.rules.skip_reason({**pr, "author": "renovate[bot]", "author_type": "Bot"}, rc, {})
    assert "gitzoid-skip" in gz.rules.skip_reason({**pr, "labels": ["gitzoid-skip"]}, rc, {})
    assert "wip" in gz.rules.skip_reason({**pr, "title": "WIP: x"}, rc, {"skip_title": ["wip"]})
    assert "targets" in gz.rules.skip_reason({**pr, "base_ref": "dev"}, rc, {"base_branches": ["main"]})
    assert "skip: true" in gz.rules.skip_reason(pr, rc, {"skip": True})


def test_hub_rules_globs_and_code_checks(tmp_path):
    folder = tmp_path / "knowledge" / "review-rules"
    folder.mkdir(parents=True)
    (folder / "no-print.md").write_text("---\ntitle: No print\npaths: ['app/*.py']\nseverity: medium\n"
                                        "pattern: '^\\s*print\\('\n---\nUse log.")
    (folder / "everywhere.md").write_text("Plain rule with no front matter.")
    rules = gz.rules.hub_rules(tmp_path)
    assert [r["title"] for r in gz.rules.rules_for(rules, "app/x.py")] == ["everywhere", "No print"]
    assert [r["title"] for r in gz.rules.rules_for(rules, "lib/x.py")] == ["everywhere"]
    files = [{"filename": "app/x.py", "patch": "@@ -1,1 +1,2 @@\n a = 1\n+print(a)"},
             {"filename": "lib/x.py", "patch": "@@ -1,1 +1,2 @@\n a = 1\n+print(a)"}]
    hits = gz.rules.code_checks(rules, files)
    assert [(h["path"], h["line"], h["severity"]) for h in hits] == [("app/x.py", 2, "medium")]
    first = gz.rules.write_rule(tmp_path, "No print", "Use log.", ["app/**"], "admin@x.com")
    second = gz.rules.write_rule(tmp_path, "No print", "Again.", [], None)
    assert first != second and (tmp_path / second).is_file()


# ---------------------------------------------------------------------- diff

PATCH = "@@ -1,3 +1,4 @@\n a = 1\n-b = 2\n+b = 3\n+c = 4\n d = 5"


def test_diff_lines():
    assert gz.diff.added_lines(PATCH) == [(2, "b = 3"), (3, "c = 4")]
    assert gz.diff.commentable([{"filename": "f.py", "patch": PATCH}]) == {("f.py", n) for n in (1, 2, 3, 4)}
    text, left = gz.diff.format_diff([{"filename": "a.py", "patch": PATCH}, {"filename": "b.py", "patch": PATCH * 50}], 200)
    assert "a.py" in text and left == ["b.py"]


def test_anchor_survives_moves_and_detects_change():
    code = "def f():\n    x = load()\n    while True:\n        retry(x)\n    return x\n"
    a = gz.diff.anchor(code, 3)
    moved = "import os\n\n" + code
    assert gz.diff.find_anchor(moved, a) == 5
    assert gz.diff.find_anchor(code.replace("while True", "for _ in range(3)"), a) is None
    assert gz.diff.enclosing_function(code, 4) == "f"
    assert gz.diff.enclosing_function('@bp.get("/x")\n@login\ndef export():\n    pass\n', 1) == "export"


def test_parse_unified():
    files = gz.diff.parse_unified("diff --git a/app/x.py b/app/x.py\nindex 1..2 100644\n--- a/app/x.py\n+++ b/app/x.py\n"
                                  + PATCH + "\ndiff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n"
                                  "+++ b/new.py\n@@ -0,0 +1 @@\n+print(1)")
    assert [(f["filename"], f["status"], f["additions"]) for f in files] == [("app/x.py", "modified", 2), ("new.py", "added", 1)]


# ----------------------------------------------------------------- lifecycle

def _finding(**kw):
    base = {"path": "app/sync.py", "line": 3, "severity": "high", "confidence": "high", "category": "bug",
            "rule": "", "body": "retries forever"}
    return {**base, **kw}


CODE = "def sync():\n    x = 1\n    while True:\n        call()\n    return x\n"


def test_lifecycle_reconcile_states():
    L = gz.lifecycle
    f = L.enrich(_finding(), CODE)
    first = L.reconcile("a/b", 1, "s1", [], [f], lambda p: CODE, set())
    row = first["new"][0]
    assert row["state"] == "open" and row["function"] == "sync"
    # Not re-reported, code unchanged: still open (moved down by two lines).
    moved = "# c\n\n" + CODE
    second = L.reconcile("a/b", 1, "s2", [row], [], lambda p: moved, set())
    assert second["still"][0]["line"] == 5
    # Reworded on the same code: the same finding.
    again = L.enrich(_finding(body="loops without end"), CODE)
    assert L.reconcile("a/b", 1, "s2", [row], [again], lambda p: CODE, set())["still"][0]["id"] == row["id"]
    # Code changed and not reported: fixed.
    fixed = CODE.replace("while True:", "for _ in range(3):")
    assert L.reconcile("a/b", 1, "s3", [row], [], lambda p: fixed, set(), {row["id"]: "Loop now bounded to three attempts"})["fixed"][0]["state"] == "fixed"
    # File removed: outdated.
    assert L.reconcile("a/b", 1, "s3", [row], [], lambda p: None, {"app/sync.py"})["outdated"]
    # Dismissed stays dismissed when found again.
    dismissed = {**row, "state": "dismissed"}
    out = L.reconcile("a/b", 1, "s4", [dismissed], [f], lambda p: CODE, set())
    assert out["suppressed"] and not out["new"]


def test_gate_keeps_real_findings_once():
    lines = {("a.py", 3)}
    checked = {"path": "a.py", "line": 3, "severity": "medium", "confidence": "high", "category": "convention",
               "rule": "No print", "body": "code-checked", "checked": True}
    model_same = {**checked, "body": "print used", "checked": False}
    low = {**checked, "rule": "", "body": "minor", "severity": "low"}
    elsewhere = {**checked, "rule": "", "line": 9, "body": "not on a changed line", "severity": "high"}
    kept = gz.review.gate([checked, model_same, low, elsewhere], lines, "medium", 8)
    assert [f["body"] for f in kept] == ["code-checked"]
    # A verify pass that lowers a finding to low takes it out on the second gate.
    verdicts = [{"index": 0, "verdict": "confirmed", "severity": "low", "reason": "x"}]
    lowered, _ = gz.review.apply_verdicts(kept, kept, verdicts)
    assert gz.review.gate(lowered, lines, "medium", 8) == []


# ------------------------------------------------------------------ commands

@pytest.mark.parametrize("body, verb, arg", [
    ("@gitzoid pause", "pause", ""),
    ("thanks!\n@GitZoid dismiss intentional, VPN only", "dismiss", "intentional, VPN only"),
    ("@gitzoid frobnicate", "help", ""),
    ("@gitzoid", "help", ""),
])
def test_command_parse(body, verb, arg):
    c = gz.commands.parse(body)
    assert (c.verb, c.arg) == (verb, arg)


@pytest.mark.parametrize("body", ["> @gitzoid pause", "```\n@gitzoid pause\n```", "ask gitzoid later", ""])
def test_command_parse_ignores_quotes_code_and_prose(body):
    assert gz.commands.parse(body) is None


# ------------------------------------------------------------------ security

LOCKS = {
    "requirements.txt": ("Flask==3.1.1\nrequests[socks]==2.31.0 ; python_version>'3'\nnumpy>=1\n-e .\n",
                         {("PyPI", "flask", "3.1.1"), ("PyPI", "requests", "2.31.0")}),
    "poetry.lock": ('[[package]]\nname = "PyYAML"\nversion = "5.3.1"\n', {("PyPI", "pyyaml", "5.3.1")}),
    "Pipfile.lock": (json.dumps({"default": {"jinja2": {"version": "==2.11.2"}}, "develop": {}}),
                     {("PyPI", "jinja2", "2.11.2")}),
    "package-lock.json": (json.dumps({"packages": {"": {}, "node_modules/lodash": {"version": "4.17.20"},
                                                   "node_modules/a/node_modules/@x/y": {"version": "1.0.0"}}}),
                          {("npm", "lodash", "4.17.20"), ("npm", "@x/y", "1.0.0")}),
    "web/package-lock.json": (json.dumps({"dependencies": {"left-pad": {"version": "1.0.0", "dependencies":
                                                                        {"nested": {"version": "2.0.0"}}}}}),
                              {("npm", "left-pad", "1.0.0"), ("npm", "nested", "2.0.0")}),
    "yarn.lock": ('"@babel/core@^7.0.0", "@babel/core@^7.1.0":\n  version "7.12.3"\n\nminimist@^1.2.0:\n  version "1.2.5"\n',
                  {("npm", "@babel/core", "7.12.3"), ("npm", "minimist", "1.2.5")}),
    "pnpm-lock.yaml": ("packages:\n  /lodash@4.17.20:\n    resolution: {}\n  '@x/y@1.0.0(react@18.0.0)':\n    resolution: {}\n",
                       {("npm", "lodash", "4.17.20"), ("npm", "@x/y", "1.0.0")}),
    "go.mod": ("module a\n\nrequire (\n\tgolang.org/x/text v0.3.0 // indirect\n\tgithub.com/gin-gonic/gin v1.6.0\n)\n"
               "require github.com/pkg/errors v0.9.1\n",
               {("Go", "golang.org/x/text", "v0.3.0"), ("Go", "github.com/gin-gonic/gin", "v1.6.0"),
                ("Go", "github.com/pkg/errors", "v0.9.1")}),
    "Cargo.lock": ('[[package]]\nname = "time"\nversion = "0.1.43"\nsource = "registry+x"\n\n[[package]]\nname = "mine"\nversion = "0.1.0"\n',
                   {("crates.io", "time", "0.1.43")}),
    "Gemfile.lock": ("GEM\n  remote: https://rubygems.org/\n  specs:\n    nokogiri (1.13.0-x86_64-linux)\n    rack (2.2.3)\n\nPLATFORMS\n",
                     {("RubyGems", "nokogiri", "1.13.0"), ("RubyGems", "rack", "2.2.3")}),
    "composer.lock": (json.dumps({"packages": [{"name": "guzzlehttp/guzzle", "version": "v7.4.0"}]}),
                      {("Packagist", "guzzlehttp/guzzle", "7.4.0")}),
}


@pytest.mark.parametrize("name", sorted(LOCKS))
def test_lockfile_parsers(name):
    text, want = LOCKS[name]
    assert {(d["ecosystem"], d["name"], d["version"]) for d in gz.security.parse(name, text)} == want


def test_declared_packages_tell_direct_from_transitive():
    files = {"package.json": json.dumps({"dependencies": {"express": "^4"}, "devDependencies": {"jest": "29"}}),
             "pyproject.toml": '[project]\ndependencies = ["requests>=2", "PyYAML"]\n',
             "requirements.txt": "certifi==2024.1.1  # via requests\n"}
    d = gz.security.declared(files)
    assert {("npm", "express"), ("npm", "jest"), ("PyPI", "requests"), ("PyPI", "pyyaml")} <= d
    assert ("PyPI", "certifi") not in d  # pip-compile output lists transitive packages
    dep = {"ecosystem": "PyPI", "name": "certifi"}
    assert not gz.security.unused(dep, d, [])
    assert not gz.security.unused({"ecosystem": "npm", "name": "express"}, d, [])
    assert not gz.security.unused({"ecosystem": "npm", "name": "express"}, d, None)


@pytest.mark.parametrize("vector, score", [
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
    ("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:L/I:L/A:N", 6.4),
    ("CVSS:3.0/AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:N/A:N", 3.1),
    ("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N", None),
])
def test_cvss3_scoring(vector, score):
    assert gz.security.cvss3_score(vector) == score


def test_advisory_takes_the_fix_for_this_package():
    record = {"id": "GHSA-x", "aliases": ["CVE-1"], "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
              "affected": [{"package": {"ecosystem": "PyPI", "name": "Other"}, "ranges": [{"events": [{"fixed": "0.1"}]}]},
                           {"package": {"ecosystem": "PyPI", "name": "My_Pkg"},
                            "ranges": [{"events": [{"introduced": "0"}, {"fixed": "1.2.0"}, {"introduced": "2.0"}, {"fixed": "2.0.5"}]}]}]}
    a = gz.security.advisory(record, {"ecosystem": "PyPI", "name": "my-pkg", "version": "2.0.1"})
    assert a["severity"] == "critical" and a["fixed"] == "2.0.5" and a["cves"] == ["CVE-1"]


def test_alias_merge_and_feed_gate():
    a = {"id": "PYSEC-1", "ids": ["PYSEC-1", "GHSA-1"], "cves": [], "severity": "unknown", "fixed": None, "summary": ""}
    b = {"id": "GHSA-1", "ids": ["GHSA-1", "CVE-9"], "cves": ["CVE-9"], "severity": "medium", "fixed": "2", "summary": ""}
    merged = gz.security.merge_aliases([a, b])
    assert len(merged) == 1 and merged[0]["key"] == "GHSA-1" and merged[0]["severity"] == "medium"
    assert not gz.security.passes_gate(merged[0], set(), "high")
    assert gz.security.passes_gate(merged[0], {"CVE-9"}, "high")  # exploited now beats the floor
    assert gz.security.passes_gate({**merged[0], "severity": "unknown"}, set(), "high")


def test_ledger_new_quiet_escalated_resolved(tmp_path):
    store = gz.store.Store(tmp_path)
    item = {"key": "GHSA-1", "severity": "high", "kev": False, "summary": "s", "fixed": "2.0", "url": "u",
            "dep": {"ecosystem": "PyPI", "name": "x", "version": "1.0", "file": "requirements.txt"}}
    assert len(gz.security.reconcile(store, "a/b", [item], "t1")["new"]) == 1
    assert gz.security.reconcile(store, "a/b", [item], "t2") == {"new": [], "escalated": [], "resolved": []}
    assert not gz.security.needs_assessment(store, "a/b", item)
    assert len(gz.security.reconcile(store, "a/b", [{**item, "kev": True}], "t3")["escalated"]) == 1
    assert len(gz.security.reconcile(store, "a/b", [], "t4")["resolved"]) == 1
    assert gz.security.needs_assessment(store, "a/b", item)


def test_imports_found_per_language():
    files = {"app/a.py": "import yaml\nfrom requests import get\n", "web/x.ts": "import { z } from 'zod/v4'\n"}
    assert gz.security.imports(files, {"ecosystem": "PyPI", "name": "pyyaml"}) == ["app/a.py:1: import yaml"]
    assert gz.security.imports(files, {"ecosystem": "npm", "name": "zod"})
    assert gz.security.imports(files, {"ecosystem": "PyPI", "name": "flask"}) == []
    assert gz.security.imports(files, {"ecosystem": "RubyGems", "name": "rack"}) is None


# ------------------------------------------------------------------ checkout

def test_tarball_extraction_keeps_only_safe_regular_files(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name, data=b"x", kind=tarfile.REGTYPE, link=""):
            info = tarfile.TarInfo(name)
            info.type, info.size, info.linkname = kind, len(data) if kind == tarfile.REGTYPE else 0, link
            tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        add("acme-web-abc/app/ok.py", b"print(1)")
        add("acme-web-abc/../../evil.py")
        add("acme-web-abc/link", kind=tarfile.SYMTYPE, link="/etc/passwd")
    out = tmp_path / "co"
    out.mkdir()
    gz.checkout._extract(buf.getvalue(), out)
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [".gitzoid-excluded.json", "app/ok.py"]
    assert not (tmp_path / "evil.py").exists()
    with pytest.raises(FileNotFoundError):
        gz.checkout.read(out, "../co/../evil.py")


# --------------------------------------------------------------------- store

def test_store_once_only_lease_and_caps(tmp_path):
    store = gz.store.Store(tmp_path)
    gz.store.Store(tmp_path)  # migrations run once
    assert store.claim_command("r:1:t", repo="r") == "new"
    assert store.claim_command("r:1:t", repo="r") == "pending"  # stopped before finishing: runs again
    store.finish_command("r:1:t", "pause")
    assert store.claim_command("r:1:t", repo="r") == "done"
    assert store.acquire("repo:r", "a") and not store.acquire("repo:r", "b")
    store.release("repo:r", "a")
    assert store.acquire("repo:r", "b")
    assert store.take("calls", "d1", 2) and store.take("calls", "d1", 2) and not store.take("calls", "d1", 2)


# ------------------------------------------------------------ reports, metrics

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def test_report_periods_and_validation():
    R = gz.reports
    s, e, _ = R.period(R.ReportRequest(period="last_week"), NOW)
    assert (s.date().isoformat(), e.date().isoformat()) == ("2026-10-05", "2026-10-12")
    s, e, _ = R.period(R.ReportRequest(period="last_month"), NOW)
    assert (s.date().isoformat(), e.date().isoformat()) == ("2026-09-01", "2026-10-01")
    s, e, label = R.period(R.ReportRequest(period="custom", start="2026-01-01", end="2026-01-31"), NOW)
    assert e.date().isoformat() == "2026-02-01" and label.startswith("01 Jan")
    for bad in ({"period": "last_n_days"}, {"period": "custom", "start": "2026-02-01", "end": "2026-01-01"},
                {"group": "g", "repos": ["a/b"]}, {"period": "last_n_days", "days": 400}):
        with pytest.raises(ValueError):
            R.ReportRequest(**bad)


def test_report_scope_and_manager_groups():
    s = gz.settings.load({"repos": ["a/b", "a/c"], "digest_groups": {"web": {"repos": ["a/b"]}}})
    R = gz.reports
    assert R.scope(R.ReportRequest(), s) == ("all repos", ["a/b", "a/c"])
    assert R.scope(R.ReportRequest(group="web"), s) == ("web", ["a/b"])
    with pytest.raises(ValueError, match="not GitZoid repos"):
        R.scope(R.ReportRequest(repos=["x/y"]), s)
    assert R.manager_groups({"gitzoid_groups": "web, nope"}, s) == ["web"]
    assert R.manager_groups({}, s) == []


def test_metrics_count_without_ranking():
    merged = [{"repo": "a/b", "number": 1, "title": "x", "author": "ana", "created_at": "2026-10-01T00:00:00Z",
               "merged_at": "2026-10-02T00:00:00Z", "additions": 10, "deletions": 5,
               "reviews": [{"author": "lee", "state": "APPROVED", "submitted_at": "2026-10-01T06:00:00Z"}]},
              {"repo": "a/b", "number": 2, "title": "Revert x", "author": "lee", "created_at": "2026-10-03T00:00:00Z",
               "merged_at": "2026-10-03T03:00:00Z", "additions": 400, "deletions": 0, "reviews": []}]
    open_prs = [{"repo": "a/b", "number": 3, "title": "y", "author": "ana", "draft": False, "url": "",
                 "updated_at": "2026-10-01T00:00:00Z", "requested": ["lee"]}]
    d = gz.metrics.delivery(merged, open_prs, NOW)
    assert d["merged"] == 2 and d["first_review_hours_median"] == 6.0 and d["unreviewed_merges"] == 1 and d["stuck"] == 1
    c = gz.metrics.code(merged, [{"event": "fixed"}])
    assert c["reverts"] == 1 and c["sizes"] == {"S": 1, "L": 1} and c["gitzoid"]["fixed"] == 1
    people = {r["person"]: r for r in gz.metrics.people(merged, open_prs)}
    assert people["lee"]["reviews_given"] == 1 and people["lee"]["waiting_on"] == 1 and people["ana"]["open"] == 1


def test_pages_escape_contributor_text():
    html = gz.render.report_page("delivery", "<script>x</script>", "label",
                                 {"headline": "<img src=x onerror=alert(1)>"},
                                 gz.metrics.delivery([], [], NOW) | {"merged_change": ""})
    assert "<script>x</script>" not in html and "<img src=x" not in html


# -------------------------------------------------------------- GitHub client

def test_github_client_etag_and_dry_run(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("if-none-match")))
        if request.url.path.endswith("/pulls") and request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[{"number": 7, "title": "t", "user": {"login": "ana"},
                                              "head": {"sha": "abc"}, "base": {"ref": "main"}}],
                                  headers={"etag": '"v1"'})
        return httpx.Response(500)

    store = gz.store.Store(tmp_path)
    gh = gz.github.GitHub("t", store=store, dry_run=True, transport=httpx.MockTransport(handler))
    assert gh.open_pulls("a/b")[0]["number"] == 7
    assert gh.open_pulls("a/b")[0]["head_sha"] == "abc"  # served from the cache on 304
    gh.set_status("a/b", "abc", "success", "ok")
    gh.reply("a/b", 7, {"kind": "issue", "id": 1}, "hi")
    assert [m["summary"] for m in gh.outbox] == ["status success", "reply"]
    assert all(method == "GET" for method, *_ in seen)


def test_github_rate_limit_reports_reset():
    def handler(request):
        return httpx.Response(403, headers={"retry-after": "3600"})
    gh = gz.github.GitHub("t", transport=httpx.MockTransport(handler))
    with pytest.raises(gz.github.RateLimited, match="resets in 3600"):
        gh.pull("a/b", 1)
