"""Every finding has a structural identity and a state, kept across pushes.

Identity: repo, PR, category or rule, file, enclosing function and the code
around the line (its anchor). The model's wording is only for display, so a
reworded finding on unchanged code is the same finding.

States: open, fixed, deferred, dismissed, outdated. After each review:
  - a finding found again keeps its identity and state (a dismissed one stays
    dismissed and is never posted again);
  - an open finding not found again stays open while its code is unchanged,
    and becomes fixed only after an explicit resolution check; outdated if its file is gone;
  - a fixed finding found again on the same code is reopened.
"""
from __future__ import annotations

import hashlib

from .diff import anchor, enclosing_function, find_anchor

SUPPRESSED = ("dismissed", "deferred")
NEARBY = 20


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def enrich(f: dict, text: str | None) -> dict:
    f = dict(f)
    f["function"] = enclosing_function(text, f.get("line"))
    f["anchor"] = anchor(text, f.get("line"))
    return f


def identity(repo: str, number: int, f: dict) -> str:
    where = f.get("anchor") or _norm(f.get("body", ""))[:120]
    if f.get("checked"):  # code-checked findings have fixed wording: two on one line stay two
        where += "|" + _norm(f.get("body", ""))[:200]
    key = "|".join([repo, str(number), f.get("category", ""), f.get("rule", ""), f.get("path", ""),
                    f.get("function", ""), where])
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _match(f: dict, prior: list[dict], taken: set[str]) -> dict | None:
    exact = next((p for p in prior if p["id"] == f["id"] and p["id"] not in taken), None)
    if exact:
        return exact
    if not f.get("path") or not f.get("function"):
        return None
    near = [p for p in prior if p["id"] not in taken and p["state"] != "fixed" and p.get("path") == f["path"]
            and p.get("category") == f.get("category") and p.get("function") == f["function"]
            and (p.get("rule") or "") == (f.get("rule") or "")
            and abs((p.get("line") or 0) - (f.get("line") or 0)) <= NEARBY]
    return min(near, key=lambda p: abs((p.get("line") or 0) - (f.get("line") or 0))) if near else None


def reconcile(repo: str, number: int, sha: str, prior: list[dict], found: list[dict],
              head_text, removed: set[str], resolved: dict[str, str] | None = None) -> dict:
    """`found` are this review's gated findings, already enriched. `head_text`
    is a callable path -> text at the new head (or None). Returns the rows to
    save, grouped by what happened to each."""
    out = {"new": [], "still": [], "fixed": [], "outdated": [], "suppressed": [], "reopened": []}
    taken: set[str] = set()
    seen: set[str] = set()
    for f in found:
        f = {**f, "id": identity(repo, number, f)}
        if f["id"] in seen:
            continue
        seen.add(f["id"])
        p = _match(f, prior, taken)
        if p is None:
            out["new"].append({**f, "repo": repo, "number": number, "state": "open", "first_sha": sha, "last_sha": sha})
            continue
        taken.add(p["id"])
        merged = {**p, "line": f.get("line"), "anchor": f.get("anchor") or p.get("anchor"),
                  "severity": f.get("severity", p.get("severity")), "body": p.get("body") or f.get("body"),
                  "last_sha": sha}
        if p["state"] in SUPPRESSED:
            out["suppressed"].append(merged)
        elif p["state"] == "fixed":
            out["reopened"].append({**merged, "state": "open"})
        else:
            out["still"].append({**merged, "state": "open"})
    for p in prior:
        if p["id"] in taken or p["state"] != "open":
            continue
        if p.get("path") in removed:
            out["outdated"].append({**p, "state": "outdated", "last_sha": sha})
            continue
        if not p.get("path") or not p.get("anchor"):
            out["still"].append({**p, "last_sha": sha})
            continue
        text = head_text(p["path"])
        if text is None:
            out["still"].append(p)
            continue
        line = find_anchor(text, p["anchor"])
        if p["id"] in (resolved or {}):
            out["fixed"].append({**p, "state": "fixed", "last_sha": sha, "reason": resolved[p["id"]]})
        elif line is None:
            out["still"].append({**p, "last_sha": sha, "reason": "Code changed; resolution not confirmed"})
        else:
            out["still"].append({**p, "line": line, "last_sha": sha})
    return out


def blocking(f: dict) -> bool:
    """A finding that holds the merge gate: open, high, a bug or security issue."""
    return f.get("state") == "open" and f.get("severity") == "high" and f.get("category") in ("bug", "security")
