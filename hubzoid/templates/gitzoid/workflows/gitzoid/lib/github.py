"""The GitHub REST and GraphQL calls GitZoid makes, through one client.

Reads need a fine-grained token or renewable GitHub App authentication with
Metadata, Contents, Pull requests, Issues, Commit statuses and Checks read.
Posting needs Pull requests and Issues write; review status needs Commit statuses write.

With `dry_run` (the default until `review.post_to_github: true`), every write
is recorded in `outbox` and nothing is sent, so a first setup can be checked
safely against real repos.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

API = "https://api.github.com"
TIMEOUT = 30
MAX_FILE_CHARS = 200_000
MAX_FILES = 3000  # GitHub lists at most 3,000 files for one pull request
DISCUSSION = 40


class GitHubError(RuntimeError):
    pass


class RateLimited(GitHubError):
    pass


def ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def pr_summary(p: dict, repo: str) -> dict:
    user = p.get("user") or {}
    return {
        "repo": repo,
        "number": p["number"],
        "title": p.get("title") or "",
        "body": (p.get("body") or "")[:6000],
        "author": user.get("login", ""),
        "author_type": user.get("type", "User"),
        "draft": bool(p.get("draft")),
        "labels": [lb["name"] if isinstance(lb, dict) else lb for lb in p.get("labels") or []],
        "head_sha": (p.get("head") or {}).get("sha", ""),
        "head_ref": (p.get("head") or {}).get("ref", ""),
        "head_repo": ((p.get("head") or {}).get("repo") or {}).get("full_name", repo),
        "base_ref": (p.get("base") or {}).get("ref", ""),
        "state": p.get("state", "open"),
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
        "merged_at": p.get("merged_at"),
        "url": p.get("html_url", ""),
        "requested_reviewers": [r.get("login") for r in p.get("requested_reviewers") or [] if r.get("login")],
    }


def file_entry(f: dict) -> dict:
    return {"filename": f["filename"], "status": f.get("status", "modified"),
            "additions": f.get("additions", 0), "deletions": f.get("deletions", 0),
            "patch": f.get("patch"), "previous_filename": f.get("previous_filename")}


def comment_entry(c: dict, kind: str, number: int | None = None) -> dict:
    user = c.get("user") or {}
    if number is None:
        url = c.get("pull_request_url") or c.get("issue_url") or ""
        number = int(url.rsplit("/", 1)[1]) if url else 0
    return {"id": c["id"], "kind": kind, "number": number, "author": user.get("login", ""),
            "author_type": user.get("type", "User"), "body": c.get("body") or "",
            "created_at": c.get("created_at"), "updated_at": c.get("updated_at") or c.get("created_at"),
            "in_reply_to": c.get("in_reply_to_id"), "path": c.get("path"),
            "line": c.get("line") or c.get("original_line"), "url": c.get("html_url", "")}


MERGED_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      number title body url createdAt mergedAt additions deletions changedFiles mergeCommit { oid }
      author { login } labels(first: 10) { nodes { name } }
      reviews(first: 100) { pageInfo { hasNextPage } nodes { author { login __typename } state submittedAt body } }
      files(first: 100) { pageInfo { hasNextPage } nodes { path additions deletions } }
    } }
  }
}"""

OPEN_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { number title url createdAt updatedAt isDraft author { login }
        reviewRequests(first: 10) { nodes { requestedReviewer { ... on User { login } } } }
        reviews(first: 100) { pageInfo { hasNextPage } nodes { author { login __typename } state submittedAt body } } }
    }
  }
}"""


class AppAuth:
    """Installation tokens refreshed before expiry; credentials never enter the store."""

    def __init__(self, app_id: str, installation_id: str, private_key: str, transport=None):
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
        try:
            self.key = load_pem_private_key(private_key.replace("\\n", "\n").encode(), password=None)
            if not isinstance(self.key, RSAPrivateKey):
                raise ValueError("RSA key required")
        except (ValueError, TypeError):
            raise GitHubError("GITHUB_APP_PRIVATE_KEY must be the App's RSA PEM private key") from None
        if not installation_id.isdigit():
            raise GitHubError("GITHUB_INSTALLATION_ID must be a numeric installation ID")
        self.app_id, self.installation_id = app_id, installation_id
        self.client = httpx.Client(base_url=API, timeout=TIMEOUT, transport=transport,
                                   headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        import threading
        self.lock = threading.RLock()
        self.value, self.expires, self.login = "", 0.0, ""

    def jwt(self) -> str:
        import base64
        import json
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        def encode(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).decode().rstrip("=")
        now = int(time.time())
        head = encode(b'{"alg":"RS256","typ":"JWT"}')
        body = encode(json.dumps({"iat": now - 60, "exp": now + 540, "iss": self.app_id}).encode())
        message = f"{head}.{body}"
        return message + "." + encode(self.key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256()))

    def token(self) -> str:
        with self.lock:
            return self._token()

    def _token(self) -> str:
        if self.value and time.time() < self.expires - 60:
            return self.value
        headers = {"Authorization": "Bearer " + self.jwt()}
        if not self.login:
            app = self.client.get("/app", headers=headers)
            if app.status_code != 200:
                raise GitHubError(f"GitHub App authentication failed ({app.status_code}); check App ID and private key")
            self.login = app.json()["slug"] + "[bot]"
        response = self.client.post(f"/app/installations/{self.installation_id}/access_tokens", headers=headers)
        if response.status_code != 201:
            raise GitHubError(f"GitHub installation token failed ({response.status_code}); check installation ID and access")
        data = response.json()
        self.value, self.expires = data["token"], ts(data["expires_at"]).timestamp()
        return self.value


class GitHub:
    """One client per run. `store` keeps ETags so unchanged lists cost nothing
    against the rate limit."""

    name = "github"

    def __init__(self, token: str, store=None, dry_run: bool = True, transport: httpx.BaseTransport | None = None, app_auth: AppAuth | None = None):
        self.app_auth = app_auth
        if app_auth:
            token = app_auth.token()
        if not token:
            raise GitHubError("GITHUB_TOKEN is not set in the hub's .env (see README.md)")
        self.store = store
        self.dry_run = dry_run
        self.outbox: list[dict] = []
        self._login: str | None = None
        self.client = httpx.Client(base_url=API, timeout=TIMEOUT, follow_redirects=True, transport=transport,
                                   headers={"Authorization": f"Bearer {token}",
                                            "Accept": "application/vnd.github+json",
                                            "X-GitHub-Api-Version": "2022-11-28",
                                            "User-Agent": "gitzoid-hub"})

    # --------------------------------------------------------------- plumbing

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _send(self, method: str, path: str, **kw) -> httpx.Response:
        for attempt in range(4):
            if self.app_auth:
                self.client.headers["Authorization"] = "Bearer " + self.app_auth.token()
            r = self.client.request(method, path, **kw)
            if r.status_code == 401 and self.app_auth and attempt == 0:
                self.app_auth.expires = 0
                continue
            if r.status_code in (403, 429) and (r.headers.get("retry-after") or r.headers.get("x-ratelimit-remaining") == "0"):
                wait = float(r.headers.get("retry-after") or max(0, int(r.headers.get("x-ratelimit-reset", "0")) - time.time()))
                if wait > 60 or attempt == 3:
                    raise RateLimited(f"GitHub rate limit reached; resets in {int(wait)} s")
                time.sleep(max(wait, 1))
                continue
            if r.status_code in (502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return r
        return r

    def _get(self, path: str, **params):
        r = self._send("GET", path, params=params or None)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise GitHubError(f"GET {path}: {r.status_code} {r.text[:200]}")
        return r.json()

    def _pages(self, path: str, limit: int, **params) -> list:
        out, url, query = [], path, {"per_page": 100, **params}
        while url and len(out) < limit:
            r = self._send("GET", url, params=query)
            if r.status_code == 404:
                return out
            if r.status_code >= 400:
                raise GitHubError(f"GET {path}: {r.status_code} {r.text[:200]}")
            out.extend(r.json())
            url, query = r.links.get("next", {}).get("url"), None
        return out[:limit]

    def _graphql(self, query: str, **variables) -> dict:
        r = self._send("POST", "/graphql", json={"query": query, "variables": variables})
        data = r.json() if r.content else {}
        if r.status_code >= 400 or data.get("errors"):
            raise GitHubError(f"GraphQL: {r.status_code} {str(data.get('errors') or r.text)[:300]}")
        return data["data"]

    def _write(self, method: str, path: str, body: dict, summary: str) -> dict:
        if self.dry_run:
            fake = {"id": -(len(self.outbox) + 1)}
            self.outbox.append({"method": method, "path": path, "summary": summary, "body": body})
            return fake
        r = self._send(method, path, json=body)
        if r.status_code >= 400:
            raise GitHubError(f"{method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    # ------------------------------------------------------------------ reads

    def login(self) -> str:
        """GitZoid's own GitHub login, so it never answers itself."""
        if self.app_auth:
            return self.app_auth.login
        if self._login is None:
            me = self._get("/user")
            self._login = (me or {}).get("login") or ""
        return self._login

    def default_branch(self, repo: str) -> str:
        info = self._get(f"/repos/{repo}")
        if info is None:
            raise GitHubError(f"{repo} was not found, or the token cannot read it")
        return info["default_branch"]

    def branch_sha(self, repo: str, branch: str) -> str:
        data = self._get(f"/repos/{repo}/branches/{branch}")
        if data is None:
            raise GitHubError(f"{repo} has no branch {branch}")
        return data["commit"]["sha"]

    def open_pulls(self, repo: str) -> list[dict]:
        key = f"open:{repo}"
        cached = self.store.cursor(key) if self.store else None
        headers = {"If-None-Match": cached["etag"]} if cached and cached.get("etag") else {}
        r = self._send("GET", f"/repos/{repo}/pulls", params={"state": "open", "per_page": 100}, headers=headers)
        if r.status_code == 304 and cached:
            return cached["value"]
        if r.status_code >= 400:
            raise GitHubError(f"listing pull requests in {repo}: {r.status_code} {r.text[:200]}")
        rows = list(r.json())
        url = r.links.get("next", {}).get("url")
        while url and len(rows) < 500:
            more = self._send("GET", url)
            rows.extend(more.json())
            url = more.links.get("next", {}).get("url")
        pulls = [pr_summary(p, repo) for p in rows]
        if self.store:
            self.store.save_cursor(key, pulls, r.headers.get("etag"))
        return pulls

    def pull(self, repo: str, number: int) -> dict | None:
        p = self._get(f"/repos/{repo}/pulls/{number}")
        return pr_summary(p, repo) if p else None

    def pull_files(self, repo: str, number: int) -> list[dict]:
        return [file_entry(f) for f in self._pages(f"/repos/{repo}/pulls/{number}/files", MAX_FILES)]

    def compare_files(self, repo: str, base: str, head: str) -> list[dict] | None:
        """Files changed between two commits, or None when `base` is no longer
        an ancestor of `head` (a force push): then the whole PR is reviewed."""
        data = self._get(f"/repos/{repo}/compare/{base}...{head}")
        if not data or data.get("status") not in ("ahead", "identical") or len(data.get("files") or []) >= 300:
            return None
        return [file_entry(f) for f in data.get("files") or []]

    def file(self, repo: str, path: str, ref: str | None = None) -> str | None:
        r = self._send("GET", f"/repos/{repo}/contents/{path}", params={"ref": ref} if ref else None,
                       headers={"Accept": "application/vnd.github.raw"})
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise GitHubError(f"reading {repo}/{path}: {r.status_code}")
        return r.text[:MAX_FILE_CHARS]

    def issue_comments_since(self, repo: str, since: str) -> list[dict]:
        rows = self._pages(f"/repos/{repo}/issues/comments", 300, since=since, sort="updated", direction="asc")
        return [comment_entry(c, "issue") for c in rows if "/pull/" in (c.get("html_url") or "")]

    def review_comments_since(self, repo: str, since: str) -> list[dict]:
        rows = self._pages(f"/repos/{repo}/pulls/comments", 300, since=since, sort="updated", direction="asc")
        return [comment_entry(c, "review") for c in rows]

    def review_comment(self, repo: str, comment_id: int) -> dict | None:
        c = self._get(f"/repos/{repo}/pulls/comments/{comment_id}")
        return comment_entry(c, "review") if c else None

    def discussion(self, repo: str, number: int) -> list[dict]:
        issue = [comment_entry(c, "issue", number) for c in self._pages(f"/repos/{repo}/issues/{number}/comments", 200)]
        inline = [comment_entry(c, "review", number) for c in self._pages(f"/repos/{repo}/pulls/{number}/comments", 200)]
        return sorted(issue + inline, key=lambda c: c["created_at"] or "")

    def permission(self, repo: str, login: str) -> str:
        data = self._get(f"/repos/{repo}/collaborators/{login}/permission")
        return (data or {}).get("permission", "none")

    def ci_status(self, repo: str, sha: str) -> str:
        try:
            combined = self._get(f"/repos/{repo}/commits/{sha}/status") or {}
            runs = (self._get(f"/repos/{repo}/commits/{sha}/check-runs", per_page=100) or {}).get("check_runs", [])
        except GitHubError as exc:
            return f"unavailable: {exc}; grant Commit statuses: read and Checks: read"
        failing = [s["context"] for s in combined.get("statuses", []) if s.get("state") in ("failure", "error")
                   and not s["context"].startswith("gitzoid")]
        failing += [r["name"] for r in runs if r.get("conclusion") in ("failure", "timed_out", "cancelled")]
        pending = [r["name"] for r in runs if r.get("status") != "completed"]
        pending += [s["context"] for s in combined.get("statuses", [])
                    if s.get("state") == "pending" and not s["context"].startswith("gitzoid")]
        if failing:
            return "failing: " + ", ".join(sorted(set(failing))[:6])
        if pending:
            return "running"
        return "passing" if runs or combined.get("statuses") else "none"

    def issue(self, repo: str, number: int) -> dict | None:
        i = self._get(f"/repos/{repo}/issues/{number}")
        return {"number": number, "title": i.get("title", ""), "body": (i.get("body") or "")[:3000]} if i else None

    def merged_pulls(self, repo: str, since: datetime, until: datetime) -> list[dict]:
        q = f"repo:{repo} is:pr is:merged merged:{since.date().isoformat()}..{until.date().isoformat()}"
        out, after = [], None
        while len(out) < 1000:
            data = self._graphql(MERGED_QUERY, q=q, after=after)["search"]
            for n in data["nodes"]:
                if not n:
                    continue
                merged = ts(n["mergedAt"])
                if merged is None or not (since <= merged < until):
                    continue
                out.append({"repo": repo, "number": n["number"], "title": n["title"], "body": (n["body"] or "")[:3000],
                            "url": n["url"], "author": (n["author"] or {}).get("login", "ghost"),
                            "created_at": n["createdAt"], "merged_at": n["mergedAt"],
                            "additions": n["additions"], "deletions": n["deletions"], "changed_files": n["changedFiles"],
                            "coverage_notes": (["Review history truncated at 100"] if n["reviews"].get("pageInfo", {}).get("hasNextPage") else [])
                                + (["File metadata truncated at 100"] if (n.get("files") or {}).get("pageInfo", {}).get("hasNextPage") else []),
                            "merge_sha": (n.get("mergeCommit") or {}).get("oid"),
                            "labels": [x["name"] for x in n["labels"]["nodes"]],
                            "reviews": [{"author": (r["author"] or {}).get("login", "ghost"), "state": r["state"],
                                         "submitted_at": r["submittedAt"], "author_type": (r["author"] or {}).get("__typename"), "body": r.get("body", "")} for r in n["reviews"]["nodes"]],
                            "files": [{"filename": f["path"], "additions": f["additions"], "deletions": f["deletions"]}
                                      for f in (n.get("files") or {}).get("nodes") or []]})
            if not data["pageInfo"]["hasNextPage"]:
                break
            after = data["pageInfo"]["endCursor"]
        return sorted(out, key=lambda p: p["merged_at"])

    def open_pull_activity(self, repo: str) -> list[dict]:
        owner, name = repo.split("/", 1)
        out, after = [], None
        while len(out) < 500:
            data = self._graphql(OPEN_QUERY, owner=owner, name=name, after=after)["repository"]["pullRequests"]
            for n in data["nodes"]:
                out.append({"repo": repo, "number": n["number"], "title": n["title"], "url": n["url"],
                            "author": (n["author"] or {}).get("login", "ghost"), "draft": n["isDraft"],
                            "created_at": n["createdAt"], "updated_at": n["updatedAt"],
                            "coverage_notes": ["Review history truncated at 100"] if n["reviews"].get("pageInfo", {}).get("hasNextPage") else [],
                            "requested": [((x["requestedReviewer"] or {}).get("login")) for x in n["reviewRequests"]["nodes"]
                                          if (x["requestedReviewer"] or {}).get("login")],
                            "reviews": [{"author": (r["author"] or {}).get("login", "ghost"), "state": r["state"],
                                         "submitted_at": r["submittedAt"], "author_type": (r["author"] or {}).get("__typename"), "body": r.get("body", "")} for r in n["reviews"]["nodes"]]})
            if not data["pageInfo"]["hasNextPage"]:
                break
            after = data["pageInfo"]["endCursor"]
        return out

    def commits(self, repo: str, path: str | None = None, since: datetime | None = None, limit: int = 30) -> list[dict]:
        params = {"path": path} if path else {}
        if since:
            params["since"] = since.isoformat()
        rows = self._pages(f"/repos/{repo}/commits", limit, **params)
        return [{"sha": c["sha"], "message": (c["commit"]["message"] or "")[:500],
                 "author": (c.get("author") or {}).get("login") or c["commit"]["author"]["name"],
                 "date": c["commit"]["author"]["date"]} for c in rows]

    def tarball(self, repo: str, ref: str, max_bytes: int = 300_000_000) -> bytes:
        if self.app_auth:
            self.client.headers["Authorization"] = "Bearer " + self.app_auth.token()
        # GitHub builds the archive on the fly; a large repo can take minutes to start streaming.
        with self.client.stream("GET", f"/repos/{repo}/tarball/{ref}", timeout=httpx.Timeout(30, read=300)) as r:
            if r.status_code >= 400:
                raise GitHubError(f"downloading {repo}@{ref[:7]}: {r.status_code}")
            data = bytearray()
            for chunk in r.iter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise GitHubError(f"{repo} is larger than {max_bytes // 1_000_000} MB; not checked out")
        return bytes(data)

    # ----------------------------------------------------------------- writes

    def upsert_summary(self, repo: str, number: int, body: str, comment_id: int | None, marker: str) -> int:
        """Edit GitZoid's own summary (found by id, else by its hidden marker)
        or create it, so a repeated run never adds a second one."""
        if comment_id and comment_id > 0:
            r = self._write("PATCH", f"/repos/{repo}/issues/comments/{comment_id}", {"body": body}, "edit summary")
            return r.get("id", comment_id)
        if not self.dry_run:
            own = self.login()
            for c in self._pages(f"/repos/{repo}/issues/{number}/comments", 500):
                if marker in (c.get("body") or "") and (c.get("user") or {}).get("login") == own:
                    self._write("PATCH", f"/repos/{repo}/issues/comments/{c['id']}", {"body": body}, "edit summary")
                    return c["id"]
        return self._write("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}, "post summary")["id"]

    def posted_markers(self, repo: str, number: int, pattern: str) -> dict[str, int]:
        """Hidden markers already in GitZoid's inline comments: marker -> comment id."""
        if self.dry_run:
            return {}
        found, own = {}, self.login()
        for c in self._pages(f"/repos/{repo}/pulls/{number}/comments", 1000):
            if (c.get("user") or {}).get("login") != own:
                continue  # a marker anyone can type is not proof GitZoid posted it
            for m in re.findall(pattern, c.get("body") or ""):
                found[m] = c["id"]
        return found

    def post_review(self, repo: str, number: int, sha: str, comments: list[dict], body: str) -> list[dict]:
        review = self._write("POST", f"/repos/{repo}/pulls/{number}/reviews",
                             {"commit_id": sha, "event": "COMMENT", "body": body,
                              "comments": [{"path": c["path"], "line": c["line"], "side": "RIGHT", "body": c["body"]}
                                           for c in comments]}, f"post {len(comments)} inline comment(s)")
        if self.dry_run:
            return [{"path": c["path"], "line": c["line"], "id": None} for c in comments]
        rows = self._pages(f"/repos/{repo}/pulls/{number}/reviews/{review['id']}/comments", 100)
        return [{"path": c["path"], "line": c.get("line"), "id": c["id"], "body": c.get("body") or ""} for c in rows]

    def reply(self, repo: str, number: int, comment: dict, body: str) -> int:
        if comment["kind"] == "review":
            root = comment.get("in_reply_to") or comment["id"]
            return self._write("POST", f"/repos/{repo}/pulls/{number}/comments/{root}/replies",
                               {"body": body}, "reply in thread")["id"]
        return self._write("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}, "reply")["id"]

    def replied(self, repo: str, number: int, marker: str) -> bool:
        if self.dry_run:
            return any(marker in (m["body"].get("body") or "") for m in self.outbox)
        own = self.login()
        return any(marker in c["body"] and c["author"] == own for c in self.discussion(repo, number))

    def react(self, repo: str, comment: dict, content: str) -> None:
        kind = "pulls" if comment["kind"] == "review" else "issues"
        self._write("POST", f"/repos/{repo}/{kind}/comments/{comment['id']}/reactions", {"content": content},
                    f"react {content}")

    def set_status(self, repo: str, sha: str, state: str, description: str, target_url: str | None = None) -> None:
        body = {"state": state, "description": description[:140], "context": "gitzoid/review"}
        if target_url:
            body["target_url"] = target_url
        self._write("POST", f"/repos/{repo}/statuses/{sha}", body, f"status {state}")
