"""Team rules, all of them files.

Hub rules: one Markdown file each in knowledge/review-rules/, shared by every
repo and surface. Front matter (all optional):

    title: Nothing retries forever
    paths: ["app/**"]          # where it applies; empty means everywhere
    severity: high             # the most a finding under this rule can be
    pattern: 'while\\s+True:'  # also checked by code on added lines

Repo rules: `.gitzoid/review.md`, else `review.md`, read from the repo's
default branch so a pull request cannot change the rules that review it.
Front matter: skip, severity_floor, ignore, focus, skip_title, base_branches,
paths (glob: instruction). Conventions come from CLAUDE.md, AGENTS.md or
CONTRIBUTING.md, plus .cursorrules and Copilot instructions.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from pathlib import Path

import yaml

from .diff import added_lines

RULES_DIR = Path("knowledge") / "review-rules"
GUIDE = Path("knowledge") / "review-guide.md"
REVIEW_MD = (".gitzoid/review.md", "review.md")
CONVENTIONS = ("CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md")
EXTRA_CONVENTIONS = (".cursorrules", ".github/copilot-instructions.md")
MAX_BLOCK = 6000
SEVERITIES = ("high", "medium", "low")


def front_matter(text: str) -> tuple[dict, str]:
    m = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", text or "", re.S)
    if not m:
        return {}, (text or "").strip()
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        data = None
    return (data if isinstance(data, dict) else {}), text[m.end():].strip()


def cap(text: str, limit: int = MAX_BLOCK) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n[... cut at {limit} characters]"


def match_glob(path: str, pattern: str) -> bool:
    """`docs/` and `docs/**` match the folder; a bare `docs` matches only a
    file named docs. Other patterns are fnmatch, where * also crosses `/`."""
    pattern = (pattern or "").strip()
    if not pattern:
        return False
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return fnmatch.fnmatchcase(path, pattern)


def _list(v, limit: int = 20) -> list[str]:
    return [str(x)[:200] for x in (v if isinstance(v, list) else [v] if v else [])][:limit]


# ---------------------------------------------------------------- hub rules

def hub_rules(hub_dir: Path) -> list[dict]:
    out = []
    folder = Path(hub_dir) / RULES_DIR
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        meta, body = front_matter(path.read_text())
        severity = str(meta.get("severity") or "high").lower()
        pattern = meta.get("pattern")
        try:
            compiled = re.compile(pattern) if pattern else None
        except re.error:
            compiled = None
        out.append({"file": path.relative_to(hub_dir).as_posix(), "id": path.stem,
                    "title": str(meta.get("title") or path.stem.replace("-", " ")),
                    "paths": _list(meta.get("paths")), "severity": severity if severity in SEVERITIES else "high",
                    "pattern": pattern if compiled else None, "text": cap(body, 3000)})
    return out


def rules_for(rules: list[dict], path: str) -> list[dict]:
    return [r for r in rules if not r["paths"] or any(match_glob(path, g) for g in r["paths"])]


def code_checks(rules: list[dict], files: list[dict]) -> list[dict]:
    """Findings from rules with a `pattern`, on added lines of matching files."""
    out = []
    for r in rules:
        if not r["pattern"]:
            continue
        pattern = re.compile(r["pattern"])
        for f in files:
            if r["paths"] and not any(match_glob(f["filename"], g) for g in r["paths"]):
                continue
            for line, text in added_lines(f.get("patch") or ""):
                if pattern.search(text):
                    out.append({"path": f["filename"], "line": line, "severity": r["severity"], "confidence": "high",
                                "category": "convention", "rule": r["title"],
                                "body": f"This breaks the team rule \"{r['title']}\". {r['text'].splitlines()[0] if r['text'] else ''}".strip()})
    return out


def guide(hub_dir: Path) -> str:
    path = Path(hub_dir) / GUIDE
    return front_matter(path.read_text())[1] if path.is_file() else ""


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "rule"


def write_rule(hub_dir: Path, title: str, text: str, paths: list[str], approved_by: str | None) -> str:
    folder = Path(hub_dir) / RULES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    base, n = slug(title), 1
    path = folder / f"{base}.md"
    while path.exists():
        n += 1
        path = folder / f"{base}-{n}.md"
    meta = {"title": title, "paths": paths, "severity": "high"}
    if approved_by:
        meta["approved_by"] = approved_by
    path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False).strip() + "\n---\n\n" + text.strip() + "\n")
    return path.relative_to(hub_dir).as_posix()


# ---------------------------------------------------------------- repo rules

def parse_review_md(text: str) -> dict:
    """A malformed front matter is ignored, never fatal: the repo is reviewed as usual."""
    meta, body = front_matter(text or "")
    floor = str(meta.get("severity_floor") or "").lower()
    paths = meta.get("paths") if isinstance(meta.get("paths"), dict) else {}
    return {
        "skip": meta.get("skip") is True,
        "severity_floor": floor if floor in SEVERITIES else None,
        "ignore": _list(meta.get("ignore")),
        "focus": _list(meta.get("focus")),
        "skip_title": [s.lower() for s in _list(meta.get("skip_title"))],
        "base_branches": _list(meta.get("base_branches")),
        "path_instructions": {str(k)[:200]: str(v)[:500] for k, v in list(paths.items())[:20]},
        "instructions": cap(body.strip()),
    }


def repo_controls(gh, repo: str) -> dict:
    """The repo's own review.md and conventions, from its default branch."""
    controls = parse_review_md("")
    for path in REVIEW_MD:
        text = gh.file(repo, path)
        if text is not None:
            controls = {**parse_review_md(text), "review_md": path}
            break
    blocks = []
    for path in CONVENTIONS:
        text = gh.file(repo, path)
        if text:
            blocks.append((path, cap(text)))
            break
    for path in EXTRA_CONVENTIONS:
        text = gh.file(repo, path)
        if text:
            blocks.append((path, cap(text, 3000)))
    controls["conventions"] = blocks
    return controls


def skip_reason(pr: dict, review_cfg, controls: dict) -> str | None:
    if controls.get("skip"):
        return f"the repo's {controls.get('review_md', 'review.md')} sets skip: true"
    if review_cfg.skip_drafts and pr.get("draft"):
        return "draft"
    if review_cfg.skip_bots and (pr.get("author_type") == "Bot" or pr.get("author", "").endswith("[bot]")):
        return f"opened by a bot ({pr['author']})"
    if review_cfg.skip_label and review_cfg.skip_label in pr.get("labels", []):
        return f"labelled {review_cfg.skip_label}"
    title = (pr.get("title") or "").lower()
    word = next((w for w in controls.get("skip_title", []) if w and w in title), None)
    if word:
        return f"title contains {word!r} (skip_title in review.md)"
    branches = controls.get("base_branches") or []
    if branches and pr.get("base_ref") not in branches:
        return f"targets {pr.get('base_ref')}, not {', '.join(branches)}"
    return None


def reviewable(files: list[dict], controls: dict) -> tuple[list[dict], list[str]]:
    keep, ignored = [], []
    for f in files:
        (ignored if any(match_glob(f["filename"], g) for g in controls.get("ignore", [])) else keep).append(f)
    return keep, [f["filename"] for f in ignored]


def fingerprint(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]
