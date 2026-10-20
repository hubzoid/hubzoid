"""`push_branch`, pull request updates under approval, and GitHub writes
from work that did not start on GitHub. A local bare repository stands in for
GitHub's Git endpoint and a fake serves its REST API. No network."""
from __future__ import annotations

import hashlib
import re
import subprocess

import httpx
import pytest

from hubzoid import db
from hubzoid.channels.errors import ChannelError
from hubzoid.channels.github import client as gh
from hubzoid.channels.github import operations
from hubzoid.work_items import store as wstore
from hubzoid.work_items.errors import Parked
from hubzoid.work_items.operations import OperationFailed
from hubzoid.workflows import context

from .test_channels_github import FakeGitHub, app_conn, hub_dir  # noqa: F401 - fixtures

GIT_ENV = {"GIT_AUTHOR_NAME": "Agent", "GIT_AUTHOR_EMAIL": "agent@sandbox.invalid",
           "GIT_COMMITTER_NAME": "Agent", "GIT_COMMITTER_EMAIL": "agent@sandbox.invalid",
           "GIT_AUTHOR_DATE": "2026-10-20T10:00:00Z", "GIT_COMMITTER_DATE": "2026-10-20T10:00:00Z",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True).stdout


class Remote:
    """A bare repository at <root>/example-co/widget.git with one commit on main."""

    def __init__(self, root):
        self.bare = root / "example-co" / "widget.git"
        self.bare.mkdir(parents=True)
        git(self.bare, "init", "--quiet", "--bare", "--initial-branch=main")
        work = root / "seed"
        work.mkdir()
        git(work, "init", "--quiet", "--initial-branch=main")
        (work / "calc.py").write_text("def total(xs):\n    return sum(xs) + 1\n")
        git(work, "add", ".")
        git(work, "commit", "--quiet", "-m", "Initial")
        git(work, "remote", "add", "origin", str(self.bare))
        git(work, "push", "--quiet", "origin", "main")
        self.base = git(work, "rev-parse", "HEAD").strip()
        self.root = root

    def head(self, branch):
        out = subprocess.run(["git", "rev-parse", f"refs/heads/{branch}"], cwd=self.bare, env=GIT_ENV,
                             capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else None

    def patch(self, start, name="fix", text="def total(xs):\n    return sum(xs)\n"):
        """Commits made as a sandbox would, exported with format-patch."""
        work = self.root / f"sandbox-{name}"
        subprocess.run(["git", "clone", "--quiet", str(self.bare), str(work)], env=GIT_ENV, check=True)
        git(work, "checkout", "--quiet", "--detach", start)
        (work / "calc.py").write_text(text)
        git(work, "commit", "--quiet", "-am", f"Fix total ({name})")
        return git(work, "format-patch", "--stdout", f"{start}..HEAD").encode()


class PushGitHub(FakeGitHub):
    def __init__(self, remote: Remote):
        super().__init__()
        self.remote = remote
        self.protected = set()
        self.pr_head = lambda: "c" * 40

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and re.fullmatch(r"/repos/example-co/widget/pulls/\d+", path):
            return httpx.Response(200, json={"number": 7, "node_id": "PR_7", "head": {"sha": self.pr_head()}})
        if request.method == "GET" and path == "/repos/example-co/widget":
            return httpx.Response(200, json={"full_name": "example-co/widget", "default_branch": "main"})
        m = re.fullmatch(r"/repos/example-co/widget/branches/(.+)", request.url.raw_path.decode().split("?")[0])
        if m and request.method == "GET":
            from urllib.parse import unquote

            name = unquote(m.group(1))
            head = self.remote.head(name)
            if head is None:
                return httpx.Response(404, json={"message": "Branch not found"})
            return httpx.Response(200, json={"name": name, "commit": {"sha": head},
                                             "protected": name in self.protected})
        return super().__call__(request)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    r = Remote(tmp_path / "git")
    monkeypatch.setenv("HUBZOID_GITHUB_WEB_URL", f"file://{tmp_path / 'git'}")
    return r


def _forget_tokens():
    """githubkit keeps installation tokens in a process-wide memory cache:
    start and end each test without them, so no other test sees ours."""
    from githubkit.cache import DEFAULT_CACHE_STRATEGY

    cache = getattr(DEFAULT_CACHE_STRATEGY, "_cache", None)
    if cache is not None:
        cache._cache.clear()


@pytest.fixture(autouse=True)
def _isolated_tokens():
    _forget_tokens()
    yield
    _forget_tokens()


@pytest.fixture
def api(remote, monkeypatch):
    fake = PushGitHub(remote)
    monkeypatch.setattr(gh, "transport", httpx.MockTransport(fake))
    gh._NAMES.clear()
    return fake


def work(hub_dir, *, requester="ana@example.org", origin=None, kind=None, stage="deliver", item_id="item-1"):
    """Run inside one work item, as the engine would."""
    payload = {"kind": kind} if kind else {}
    item = {"id": item_id, "hub": "factory", "generation": 1,
            "route_json": {"stages": None, "actions": None}, "route": "implement",
            "requester": requester, "origin_json": origin or {"surface": "api"}, "input_json": {"input": payload}}
    return context.run_scope(hub="factory", workflow="factory", hub_dir=hub_dir,
                             engine=db.operational_engine(hub_dir), run_id="run-1",
                             work={"item": item, "stage": stage})


def push(conn, data, **kw):
    args = dict(repository_id=2001, branch="factory/12-fix-total", patch=data,
                patch_sha256="sha256:" + hashlib.sha256(data).hexdigest())
    args.update(kw)
    return operations.push_branch(conn, **args)


def test_push_creates_the_branch_with_a_narrowed_token(hub_dir, api, remote):
    conn = app_conn(hub_dir)
    data = remote.patch(remote.base)
    with work(hub_dir):
        out = push(conn, data, base_sha=remote.base)
    assert out["branch"] == "factory/12-fix-total" and out["commits"] == 1
    assert remote.head("factory/12-fix-total") == out["head_sha"] and out["previous_head"] is None
    assert remote.head("main") == remote.base                       # the default branch is untouched
    tokens = [b for m, p, b in api.calls if p.endswith("/access_tokens")]
    assert {"repository_ids": [2001], "permissions": {"metadata": "read", "contents": "write"}} in tokens
    assert all(t["repository_ids"] == [2001] for t in tokens)
    ops = wstore.list_ops(hub_dir, "factory", item_id="item-1")
    assert ops[0]["name"] == "github.pushBranch" and ops[0]["state"] == "succeeded"
    assert ops[0]["result_json"]["head_sha"] == out["head_sha"]


def test_commit_id_is_deterministic(hub_dir, api, remote, tmp_path):
    """The committer date is the author date, so a recovery can recompute the
    exact commit and recognise a push that already landed."""
    conn = app_conn(hub_dir)
    data = remote.patch(remote.base)
    args = {"base_sha": remote.base, "expected_head": None}
    a = operations._apply(conn, "example-co/widget", args, data, "t", (tmp_path / "a").mkdir() or tmp_path / "a")
    b = operations._apply(conn, "example-co/widget", args, data, "t", (tmp_path / "b").mkdir() or tmp_path / "b")
    assert a == b


def test_revision_fast_forwards_on_the_expected_head(hub_dir, api, remote):
    conn = app_conn(hub_dir)
    with work(hub_dir):
        first = push(conn, remote.patch(remote.base), base_sha=remote.base)
    second_patch = remote.patch(first["head_sha"], name="ci", text="def total(xs):\n    return sum(list(xs))\n")
    with work(hub_dir, stage="revise"):
        second = push(conn, second_patch, base_sha=remote.base, expected_head=first["head_sha"])
    assert second["previous_head"] == first["head_sha"]
    assert git(remote.bare, "rev-parse", f"{second['head_sha']}^").strip() == first["head_sha"]


def test_refusals_change_nothing(hub_dir, api, remote):
    conn = app_conn(hub_dir)
    data = remote.patch(remote.base)
    with work(hub_dir):
        for branch, code in [("main", "validation_failed"), ("refs/heads/x", "validation_failed"),
                             ("a..b", "validation_failed"), ("-x", "validation_failed")]:
            with pytest.raises(ChannelError) as exc:
                push(conn, data, base_sha=remote.base, branch=branch)
            assert exc.value.code == code, branch
        with pytest.raises(ChannelError) as exc:
            push(conn, data, base_sha=remote.base, patch_sha256="sha256:" + "0" * 64)
        assert exc.value.code == "patch_changed"
        api.protected.add("release/1")
        git(remote.bare, "branch", "release/1", "main")
        with pytest.raises(ChannelError) as exc:
            push(conn, data, base_sha=remote.base, branch="release/1", expected_head=remote.base)
        assert exc.value.code == "branch_protected"
        git(remote.bare, "branch", "factory/12-fix-total", "main")       # someone else's branch
        with pytest.raises(ChannelError) as exc:
            push(conn, data, base_sha=remote.base)
        assert exc.value.code == "branch_exists"
        with pytest.raises(ChannelError) as exc:
            push(conn, data, base_sha=remote.base, expected_head="a" * 40)
        assert exc.value.code == "stale_head"
    assert remote.head("factory/12-fix-total") == remote.base
    assert not wstore.list_ops(hub_dir, "factory", item_id="item-1")


def test_default_branch_by_name_is_refused(hub_dir, api, remote):
    conn = app_conn(hub_dir)
    data = remote.patch(remote.base)
    with work(hub_dir):
        with pytest.raises(ChannelError) as exc:
            push(conn, data, base_sha=remote.base, branch="develop", base_branch="develop")
    assert exc.value.code == "default_branch_refused"


def test_push_needs_a_work_item_and_never_runs_in_a_station(hub_dir, api, remote):
    conn = app_conn(hub_dir)
    data = remote.patch(remote.base)
    with pytest.raises(ChannelError) as exc:
        push(conn, data, base_sha=remote.base)
    assert exc.value.code == "work_item_required"
    with work(hub_dir) as _:
        context._ctx().work["station_call"] = "call-1"
        with pytest.raises(ChannelError) as exc:
            push(conn, data, base_sha=remote.base)
    assert exc.value.code == "out_of_scope"


def test_a_moved_branch_is_rejected_not_overwritten(hub_dir, api, remote, monkeypatch):
    """The lease: the branch moves between the check and the push."""
    conn = app_conn(hub_dir)
    with work(hub_dir):
        first = push(conn, remote.patch(remote.base), base_sha=remote.base)
    data = remote.patch(first["head_sha"], name="second", text="x = 1\n")
    real = operations._apply

    def racing(*a, **kw):
        out = real(*a, **kw)
        git(remote.bare, "update-ref", "refs/heads/factory/12-fix-total", remote.base)   # someone pushed
        return out

    monkeypatch.setattr(operations, "_apply", racing)
    with work(hub_dir, stage="revise"):
        with pytest.raises(OperationFailed) as exc:
            push(conn, data, base_sha=remote.base, expected_head=first["head_sha"])
    assert exc.value.code == "operation_rejected"
    assert remote.head("factory/12-fix-total") == remote.base


def test_a_replayed_push_is_settled_by_its_record(hub_dir, api, remote):
    """The process stopped after the push landed but before it was recorded:
    the replay reads the branch, recognises its own commit, and pushes nothing."""
    conn = app_conn(hub_dir)
    data = remote.patch(remote.base)
    with work(hub_dir):
        out = push(conn, data, base_sha=remote.base)
    op = wstore.list_ops(hub_dir, "factory", item_id="item-1")[0]
    wstore.update_op(hub_dir, op["id"], expected_revision=op["revision"], state="sending", result_json=None)
    def writes():
        return len([b for _, p, b in api.calls if p.endswith("/access_tokens")
                    and b.get("permissions", {}).get("contents") == "write"])

    pushes = writes()
    with work(hub_dir):
        again = push(conn, data, base_sha=remote.base)        # no branch_exists: the record decides
    assert again["head_sha"] == out["head_sha"]
    assert wstore.get_op(hub_dir, op["id"])["state"] == "succeeded"
    assert writes() == pushes + 1        # one token for the check's clone, and no push


def test_a_dropped_connection_after_the_push_is_recognised(hub_dir, api, remote, monkeypatch):
    conn = app_conn(hub_dir)
    real = operations._git

    def flaky(cwd, env, *args, **kw):
        out = real(cwd, env, *args, **kw)
        if args and args[0] == "push":
            raise operations._GitFailed("push", 128, "fatal: the remote end hung up unexpectedly")
        return out

    monkeypatch.setattr(operations, "_git", flaky)
    with work(hub_dir):
        out = push(conn, remote.patch(remote.base), base_sha=remote.base)
    assert remote.head("factory/12-fix-total") == out["head_sha"]
    assert wstore.list_ops(hub_dir, "factory", item_id="item-1")[0]["state"] == "succeeded"


def test_a_push_that_never_left_is_safe_to_retry(hub_dir, api, remote, monkeypatch):
    conn = app_conn(hub_dir)
    real = operations._git

    def down(cwd, env, *args, **kw):
        if args and args[0] == "push":
            raise operations._GitFailed("push", 128, "fatal: unable to access: Could not resolve host")
        return real(cwd, env, *args, **kw)

    monkeypatch.setattr(operations, "_git", down)
    from hubzoid.work_items.errors import NotSent

    data = remote.patch(remote.base)
    with work(hub_dir):
        with pytest.raises(NotSent):
            push(conn, data, base_sha=remote.base)
    assert wstore.list_ops(hub_dir, "factory", item_id="item-1")[0]["state"] == "failed_before_send"
    monkeypatch.setattr(operations, "_git", real)
    with work(hub_dir):
        out = push(conn, data, base_sha=remote.base)
    assert remote.head("factory/12-fix-total") == out["head_sha"]


# -- writes from any work item, label pull requests, approval of updates ---------------------

def test_attended_chat_work_may_open_a_draft(hub_dir, api, remote):
    conn = app_conn(hub_dir)
    with work(hub_dir, origin={"surface": "chat"}):
        out = operations.call(conn, "createPullRequest", {"repository_id": 2001, "title": "Fix", "base": "main",
                                                          "head": "factory/12-fix-total"})
    assert out["data"]["draft"] is True


def test_unattended_label_work_opens_a_draft_only_for_its_own_branch(hub_dir, api, remote, monkeypatch):
    conn = app_conn(hub_dir)
    monkeypatch.setattr(operations, "_bound_origin", lambda: {
        "item": {"id": "item-1"}, "coords": {"repository_id": 2001, "number": 12}, "hub_dir": hub_dir,
        "hub": "factory", "unattended": True, "trigger": "label", "in_station": False})
    args = {"repository_id": 2001, "title": "Fix", "base": "main", "head": "factory/12-fix-total"}
    with work(hub_dir, requester="workflow:factory"):
        with pytest.raises(ChannelError) as exc:
            operations.call(conn, "createPullRequest", args)        # not pushed by this work
        assert exc.value.code == "out_of_scope"
        push(conn, remote.patch(remote.base), base_sha=remote.base)
        out = operations.call(conn, "createPullRequest", args)
        assert out["data"]["draft"] is True
        with pytest.raises(ChannelError):
            operations.call(conn, "createIssue", {"repository_id": 2001, "title": "x"})


def test_unattended_ci_work_cannot_open_a_pull_request(hub_dir, api, remote, monkeypatch):
    conn = app_conn(hub_dir)
    monkeypatch.setattr(operations, "_bound_origin", lambda: {
        "item": {"id": "item-1"}, "coords": {"repository_id": 2001, "number": 7}, "hub_dir": hub_dir,
        "hub": "factory", "unattended": True, "trigger": "ci_failure", "in_station": False})
    with work(hub_dir, requester="workflow:factory"):
        push(conn, remote.patch(remote.base), base_sha=remote.base)
        with pytest.raises(ChannelError) as exc:
            operations.call(conn, "createPullRequest", {"repository_id": 2001, "title": "Fix", "base": "main",
                                                        "head": "factory/12-fix-total"})
    assert exc.value.code == "out_of_scope"


def test_pr_update_is_off_until_the_hub_asks_for_approval(hub_dir, api):
    conn = app_conn(hub_dir)
    with work(hub_dir):
        with pytest.raises(ChannelError) as exc:
            operations.call(conn, "updatePullRequest", {"repository_id": 2001, "number": 7, "draft": False})
    assert exc.value.code == "pr_update_requires_approval"
    assert not any(m == "PATCH" for m, _, _ in api.calls)


def test_pr_update_waits_for_the_exact_approval(hub_dir, api):
    (hub_dir / "work-items.yaml").write_text("version: 1\nwrite_modes:\n  github.pr_update: approve\n")
    conn = app_conn(hub_dir)
    with work(hub_dir):
        with pytest.raises(Parked) as exc:
            operations.call(conn, "updatePullRequest", {"repository_id": 2001, "number": 7, "title": "Ready"})
    assert exc.value.kind == "approval"
    op = wstore.get_op(hub_dir, exc.value.operation_id)
    assert op["state"] == "awaiting_approval" and op["action"] == "github.pr_update,github.write"
    assert not any(m == "PATCH" for m, _, _ in api.calls)


def test_pr_update_never_from_unattended_work_or_a_station(hub_dir, api):
    (hub_dir / "work-items.yaml").write_text("version: 1\nwrite_modes:\n  github.pr_update: approve\n")
    conn = app_conn(hub_dir)
    with work(hub_dir, requester="workflow:factory"):
        with pytest.raises(ChannelError) as exc:
            operations.call(conn, "updatePullRequest", {"repository_id": 2001, "number": 7, "title": "x"})
    assert exc.value.code == "pr_update_requires_approval"
    with work(hub_dir):
        context._ctx().work["station_call"] = "call-1"
        with pytest.raises(ChannelError) as exc:
            operations.call(conn, "closeIssue", {"repository_id": 2001, "number": 7})   # 7 is a pull request
    assert exc.value.code == "pr_update_requires_approval"


def test_draft_mode_is_refused_for_pr_updates():
    from hubzoid.work_items import config

    with pytest.raises(Exception):
        config.parse("version: 1\nwrite_modes:\n  github.pr_update: draft\n")


def test_a_hub_sets_the_route_each_trigger_asks_for(hub_dir, api, monkeypatch):
    import textwrap

    from hubzoid.channels import router, store
    from hubzoid.work_items import service as wi

    from .test_channels_github import _label, comment, deliver

    (hub_dir / "workflows" / "factory" / "main.py").write_text(textwrap.dedent('''
        from hubzoid import hub, workflow
        from hubzoid.work_items import Route

        @workflow(work_items={"request": Route(), "implement": Route(), "triage": Route()})
        def factory():
            return {"reply": "done"}
    '''))
    monkeypatch.setattr(wi, "start", lambda hub_dir, item: item)
    conn = app_conn(hub_dir, rules={"label": "factory", "routes": {"label": "implement", "mention": "request"}})
    deliver(conn, "issues", _label("factory"))
    deliver(conn, "issue_comment", comment("@acme-factory please fix this", number=13), delivery="d2")
    for row in store.open_deliveries(hub_dir, "factory"):
        router._route_one(hub_dir, row, None)
    routes = {}
    for row in store.list_deliveries(hub_dir, "factory", "github")["items"]:
        item = wstore.get_item(hub_dir, row["route_json"]["work_item_id"])
        routes[item["input_json"]["input"]["kind"]] = item["route"]
    assert routes == {"label": "implement", "mention": "request"}


def test_route_names_are_checked():
    from hubzoid.channels.github.adapter import Rules

    assert Rules().route("label") == "triage"          # the default, unchanged for other hubs
    with pytest.raises(Exception):
        Rules(routes={"label": "Not A Route"})
    with pytest.raises(Exception):
        Rules(routes={"schedule": "implement"})


def test_a_push_the_remote_refuses_is_rejected_not_ambiguous(hub_dir, api, remote):
    """A branch rule GitHub enforces (a ruleset on a new branch name) refuses
    the push itself: recorded as rejected, never as an unknown outcome."""
    hook = remote.bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'protected branch hook declined' >&2\nexit 1\n")
    hook.chmod(0o755)
    conn = app_conn(hub_dir)
    with work(hub_dir):
        with pytest.raises(OperationFailed) as exc:
            push(conn, remote.patch(remote.base), base_sha=remote.base)
    assert exc.value.code == "operation_rejected"
    op = wstore.list_ops(hub_dir, "factory", item_id="item-1")[0]
    assert op["state"] == "rejected" and remote.head("factory/12-fix-total") is None


def test_pr_update_approval_is_bound_to_the_head(hub_dir, api):
    """The pull request moved after approval: a fresh approval is needed."""
    (hub_dir / "work-items.yaml").write_text("version: 1\nwrite_modes:\n  github.pr_update: approve\n")
    conn = app_conn(hub_dir)
    heads = iter(["a" * 40, "b" * 40])
    api.pr_head = lambda: next(heads)
    with work(hub_dir):
        with pytest.raises(Parked) as exc:
            operations.call(conn, "updatePullRequest", {"repository_id": 2001, "number": 7, "draft": False})
    op = wstore.get_op(hub_dir, exc.value.operation_id)
    assert op["target_version"] == "a" * 40
    wstore.update_op(hub_dir, op["id"], expected_revision=op["revision"], approval_json={
        "digest": op["args_digest"], "approver": "ana@example.org", "target_version": op["target_version"]})
    import hubzoid.work_items.operations as ops_mod

    ops_mod_allowed = ops_mod._approver_still_allowed
    ops_mod._approver_still_allowed = lambda *a: True
    try:
        with work(hub_dir):
            with pytest.raises(Parked) as again:
                operations.call(conn, "updatePullRequest", {"repository_id": 2001, "number": 7, "draft": False})
    finally:
        ops_mod._approver_still_allowed = ops_mod_allowed
    assert again.value.reason == "target_changed"
    assert not any(p == "/graphql" for _, p, _ in api.calls)


# -- review fixes ---------------------------------------------------------------------------

def test_work_without_a_person_is_unattended(hub_dir, api, remote):
    """A webhook-started work item has no requester: it gets the unattended
    limits, never attended authority."""
    conn = app_conn(hub_dir)
    for requester in ("", "workflow:factory", "service-account"):
        with work(hub_dir, requester=requester, origin={"surface": "webhook"}):
            with pytest.raises(ChannelError) as exc:
                operations.call(conn, "createIssue", {"repository_id": 2001, "title": "x"})
            assert exc.value.code == "out_of_scope", requester


def test_pull_request_is_bound_to_the_reviewed_head(hub_dir, api, remote):
    conn = app_conn(hub_dir)
    with work(hub_dir, origin={"surface": "chat"}):
        pushed = push(conn, remote.patch(remote.base), base_sha=remote.base)
        with pytest.raises(OperationFailed) as exc:
            operations.call(conn, "createPullRequest", {"repository_id": 2001, "title": "Fix", "base": "main",
                                                        "head": "factory/12-fix-total", "head_sha": "a" * 40})
        assert exc.value.code == "operation_rejected"
        assert not any(p.endswith("/pulls") and m == "POST" for m, p, _ in api.calls)
    with work(hub_dir, origin={"surface": "chat"}, item_id="item-2"):
        out = operations.call(conn, "createPullRequest", {"repository_id": 2001, "title": "Fix", "base": "main",
                                                          "head": "factory/12-fix-total",
                                                          "head_sha": pushed["head_sha"]})
    assert out["data"]["draft"] is True


def test_a_lost_pull_request_response_is_recognised(hub_dir, api, remote, monkeypatch):
    conn = app_conn(hub_dir)
    created = {}
    real = api.__call__

    def lossy(request):
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            real(request)
            created["yes"] = True
            raise httpx.ReadError("connection reset")
        if request.method == "GET" and request.url.path.endswith("/pulls") and created:
            return httpx.Response(200, json=[{"number": 7, "draft": True, "html_url": "https://pr",
                                              "head": {"ref": "factory/x"}}])
        return real(request)

    monkeypatch.setattr(gh, "transport", httpx.MockTransport(lossy))
    with work(hub_dir, origin={"surface": "chat"}):
        out = operations.call(conn, "createPullRequest", {"repository_id": 2001, "title": "Fix", "base": "main",
                                                          "head": "factory/x"})
    assert out["data"]["number"] == 7
    op = wstore.list_ops(hub_dir, "factory", item_id="item-1")[0]
    assert op["state"] == "succeeded" and op["recovery"] == "reconcile"


def test_pr_update_mode_does_not_depend_on_import_order():
    import subprocess
    import sys

    code = ("from hubzoid.work_items import config; import sys; "
            "assert 'hubzoid.channels.github.operations' not in sys.modules; "
            "print(config.mode_for(config.Config(), ['github.pr_update']))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "off"


def test_a_checkout_can_be_pinned_to_a_commit(remote, tmp_path):
    from hubzoid.sandbox import service as sandbox_service
    from hubzoid.work_items.errors import WorkItemError

    first = remote.base
    second = remote.patch(first)       # leaves the bare repository unchanged
    del second
    work_dir = tmp_path / "advance"
    subprocess.run(["git", "clone", "--quiet", str(remote.bare), str(work_dir)], env=GIT_ENV, check=True)
    (work_dir / "calc.py").write_text("x = 2\n")
    git(work_dir, "commit", "--quiet", "-am", "main moves on")
    git(work_dir, "push", "--quiet", "origin", "main")

    class Prov:
        def write_tree(self, state, src, target):
            self.head = git(src, "rev-parse", "HEAD").strip()
            return {}

    prov = Prov()
    out = sandbox_service._checkout(prov, {}, {"url": str(remote.bare), "ref": "main", "commit": first})
    assert out["commit"] == first == prov.head
    with pytest.raises(WorkItemError):
        sandbox_service._checkout(prov, {}, {"url": str(remote.bare), "commit": "b" * 40})


def test_an_export_says_whether_the_tree_had_uncommitted_changes(tmp_path, monkeypatch):
    """A reviewer that edits files without committing is visible in its export."""
    from hubzoid.sandbox import service as sandbox_service

    class Prov:
        def __init__(self, dirty):
            self.dirty = dirty

        def quiesce(self, binding):
            pass

        def run(self, binding, argv, timeout=60):
            if "status" in argv:
                return {"exit_code": 0, "stdout": b" M calc.py\n" if self.dirty else b"", "stderr": b""}
            return {"exit_code": 0, "stdout": b"", "stderr": b""}

    row = {"provider_json": {}, "profile_json": {}, "checkouts_json": [{"path": "/workspace/repo", "commit": "a" * 40}]}
    monkeypatch.setattr(sandbox_service, "attach", lambda hub_dir, sid: row)
    monkeypatch.setattr(sandbox_service, "_update", lambda *a, **kw: None)
    for dirty in (False, True):
        monkeypatch.setattr(sandbox_service, "_provider_for", lambda hub_dir, r, d=dirty: Prov(d))
        out = sandbox_service.export_patches(tmp_path, "box-1", tmp_path / "out")
        assert out[0]["dirty"] is dirty and out[0]["commits"] == 0
