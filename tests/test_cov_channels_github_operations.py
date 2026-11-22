"""GitHub operation validation, bounded reads and fixed provider write targets."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from hubzoid.channels.errors import ChannelError
from hubzoid.channels.github import operations as op
from hubzoid.work_items.errors import Rejected

FULL = "example-co/widget"
SHA = "a" * 40


class Web:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer, request=httpx.Request(method, "https://api.example.test" + path))


@pytest.mark.parametrize("fn,value,name", [
    (op._int, True, "number"), (op._int, 0, "number"), (op._int, "1", "number"),
    (op._str, " ", "title"), (op._str, 3, "title"), (op._str, "a" * 65537, "title"),
    (op._sha, "../path", "sha"), (op._sha, None, "sha"),
    (op._ref, "../main", "ref"), (op._ref, "/main", "ref"), (op._ref, "main/", "ref"),
])
def test_invalid_scalar_arguments_are_refused(fn, value, name):
    with pytest.raises(ChannelError) as exc:
        fn(value, name)
    assert exc.value.status == 422
    assert exc.value.code == "validation_failed"
    assert name in exc.value.message


@pytest.mark.parametrize("args,message", [({"page_size": 101}, "page_size"),
                                          ({"page_size": 0}, "page_size"),
                                          ({"cursor": "oops"}, "cursor"),
                                          ({"cursor": []}, "cursor")])
def test_invalid_pagination(args, message):
    # An empty cursor is the first page; a nonempty malformed one is refused.
    if args.get("cursor") == []:
        args = {"cursor": [1]}
    with pytest.raises(ChannelError, match=message):
        op._page(args)


@pytest.mark.parametrize("name,path,args,extra", [
    ("list_branches", "/branches", {}, {}), ("list_labels", "/labels", {}, {}),
    ("list_commits", "/commits", {"ref": "topic/x"}, {"sha": "topic/x"}),
    ("list_issues", "/issues", {"state": "closed"}, {"state": "closed"}),
    ("list_pull_requests", "/pulls", {}, {"state": "open"}),
    ("list_issue_comments", "/issues/4/comments", {"number": 4}, {}),
    ("list_pull_request_files", "/pulls/4/files", {"number": 4}, {}),
    ("list_pull_request_reviews", "/pulls/4/reviews", {"number": 4}, {}),
    ("list_check_runs", f"/commits/{SHA}/check-runs", {"head_sha": SHA}, {}),
])
def test_read_pages_preserve_items_and_next_cursor(name, path, args, extra):
    web = Web([{"id": 1}, {"id": 2}])
    result = getattr(op, name)(None, web, FULL, {**args, "page_size": 2, "cursor": "3"})
    assert result == {"data": [{"id": 1}, {"id": 2}], "truncated": False, "next_cursor": "4"}
    assert web.calls == [("GET", f"/repos/{FULL}{path}", {"params": {"per_page": 2, "page": 3, **extra}})]


@pytest.mark.parametrize("envelope", ["items", "check_runs", "workflow_runs"])
def test_enveloped_empty_read_has_no_next_cursor(envelope):
    assert op._list(Web({envelope: []}), "/items", {}) == {
        "data": [], "truncated": False, "next_cursor": None}


@pytest.mark.parametrize("name", ["list_issues", "list_pull_requests"])
def test_invalid_issue_state(name):
    with pytest.raises(ChannelError, match="state is open, closed or all"):
        getattr(op, name)(None, Web(), FULL, {"state": "deleted"})


@pytest.mark.parametrize("name", ["search_code", "search_issues"])
def test_search_is_scoped_and_reports_incomplete_results(name):
    web = Web({"items": [{"path": "src.py"}], "incomplete_results": True})
    assert getattr(op, name)(None, web, FULL, {"query": "fix"}) == {
        "data": [{"path": "src.py"}], "truncated": True, "next_cursor": None}
    assert web.calls[0][2]["params"]["q"] == f"fix repo:{FULL}"


@pytest.mark.parametrize("query", ["repo:other/x fix", "ORG:other fix", "user:other"])
def test_search_cannot_escape_repository(query):
    with pytest.raises(ChannelError, match="limited to this repository"):
        op.search_code(None, Web(), FULL, {"query": query})


@pytest.mark.parametrize("path", ["/file", "../file", "a//b", "a/./b", "a/../b"])
def test_file_path_cannot_escape_repository(path):
    with pytest.raises(ChannelError, match="relative to the repository root"):
        op.get_file_content(None, Web(), FULL, {"ref": "main", "path": path})


def test_file_content_directory_tree_commit_and_compare_bounds():
    web = Web([{"path": "dir/a", "type": "file", "secret": "hidden"}])
    assert op.get_file_content(None, web, FULL, {"ref": "main", "path": "dir"}) == {
        "data": [{"path": "dir/a", "type": "file"}]}
    web = Web({"path": "a b", "sha": SHA, "encoding": "base64", "size": 2, "content": "eA=="})
    assert op.get_file_content(None, web, FULL, {"ref": "main", "path": "a b"})["source_sha"] == SHA
    assert web.calls[0][1].endswith("/contents/a%20b")
    web = Web({"sha": SHA, "tree": [{"path": "a", "type": "blob"}] * 5001})
    tree = op.get_repository_tree(None, web, FULL, {"ref": "topic/x"})
    assert len(tree["data"]["tree"]) == 5000 and tree["truncated"]
    assert web.calls[0][1].endswith("/trees/topic%2Fx")
    assert op.get_commit(None, Web({"sha": SHA}), FULL, {"sha": SHA}) == {"data": {"sha": SHA}, "source_sha": SHA}
    result = op.compare_commits(None, Web({"files": list(range(51))}), FULL, {"base": "main", "head": "topic/x"})
    assert result == {"data": {"files": list(range(50))}, "truncated": True}
    assert op.get_repository(None, Web({"id": 2001}), FULL, {}) == {"data": {"id": 2001}}


def test_pr_context_bounds_generated_files_and_utf8_patch_budget():
    conn = SimpleNamespace(settings=SimpleNamespace(excluded_diff_files=["secret/*"]))
    files = [{"filename": "poetry.lock"}, {"filename": "secret/key"},
             {"filename": "big.py", "patch": "é" * 10001},
             *[{"filename": f"src/{n}.py", "patch": "+ok"} for n in range(51)]]
    web = Web({"number": 4, "body": "x" * 4001, "head": {"sha": SHA}, "base": {"sha": "b" * 40},
               "changed_files": 99}, files, [{"id": 5}])
    result = op.get_pull_request_context(conn, web, FULL, {"number": 4})
    data = result["data"]
    assert result["truncated"] and result["source_sha"] == SHA
    assert len(data["files"]) == 50 and data["files"][0]["patch"] == ""
    assert data["omissions"] == {"generated": ["poetry.lock", "secret/key"],
                                  "patch_too_large": ["big.py"], "over_limit": ["src/49.py", "src/50.py"]}
    assert data["not_listed_count"] == 45 and len(data["pull_request"]["body"]) == 4000
    assert data["reviews"] == [{"id": 5}]


def test_pr_context_refuses_moved_head_before_reading_files():
    web = Web({"head": {"sha": SHA}})
    with pytest.raises(ChannelError) as exc:
        op.get_pull_request_context(None, web, FULL, {"number": 4, "head_sha": "b" * 40})
    assert exc.value.code == "head_changed" and exc.value.status == 409
    assert len(web.calls) == 1


@pytest.mark.parametrize("name,args,method,path,body", [
    ("create_issue", {"title": "Bug", "labels": ["bug"], "assignees": ["ana"]}, "POST", "/issues",
     {"title": "Bug", "body": "", "labels": ["bug"], "assignees": ["ana"]}),
    ("update_issue", {"number": 4, "state": "closed"}, "PATCH", "/issues/4", {"state": "closed"}),
    ("close_issue", {"number": 4}, "PATCH", "/issues/4", {"state": "closed"}),
    ("add_issue_comment", {"number": 4, "body": "Hello"}, "POST", "/issues/4/comments", {"body": "Hello"}),
    ("add_pull_request_comment", {"number": 4, "body": "Hello"}, "POST", "/issues/4/comments", {"body": "Hello"}),
    ("add_pull_request_comment", {"number": 4, "body": "Hello", "review_root_comment_id": 7},
     "POST", "/pulls/4/comments/7/replies", {"body": "Hello"}),
    ("add_labels", {"number": 4, "labels": ["bug"]}, "POST", "/issues/4/labels", {"labels": ["bug"]}),
    ("remove_label", {"number": 4, "label": "needs/review"}, "DELETE", "/issues/4/labels/needs%2Freview", None),
    ("add_assignees", {"number": 4, "assignees": ["ana"]}, "POST", "/issues/4/assignees", {"assignees": ["ana"]}),
    ("remove_assignees", {"number": 4, "assignees": ["ana"]}, "DELETE", "/issues/4/assignees", {"assignees": ["ana"]}),
    ("request_reviewers", {"number": 4, "reviewers": ["ana"]}, "POST", "/pulls/4/requested_reviewers", {"reviewers": ["ana"]}),
])
def test_write_wrappers_only_send_declared_target_and_body(name, args, method, path, body):
    web = Web({"id": 9})
    assert getattr(op, name)(None, web, FULL, args) == {"id": 9}
    assert web.calls == [(method, f"/repos/{FULL}{path}", {} if body is None else {"json": body})]


@pytest.mark.parametrize("name,args,message", [
    ("create_issue", {"title": "ok", "labels": [""]}, "labels"),
    ("create_issue", {"title": "ok", "assignees": ["a"] * 11}, "assignees"),
    ("update_issue", {"number": 1}, "Change at least one"),
    ("update_issue", {"number": 1, "state": "deleted"}, "state is open or closed"),
    ("close_issue", {"number": 1, "state": "deleted"}, "state is closed or open"),
    ("update_pull_request", {"number": 1}, "Change at least one"),
    ("update_pull_request", {"number": 1, "draft": "false"}, "draft is true or false"),
    ("update_pull_request", {"number": 1, "state": "deleted"}, "state is open or closed"),
])
def test_invalid_writes_make_no_provider_call(name, args, message):
    web = Web()
    with pytest.raises(ChannelError, match=message):
        getattr(op, name)(None, web, FULL, args)
    assert web.calls == []


@pytest.mark.parametrize("draft,mutation", [(True, "convertPullRequestToDraft"), (False, "markPullRequestReadyForReview")])
def test_pr_draft_change_uses_node_id_and_graphql(draft, mutation):
    web = Web({"node_id": "node4"}, {"data": {mutation: {"pullRequest": {"isDraft": draft}}}})
    assert op.update_pull_request(None, web, FULL, {"number": 4, "base": "main", "draft": draft}) == {
        "node_id": "node4", "draft": draft}
    assert web.calls[1][1] == "/graphql"
    assert mutation in web.calls[1][2]["json"]["query"]
    assert web.calls[1][2]["json"]["variables"] == {"id": "node4"}


def test_pr_draft_change_provider_refusal():
    web = Web({"node_id": "node4"}, {"errors": [{"message": "denied"}]})
    with pytest.raises(ChannelError) as exc:
        op.update_pull_request(None, web, FULL, {"number": 4, "draft": False})
    assert exc.value.code == "provider_rejected"


def test_pr_creation_checks_reviewed_branch_sha_and_always_drafts():
    args = {"title": "Fix", "base": "main", "head": "topic", "head_sha": SHA[:7]}
    web = Web({"commit": {"sha": SHA}}, {"number": 4})
    assert op.create_pull_request(None, web, FULL, args) == {"number": 4}
    assert web.calls[1][2]["json"]["draft"] is True
    web = Web({"commit": {"sha": "b" * 40}})
    with pytest.raises(Rejected, match="No pull request was opened"):
        op.create_pull_request(None, web, FULL, args)
    assert len(web.calls) == 1
    assert op.find_pull_request(None, Web([]), FULL, "topic") is None
    assert op.find_pull_request(None, Web([{"number": 4}]), FULL, "topic") == {"number": 4}


def test_ci_failure_context_reads_redirect_without_credentials_and_bounds_logs(monkeypatch):
    monkeypatch.setattr(op, "_failed_statuses", lambda *a: ([], "status read unavailable"))
    fetched = []
    def get(url, **kw):
        fetched.append((url, kw))
        return httpx.Response(200, text="\n".join(str(i) for i in range(250)))
    monkeypatch.setattr(httpx, "get", get)
    runs = [{"id": n, "name": f"job{n}", "conclusion": "failure", "app": {"slug": "github-actions"}}
            for n in range(1, 13)]
    web = Web({"check_runs": runs}, httpx.Response(302, headers={"location": "https://logs.example.test/1"}),
              RuntimeError("no logs"), *[httpx.Response(200, text="tail") for _ in range(8)])
    result = op.get_ci_failure_context(None, web, FULL, {"head_sha": SHA})
    assert len(result["data"]["jobs"]) == 10 and result["truncated"]
    assert result["data"]["omissions"] == {"jobs": 2}
    assert result["data"]["jobs"][0]["log_tail"].splitlines() == [str(i) for i in range(50, 250)]
    assert result["data"]["jobs"][1]["note"] == "log unavailable"
    assert fetched == [("https://logs.example.test/1", {"timeout": 20, "follow_redirects": False})]
    assert result["data"]["statuses_note"] == "status read unavailable"


def test_credentials_stripped_recursively():
    assert op._strip({"token": "secret", "a": [{"access_tokens_url": "secret", "_links": {}, "id": 2}]}) == {
        "a": [{"id": 2}]}
