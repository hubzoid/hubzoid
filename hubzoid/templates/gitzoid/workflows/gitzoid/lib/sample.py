"""The bundled sample: a fictional repo, kestrel-oak/storefront, with open pull
requests that contain planted bugs, a clean control, a draft, a dependency
bump and a few months of merged history. Same calls as `github.GitHub`, no
token, no network. Writes are recorded in `outbox`, never sent.

Layout of raw_data/sample/:
  sample.yaml           repo, people and their GitHub permission, merged history
  storefront/           the default branch
  pulls/<n>-<slug>/     pull.yaml plus head/, the changed files at the PR head
  osv/                  trimmed public OSV and CISA KEV records
"""
from __future__ import annotations

import difflib
import hashlib
import json
from datetime import datetime
from pathlib import Path

import yaml

from .github import ts


def _sha(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode())
    return h.hexdigest()


def patch_for(before: str | None, after: str | None) -> str:
    """A GitHub-style patch: hunks only, no file header."""
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    lines = list(difflib.unified_diff(a, b, lineterm="", n=3))
    return "\n".join(lines[2:])


class SampleGitHub:
    name = "sample"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.meta = yaml.safe_load((self.root / "sample.yaml").read_text())
        self.repo = self.meta["repo"]
        self.base_dir = self.root / self.meta.get("base_dir", "storefront")
        self.outbox: list[dict] = []
        self.dry_run = True
        self._pulls = {}
        for d in sorted((self.root / "pulls").iterdir()) if (self.root / "pulls").is_dir() else []:
            if (d / "pull.yaml").is_file():
                p = yaml.safe_load((d / "pull.yaml").read_text())
                p["_dir"] = d
                self._pulls[int(p["number"])] = p

    # ---------------------------------------------------------------- helpers

    def _check(self, repo: str) -> None:
        if repo != self.repo:
            raise ValueError(f"the sample has one repo, {self.repo}, not {repo!r}")

    def base_files(self) -> dict[str, str]:
        return {p.relative_to(self.base_dir).as_posix(): p.read_text()
                for p in sorted(self.base_dir.rglob("*")) if p.is_file()}

    def _head_files(self, p: dict) -> dict[str, str | None]:
        head = p["_dir"] / "head"
        files: dict[str, str | None] = {}
        if head.is_dir():
            files = {x.relative_to(head).as_posix(): x.read_text() for x in sorted(head.rglob("*")) if x.is_file()}
        for path in p.get("removed") or []:
            files[path] = None
        return files

    def _head_sha(self, p: dict) -> str:
        files = self._head_files(p)
        return _sha(str(p["number"]), *(f"{k}\0{v}" for k, v in sorted(files.items(), key=lambda kv: kv[0])))

    def _summary(self, p: dict) -> dict:
        return {"repo": self.repo, "number": p["number"], "title": p["title"], "body": p.get("body", ""),
                "author": p["author"], "author_type": "Bot" if p["author"].endswith("[bot]") else "User",
                "draft": bool(p.get("draft")), "labels": list(p.get("labels") or []),
                "head_sha": self._head_sha(p), "head_ref": p.get("branch", f"pr-{p['number']}"),
                "head_repo": self.repo, "base_ref": self.meta.get("default_branch", "main"),
                "state": p.get("state", "open"), "created_at": p["created_at"],
                "updated_at": p.get("updated_at", p["created_at"]), "merged_at": None,
                "url": f"https://github.com/{self.repo}/pull/{p['number']}",
                "requested_reviewers": list(p.get("requested_reviewers") or [])}

    def _comments(self, p: dict) -> list[dict]:
        out = []
        for c in p.get("comments") or []:
            out.append({"id": int(c["id"]), "kind": c.get("kind", "issue"), "number": p["number"],
                        "author": c["author"], "author_type": "Bot" if c["author"].endswith("[bot]") else "User",
                        "body": c["body"], "created_at": c["created_at"],
                        "updated_at": c.get("updated_at", c["created_at"]), "in_reply_to": c.get("in_reply_to"),
                        "path": c.get("path"), "line": c.get("line"), "url": ""})
        return out

    # ------------------------------------------------------------------ reads

    def now(self) -> datetime:
        return ts(self.meta["now"])

    def login(self) -> str:
        return self.meta.get("gitzoid_login", "gitzoid[bot]")

    def default_branch(self, repo: str) -> str:
        self._check(repo)
        return self.meta.get("default_branch", "main")

    def branch_sha(self, repo: str, branch: str) -> str:
        self._check(repo)
        return _sha("base", *(f"{k}\0{v}" for k, v in self.base_files().items()))

    def open_pulls(self, repo: str) -> list[dict]:
        self._check(repo)
        return [self._summary(p) for p in self._pulls.values() if p.get("state", "open") == "open"]

    def pull(self, repo: str, number: int) -> dict | None:
        self._check(repo)
        p = self._pulls.get(int(number))
        return self._summary(p) if p else None

    def pull_files(self, repo: str, number: int) -> list[dict]:
        self._check(repo)
        p = self._pulls[int(number)]
        base = self.base_files()
        out = []
        for path, after in self._head_files(p).items():
            before = base.get(path)
            status = "removed" if after is None else "added" if before is None else "modified"
            patch = patch_for(before, after)
            out.append({"filename": path, "status": status, "previous_filename": None, "patch": patch,
                        "additions": sum(1 for x in patch.splitlines() if x.startswith("+")),
                        "deletions": sum(1 for x in patch.splitlines() if x.startswith("-"))})
        return out

    def compare_files(self, repo: str, base: str, head: str) -> list[dict] | None:
        return None  # the sample keeps no history of pushes: every review covers the whole PR

    def file(self, repo: str, path: str, ref: str | None = None) -> str | None:
        self._check(repo)
        if ref:
            for p in self._pulls.values():
                if self._head_sha(p) == ref:
                    head = self._head_files(p)
                    if path in head:
                        return head[path]
        target = (self.base_dir / path).resolve()
        if self.base_dir.resolve() not in target.parents or not target.is_file():
            return None
        return target.read_text()

    def issue_comments_since(self, repo: str, since: str) -> list[dict]:
        return [c for p in self._pulls.values() for c in self._comments(p)
                if c["kind"] == "issue" and c["updated_at"] >= since]

    def review_comments_since(self, repo: str, since: str) -> list[dict]:
        return [c for p in self._pulls.values() for c in self._comments(p)
                if c["kind"] == "review" and c["updated_at"] >= since]

    def review_comment(self, repo: str, comment_id: int) -> dict | None:
        posted = next((m for m in self.outbox if m.get("comment_id") == comment_id), None)
        if posted:
            return {"id": comment_id, "kind": "review", "body": posted["body"]["body"], "author": self.login(),
                    "number": posted["number"], "in_reply_to": None}
        return next((c for p in self._pulls.values() for c in self._comments(p) if c["id"] == comment_id), None)

    def discussion(self, repo: str, number: int) -> list[dict]:
        return self._comments(self._pulls[int(number)])

    def permission(self, repo: str, login: str) -> str:
        return (self.meta.get("people") or {}).get(login, {}).get("permission", "read")

    def ci_status(self, repo: str, sha: str) -> str:
        p = next((p for p in self._pulls.values() if self._head_sha(p) == sha), None)
        return (p or {}).get("ci", "passing")

    def issue(self, repo: str, number: int) -> dict | None:
        i = (self.meta.get("issues") or {}).get(number)
        return {"number": number, **i} if i else None

    def merged_pulls(self, repo: str, since: datetime, until: datetime) -> list[dict]:
        self._check(repo)
        out = []
        for h in self.meta.get("history") or []:
            merged = ts(h["merged_at"])
            if since <= merged < until:
                out.append({"repo": repo, "url": f"https://github.com/{repo}/pull/{h['number']}", "body": "",
                            "labels": [], "reviews": [], "files": [], **h})
        return sorted(out, key=lambda p: p["merged_at"])

    def open_pull_activity(self, repo: str) -> list[dict]:
        return [{"repo": repo, "number": p["number"], "title": p["title"], "author": p["author"],
                 "url": f"https://github.com/{repo}/pull/{p['number']}", "draft": bool(p.get("draft")),
                 "created_at": p["created_at"], "updated_at": p.get("updated_at", p["created_at"]),
                 "requested": list(p.get("requested_reviewers") or []), "reviews": list(p.get("reviews") or [])}
                for p in self._pulls.values() if p.get("state", "open") == "open"]

    def commits(self, repo: str, path: str | None = None, since: datetime | None = None, limit: int = 30) -> list[dict]:
        rows = []
        for h in sorted(self.meta.get("history") or [], key=lambda h: h["merged_at"], reverse=True):
            files = [f["filename"] for f in h.get("files") or []]
            if path and not any(f == path or f.startswith(path.rstrip("/") + "/") for f in files):
                continue
            if since and ts(h["merged_at"]) < since:
                continue
            rows.append({"sha": _sha(str(h["number"]))[:40], "message": f"{h['title']} (#{h['number']})",
                         "author": h["author"], "date": h["merged_at"]})
        return rows[:limit]

    # ----------------------------------------------------------------- writes

    def _record(self, method: str, path: str, body: dict, summary: str, **extra) -> dict:
        # Stable and unique like GitHub's ids, negative so they never look real.
        fake = -int(_sha(method, path, json.dumps(body, sort_keys=True))[:12], 16)
        self.outbox.append({"method": method, "path": path, "summary": summary, "body": body, "id": fake, **extra})
        return {"id": fake}

    def upsert_summary(self, repo: str, number: int, body: str, comment_id: int | None, marker: str) -> int:
        return self._record("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}, "post summary",
                            number=number)["id"]

    def posted_markers(self, repo: str, number: int, pattern: str) -> dict[str, int]:
        return {}

    def post_review(self, repo: str, number: int, sha: str, comments: list[dict], body: str) -> list[dict]:
        out = []
        for c in comments:
            rid = self._record("POST", f"/repos/{repo}/pulls/{number}/comments", {"body": c["body"]},
                               "inline comment", number=number)["id"]
            self.outbox[-1]["comment_id"] = rid
            out.append({"path": c["path"], "line": c["line"], "id": rid, "body": c["body"]})
        return out

    def reply(self, repo: str, number: int, comment: dict, body: str) -> int:
        return self._record("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}, "reply",
                            number=number)["id"]

    def replied(self, repo: str, number: int, marker: str) -> bool:
        return any(marker in (m["body"].get("body") or "") for m in self.outbox)

    def react(self, repo: str, comment: dict, content: str) -> None:
        self._record("POST", f"/repos/{repo}/comments/{comment['id']}/reactions", {"content": content},
                     f"react {content}")

    def set_status(self, repo: str, sha: str, state: str, description: str, target_url: str | None = None) -> None:
        self._record("POST", f"/repos/{repo}/statuses/{sha}", {"state": state, "description": description,
                                                               "context": "gitzoid/review"}, f"status {state}")
