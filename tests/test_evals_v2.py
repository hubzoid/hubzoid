"""Console eval reads: account-owned details stay private; the index pages runs."""
from __future__ import annotations

import json

from hubzoid.evals import cases, runner
from hubzoid.evals.assertions import Check
from hubzoid.evals.report import save
from hubzoid.evals.results import CaseResult, SuiteResult, ToolCallRecord
from tests.test_access_service import dep  # noqa: F401 — fixture used by api
from tests.test_evals import FakeRuntime
from tests.test_portal_accounts import DELEGATE, ROOT, api  # noqa: F401 — fixture


def test_eval_api_private_details_and_index_paging(api):
    hub = api.hub_dir
    cases_dir = hub / "evals"
    cases_dir.mkdir()
    (cases_dir / "private.md").write_text(
        "---\nrun_as: dele@x.org\ncontains: [private criterion]\n---\nPRIVATE PROMPT"
    )
    (cases_dir / "public.md").write_text("PUBLIC PROMPT")
    first = save(hub, SuiteResult(hub="finance", cases=[
        CaseResult(name="private", run_as=DELEGATE, response="PRIVATE ANSWER",
                   checks=[Check("contains", False, "PRIVATE CHECK DETAIL")],
                   tools=[ToolCallRecord(name="lookup", args={"api_key": "SECRET", "query": "private"})]),
        CaseResult(name="public", response="PUBLIC ANSWER"),
    ]), stamp="20261001_120000")
    save(hub, SuiteResult(hub="finance", cases=[CaseResult(name="public", response="new")]),
         stamp="20261001_130000")

    overview = api.as_(ROOT).get("/portal/api/evals", params={"hub": "finance"})
    assert overview.status_code == 200, overview.text
    private = next(c for c in overview.json()["cases"] if c["name"] == "private")
    assert private["prompt"] == "" and private["checks"] == [] and private["run_as"] is None
    assert private["latest"]["passed"] is False
    # Scores are counts and pass/fail results, never a private case's content.
    score = overview.json()["score"]
    assert score["latest"]["passed"] == 1 and score["latest"]["total"] == 1
    assert [(t["passed"], t["total"]) for t in score["trend"]] == [(1, 2), (1, 1)]
    assert score["failing"] == ["private"] and score["never_run"] == []
    assert private["history"] == [False]
    public = next(c for c in overview.json()["cases"] if c["name"] == "public")
    assert public["history"] == [True, True]
    card = api.as_(ROOT).get("/portal/api/evals/summary", params={"hub": "finance"}).json()
    assert card["runs"] == 2 and card["cases"] == 2
    assert (card["latest"]["passed"], card["latest"]["total"]) == (1, 1)
    assert "PRIVATE" not in overview.text.replace("private", "")

    params = {"hub": "finance", "limit": 1}
    newest = api.as_(ROOT).get("/portal/api/evals/runs", params={**params, "offset": 0})
    older = api.as_(ROOT).get("/portal/api/evals/runs", params={**params, "offset": 1})
    assert newest.status_code == older.status_code == 200
    assert newest.json()["total"] == older.json()["total"] == 2
    assert newest.json()["runs"][0]["stamp"] > older.json()["runs"][0]["stamp"] == first.stem
    assert "cases" not in older.json()["runs"][0]

    url = f"/portal/api/evals/runs/{first.stem}"
    hidden = api.as_(ROOT).get(url, params={"hub": "finance"})
    assert hidden.status_code == 200, hidden.text
    private_result = next(c for c in hidden.json()["cases"] if c["name"] == "private")
    assert private_result["private"] is True and private_result["passed"] is False
    for secret in ("PRIVATE PROMPT", "PRIVATE ANSWER", "PRIVATE CHECK DETAIL", "SECRET"):
        assert secret not in hidden.text
    assert next(c for c in hidden.json()["cases"] if c["name"] == "public")["answer"] == "PUBLIC ANSWER"

    visible = api.as_(DELEGATE).get(url, params={"hub": "finance"})
    assert visible.status_code == 200 and "PRIVATE ANSWER" in visible.text
    assert "SECRET" not in visible.text and "[redacted]" in visible.text
    assert api.as_("ann@x.org").get(url, params={"hub": "finance"}).status_code == 403


def test_schema_one_result_remains_readable_in_console(api):
    hub = api.hub_dir
    folder = hub / ".hubzoid" / "evals"
    folder.mkdir(parents=True)
    old = SuiteResult(hub="finance", cases=[CaseResult(
        name="old", response="old answer", tool_calls=["read_knowledge"])]).to_dict()
    old.pop("schema")
    old.pop("trigger")
    old.pop("run_as")
    for case in old["cases"]:
        for field in ("tools", "turns", "run_as"):
            case.pop(field)
    (folder / "20260901_120000.json").write_text(json.dumps(old))

    summary = api.as_(ROOT).get("/portal/api/evals/runs", params={"hub": "finance"})
    detail = api.as_(ROOT).get("/portal/api/evals/runs/20260901_120000", params={"hub": "finance"})
    assert summary.status_code == detail.status_code == 200
    assert summary.json()["runs"][0]["schema"] == 1
    assert detail.json()["trigger"] is None
    assert detail.json()["cases"][0]["tools"] == [dict(
        name="read_knowledge", args=None, ok=None, error=None,
        duration_ms=None, preview=None, turn=1)]


def test_multi_turn_timeout_is_a_whole_case_deadline(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    folder = hub / "evals"
    folder.mkdir(parents=True)
    (folder / "slow.md").write_text(
        "---\ntimeout: 1\n---\n## Turn 1\nFirst\n\n## Turn 2\nSecond"
    )
    runtime = FakeRuntime(delay=0.7)
    monkeypatch.setattr("hubzoid.runtime.build", lambda *_a, **_k: runtime)
    suite = runner.run_suite(hub, cases.discover(hub))
    assert not suite.ok
    assert "timed out after 1s" in suite.cases[0].error
    assert "turn 2" in suite.cases[0].error


def test_gateway_eval_reads_redirect_to_the_registered_slug(api, tmp_path):
    """Another hub's results are served by its own bridge, at the slug the
    gateway registered for it (not a slug guessed from its folder name)."""
    manifest = tmp_path / "gateway" / "deployment.json"
    data = json.loads(manifest.read_text())
    for hub in data["hubs"]:
        if hub["key"] == "ops":
            hub["slug"] = "ops-2"
    manifest.write_text(json.dumps(data))
    reply = api.as_(ROOT).get("/portal/api/evals/runs", params={"hub": "ops"},
                              follow_redirects=False)
    assert reply.status_code in (302, 307)
    assert reply.headers["location"].startswith("/b/ops-2/portal/api/evals/runs?")
