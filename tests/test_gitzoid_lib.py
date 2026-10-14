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


def test_review_budget_reads_code_first_and_skips_build_output():
    def added(text, n=1):
        return {"patch": "@@ -0,0 +1,%d @@\n" % n + "\n".join("+" + text for _ in range(n))}
    files = [{"filename": "CHANGELOG.md", **added("Notes " * 20, 40)},
             {"filename": "docs/guide.md", **added("Words " * 20, 40)},
             {"filename": "package-lock.json", **added('"x": "1"', 40)},
             {"filename": "tests/test_auth.py", **added("assert login()", 5)},
             {"filename": "portal_dist/assets/App-C_uA2yjw.js", **added("var a=1;" * 400, 60)},
             {"filename": "web/app.min.js", **added("x" * 300, 80)},
             {"filename": "web/tiny.min.js", **added("fetch('/steal')", 1)},  # small: read whatever its name
             {"filename": "app/auth.py", **added("def login(): return True", 5)},
             {"filename": "app/gen_pb2.py", **added("# Code generated. DO NOT EDIT.", 1)}]
    built: list[str] = []
    text, left = gz.diff.format_diff(files, 2_000, built=built)
    assert built == ["portal_dist/assets/App-C_uA2yjw.js", "web/app.min.js"]   # small files are always read
    assert "web/tiny.min.js" in text
    assert text.index("app/auth.py") < text.index("tests/test_auth.py")         # code before tests
    assert "app/gen_pb2.py" in text and left == ["CHANGELOG.md", "docs/guide.md"]  # too big for what was left
    assert text.index("package-lock.json") > text.index("tests/test_auth.py")   # lockfiles last
    assert gz.settings.load({}).review.max_diff_chars == 200_000
    assert gz.diff.priority({"filename": "docs/conf.py"}) == 0 and gz.diff.priority({"filename": "requirements.txt"}) == 1
    # Only the file's own first lines count as a "generated" header: a big edit lower down is still read.
    edit = {"filename": "app/models.py", "patch": "@@ -400,1 +400,401 @@\n x = 1\n+# do not edit by hand\n"
                                                    + "".join(f"+value_{i} = {'1' * 60}\n" for i in range(400))}
    header = {"filename": "app/models_pb2.py", "patch": "@@ -1,2 +1,402 @@\n # Generated code. DO NOT EDIT.\n x = 1\n"
                                                         + "".join(f"+value_{i} = {'1' * 60}\n" for i in range(400))}
    assert not gz.diff.is_built(edit) and gz.diff.is_built(header)


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



def _view(**over):
    f = {"id": "f" * 16, "path": "a.py", "line": 3, "severity": "high", "category": "bug", "rule": "No print",
         "body": "This breaks the team rule No print. Printed lines never reach the log", "suggestion": ""}
    view = {"open": [f], "fixed": [], "decided": [], "summary": "Adds a sync", "suggestions": ["Log the count"],
            "risk": "high", "sha": "abc1234def", "reviewed_files": 2, "total_files": 2, "left_out": [], "ignored": [],
            "notes": ["ruff: 3 result(s) on added lines", "CI unavailable"], "incremental": False, "truncated": False,
            "rule_files": ["review.md"], "model": "m", "dropped": [], "paused": None, "verdict": "needs_changes"}
    return {**view, **over}


def test_pr_comment_keeps_the_gitzoid_layout():
    pr = {"repo": "o/r", "number": 7, "head_ref": "b"}
    body = gz.review.summary(_view(), pr, True)
    assert body.startswith("<!-- gitzoid:summary -->\n### GitZoid review · Needs changes\n\n"
                           "**Merge risk: high** · commit `abc1234` · 2 of 2 files reviewed\n")
    assert "## 📝 Summary\n- Adds a sync." in body and "## ⚠️ Potential Issues (1)" in body
    # Severity as a word, the location, one full stop, and no repeat of a rule the text already names.
    assert "- _high_ `a.py:3` — This breaks the team rule No print. Printed lines never reach the log.\n" in body
    assert body.index("<summary>Run details</summary>") > body.index("## ⚠️ Potential Issues")
    # Analyzer and CI notes are run details, not a partial review; missing files are.
    assert "Partial review" not in body
    partial = gz.review.summary(_view(left_out=["big.py"], total_files=3), pr, False)
    assert "> ⚠️ **Partial review.** 1 of 3 files were not reviewed." in partial and "`big.py`" in partial
    assert "@gitzoid" not in partial
    clean = gz.review.clean_summary(_view(open=[], suggestions=[], verdict="looks_good"))
    assert "### GitZoid review · Looks good" in clean and "## ⚠️" not in clean and "Fix all" not in clean
    partly = gz.review.clean_summary(_view(open=[], verdict="looks_good", left_out=["big.py"], total_files=3))
    assert "### GitZoid review · No issues in the reviewed files" in partly


def test_one_click_suggestion_only_when_confirmed_and_fenced():
    pr = {"repo": "o/r", "number": 7}
    f = {**_view()["open"][0], "k": 0, "suggestion": "    log.info(\"```x\")"}
    kept, _ = gz.review.apply_verdicts([f], [f], [{"index": 0, "verdict": "uncertain", "severity": "high"}])
    assert kept[0]["suggestion"] == ""
    kept, _ = gz.review.apply_verdicts([f], [f], [])  # the second pass skipped it
    assert kept[0]["suggestion"] == ""
    kept, _ = gz.review.apply_verdicts([f], [f], [{"index": 0, "verdict": "confirmed", "severity": "high"}])
    inline = gz.review.inline_body(kept[0], pr, True)
    assert inline.startswith("**🐛 Bug** · high severity\n\n")
    assert "````suggestion\n    log.info(\"```x\")\n````" in inline  # its own backticks cannot close the block
    assert "```suggestion" not in gz.review.inline_body({**f, "suggestion": "x = 1\ny = 2"}, pr, True)
    assert "```suggestion" not in gz.review.inline_body({**f, "line": None, "suggestion": "x"}, pr, True)


def test_dependency_advisories_are_one_finding_per_package():
    dep = {"ecosystem": "PyPI", "name": "gunicorn", "version": "21.2.0"}
    advs = [{"key": "GHSA-1", "cves": ["CVE-1"], "severity": "high", "summary": "Smuggling.", "fixed": "22.0.0", "dep": dep},
            {"key": "GHSA-2", "cves": [], "severity": "unknown", "summary": "Bypass", "fixed": "21.3.1", "dep": dep}]
    lines = [(1, "gunicorn-extras==21.2.0"), (2, "gunicorn==21.2.0  # py3.8")]
    [group] = gz.security.by_package(advs)
    f = gz.security.pr_finding("requirements.txt", group, set(), lines)
    assert f["line"] == 2 and f["severity"] == "high" and f["checked"]
    assert f["body"] == ("gunicorn 21.2.0 has 2 known vulnerabilities, the worst high: GHSA-1 (Smuggling), "
                         "GHSA-2 (Bypass). Upgrade to 22.0.0 or later.")
    assert f["suggestion"] == "gunicorn==22.0.0  # py3.8"
    unrated = gz.security.pr_finding("requirements.txt", group[1:], {"CVE-1"}, lines)
    assert unrated["severity"] == "medium" and "no severity rating" in unrated["body"]
    unfixed = gz.security.pr_finding("requirements.txt", [{**advs[0], "fixed": None}], {"CVE-1"}, lines)
    assert "actively exploited" in unfixed["body"] and "No fixed version" in unfixed["body"] and not unfixed["suggestion"]
    # The model restating the same advisory on that line is dropped by the gate.
    model = {"path": "requirements.txt", "line": 2, "severity": "high", "confidence": "high", "category": "security",
             "body": "gunicorn 21.2 has request smuggling", "rule": "review.md"}
    kept = gz.review.gate([f, model], {("requirements.txt", 2)}, "high", 8)
    assert [k["body"] for k in kept] == [f["body"]]


def test_audit_rewording_is_the_same_finding_and_verdicts_apply(tmp_path):
    st = gz.store.Store(tmp_path)
    f = {"path": "app/a.py", "line": 10, "kind": "authorization", "severity": "high", "body": "No check.",
         "evidence": "def a():"}
    assert len(gz.audit.record(st, "o/r", [f], ["app/a.py"], tmp_path, "t1")) == 1
    seen: set = set()
    moved = {**f, "line": 25, "body": "Anyone can call a.", "evidence": "return a_rows()"}
    assert gz.audit.record(st, "o/r", [moved], ["app/a.py"], tmp_path, "t2", seen=seen) == []
    [row] = st.ledger_rows(["o/r"], kind="audit")
    assert row["key"] in seen and row["line"] == 25 and row["last_seen"] == "t2"
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "b.py").write_text("def list_orders():\n    return rows()\n\n\ndef export_orders():\n"
                                         "    return all_rows()\n")
    one = {**f, "path": "app/b.py", "line": 2, "evidence": "return rows()"}
    two = {**f, "path": "app/b.py", "line": 6, "evidence": "return all_rows()"}
    assert len(gz.audit.record(st, "o/r", [one], ["app/b.py"], tmp_path, "t2")) == 1
    assert len(gz.audit.record(st, "o/r", [two], ["app/b.py"], tmp_path, "t2")) == 1  # another handler: news
    reworded = {**one, "evidence": "rows()", "body": "Anyone lists orders."}
    assert gz.audit.record(st, "o/r", [reworded], ["app/b.py"], tmp_path, "t2") == []  # same function: not news
    far = {**f, "line": 80, "evidence": "def b():"}
    other = {**f, "kind": "injection", "evidence": "sql"}
    assert len(gz.audit.record(st, "o/r", [far, other], ["app/a.py"], tmp_path, "t3")) == 2
    kept, dropped = gz.audit.apply_verdicts([f, far], [{"index": 0, "verdict": "refuted", "reason": "public"},
                                                      {"index": 1, "verdict": "confirmed", "severity": "low"}])
    assert [d["line"] for d in dropped] == [10] and kept[0]["severity"] == "low"
    assert gz.audit.gate(kept, {"app/a.py"}) == []  # lowered to low: not alerted
    prompt = gz.audit.prompt("o/r", "", [("app/a.py", "x = 1", 0)], [{"path": "app/a.py", "line": 10,
                                                                       "kind": "authorization", "title": "No check."}])
    assert "<already_reported" in prompt and "app/a.py:10 authorization: No check." in prompt
    # Progress survives a new commit only for windows whose content did not change.
    (tmp_path / "app" / "c.py").write_text("x = 1\n")
    done = gz.audit.windows(tmp_path, ["app/b.py", "app/c.py"], [])
    (tmp_path / "app" / "c.py").write_text("x = 2\n")
    assert [gz.audit.unit(u)[0] for u in gz.audit.windows(tmp_path, ["app/b.py", "app/c.py"], done)] == ["app/c.py"]

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


def test_dependency_files_in_test_sample_and_excluded_folders_are_skipped(tmp_path):
    for rel in ("requirements.txt", "tests/requirements.txt", "templates/x/raw_data/sample/pulls/1/requirements.txt",
                "examples/demo/package-lock.json", "legacy/requirements.txt", "app/requirements.txt"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x==1.0\n")
    skipped = []
    files, complete = gz.security.manifest_files(tmp_path, exclude=["legacy/"], skipped=skipped)
    assert set(files) == {"requirements.txt", "app/requirements.txt"} and complete
    assert len(skipped) == 4
    store = gz.store.Store(tmp_path)
    item = {"key": "GHSA-1", "severity": "high", "kev": False, "summary": "s", "fixed": "2.0", "url": "u",
            "dep": {"ecosystem": "PyPI", "name": "x", "version": "1.0", "file": "tests/requirements.txt"}}
    gz.security.reconcile(store, "a/b", [item], "t1")
    out = gz.security.reconcile(store, "a/b", [], "t2", skipped_files={"tests/requirements.txt"})
    assert out["resolved"] == [] and store.ledger_rows(["a/b"], state="skipped")  # skipped, not fixed
    assert len(gz.security.reconcile(store, "a/b", [item], "t3")["new"]) == 1     # checked again: alerted again


def test_audit_skips_minified_generated_and_excluded_files(tmp_path):
    files = {"app/auth.py": "def login():\n    return True\n",
             "portal_dist/assets/App-C_uA2yjw.js": "var a=1;" * 5000,
             "api/gen_pb2.py": "# Generated by the protocol buffer compiler.  DO NOT EDIT!\n" + "x = 1\n" * 4000,
             "app/admin.py": "# Code generated. DO NOT EDIT.\ndef backdoor():\n    pass\n",  # small: still audited
             "app/samples/demo.py": "print(1)\n", "old/auth.py": "def f():\n    pass\n"}
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    skipped = []
    chosen = gz.audit.select(tmp_path, None, [], [], 100, exclude=["old/**"], skipped=skipped)
    assert sorted(chosen) == ["app/admin.py", "app/auth.py"]
    assert set(skipped) == {"portal_dist/assets/App-C_uA2yjw.js", "api/gen_pb2.py", "old/auth.py"}


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


def test_email_external_text_is_literal_and_coverage_survives_trimming():
    from hubzoid.email_delivery import MAX_LIST_HTML_TEXT, _html_text, _safe_html
    long = "word " * 150
    subject, body = gz.mail.report_email(
        "code", "team", "01 Sep 2026 to 30 Sep 2026",
        {"headline": "A & B, see `retry()` in acme/web#7 and other/repo#8",
         "highlights": ["[Click](https://attacker.invalid) <img src=x>"] + [long] * 6, "watch": [long] * 3},
        {"merged": 2, "additions": 10, "deletions": 1, "files_changed": 3},
        ["acme/web#1: 2 file patches unavailable", "acme/web#2: Diff unavailable",
         "GitHub search limit reached; merged PR totals may be incomplete"],
        link="https://hub.example/portal/latest/r", repos={"acme/web"})
    safe = _safe_html(body)
    assert subject == "GitZoid Team Code Report: 01 Sep 2026 to 30 Sep 2026" and "GitZoid Team Code Report</h1>" in safe
    assert "<img" not in safe and 'href="https://attacker.invalid"' not in safe and "[Click]" in safe
    assert "A &amp; B" in safe and "retry()</code>" in safe
    assert 'href="https://github.com/acme/web/pull/7"' in safe and "other/repo#8</a>" not in safe  # only GitZoid's repos
    assert "part of the code in 2 pull requests" in safe and "search limit reached" in safe  # kept when trimmed
    assert 'href="https://hub.example/portal/latest/r"' in safe and len(_html_text(body)) <= MAX_LIST_HTML_TEXT


def test_security_email_groups_packages_and_puts_code_first():
    dep = {"kind": "dependency", "repo": "acme/web", "package": "django", "version": "4.2", "severity": "high",
           "impact": "Request smuggling is possible.", "fix": "Upgrade to 4.2.9.", "path": "requirements.txt",
           "url": "https://osv.dev/vulnerability/GHSA-1"}
    changes = {"new": [{**dep, "advisory": "GHSA-1", "key": "k1"}, {**dep, "advisory": "GHSA-2", "key": "k2", "kev": 1},
                       {"kind": "audit", "repo": "acme/web", "advisory": "authorization", "severity": "high",
                        "title": "Admin export has no permission check.", "impact": "Anyone signed in can export.",
                        "path": "app/admin.py", "line": 12, "key": "k3"}],
               "escalated": [], "resolved": [{"key": "old"}]}
    subject, body = gz.mail.security_email(changes, [{"repo": "acme/web", "check_name": "lockfiles",
                                                      "last_error": "yarn.lock unreadable"}], 1, "https://h/s")
    assert subject == "GitZoid Security: 2 issues found · 1 check could not run"
    assert body.index("Checks that could not run") < body.index("Code and access issues") < body.index("Vulnerable dependencies")
    assert "2 known vulnerabilities" in body and "actively exploited" in body and body.count("django 4.2") == 1
    assert "app/admin.py:12" in body and "1 resolved since the last check" in body and ".." not in body
    assert gz.mail.sentence("Fixed it.", "Then this") == "Fixed it. Then this."


def test_digest_email_matches_subject_and_shows_security_state():
    d = {"headline": "One thing merged.", "for_leadership": ["Sign-in is faster."], "technical": [], "areas": [],
         "watch": [], "poem": ""}
    numbers = {"merged": 3, "contributors": 2, "open": 1, "stuck": 0}
    never = {"open": [], "coverage": []}
    subject, body = gz.mail.digest_email("team", "06 Oct to 12 Oct 2026", "12 Oct 2026", d, numbers, never)
    assert subject == "GitZoid Team Digest: Week of 12 Oct 2026" and "GitZoid Team Digest</h1>" in body
    assert "Security</h2>" not in body  # never checked: no all-clear
    clean = {"open": [], "coverage": [{"repo": "a/b", "check_name": "dependencies", "last_ok": "t"}],
             "audit_in_progress": ["a/b"]}
    _, body = gz.mail.digest_email("all", "x", "y", d, numbers, clean, implicit=True)
    assert "GitZoid Digest</h1>" in body and "No open security issues" in body and "still in progress" in body
