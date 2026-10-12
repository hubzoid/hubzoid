"""Team numbers from merged and open pull requests. Plain code: the model may
quote these numbers in a narrative but never computes them.

Definitions (after DevLake and Middleware):
  cycle time        opened to merged
  first review      opened to the first review by someone other than the author
  size              additions + deletions: S < 50, M < 250, L < 1000, XL above
  stuck             open, not a draft, no update for 3 days
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median

from .github import ts

STUCK_DAYS = 3


def hours(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    return round((ts(b) - ts(a)).total_seconds() / 3600, 1)


def _med(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return round(median(values), 1) if values else None


def _p75(values: list[float]) -> float | None:
    values = sorted(v for v in values if v is not None)
    return values[min(len(values) - 1, int(len(values) * 0.75))] if values else None


def size(p: dict) -> str:
    n = (p.get("additions") or 0) + (p.get("deletions") or 0)
    return "S" if n < 50 else "M" if n < 250 else "L" if n < 1000 else "XL"


def human_review(r: dict, p: dict) -> bool:
    return (r.get("author") != p.get("author") and r.get("author_type") != "Bot"
            and not (r.get("author") or "").endswith("[bot]")
            and "<!-- gitzoid:" not in (r.get("body") or "").lower()
            and not (r.get("body") or "").startswith("GitZoid found ")
            and r.get("state") in ("COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED"))


def first_review(p: dict) -> str | None:
    times = sorted(r["submitted_at"] for r in p.get("reviews") or []
                   if r.get("submitted_at") and human_review(r, p))
    return times[0] if times else None


def delivery(merged: list[dict], open_prs: list[dict], now: datetime) -> dict:
    cycle = [hours(p["created_at"], p["merged_at"]) for p in merged]
    review = [hours(p["created_at"], first_review(p)) for p in merged if first_review(p)]
    stuck = [p for p in open_prs if not p.get("draft") and ts(p["updated_at"]) < now - timedelta(days=STUCK_DAYS)]
    by_week: Counter = Counter()
    for p in merged:
        d = ts(p["merged_at"])
        by_week[(d - timedelta(days=d.weekday())).date().isoformat()] += 1
    return {
        "merged": len(merged),
        "cycle_hours_median": _med(cycle), "cycle_hours_p75": _p75(cycle),
        "first_review_hours_median": _med(review),
        "unreviewed_merges": sum(1 for p in merged if not first_review(p)),
        "open": len([p for p in open_prs if not p.get("draft")]), "stuck": len(stuck),
        "stuck_list": [{"repo": p["repo"], "number": p["number"], "title": p["title"], "url": p.get("url", ""),
                        "days": (now - ts(p["updated_at"])).days} for p in sorted(stuck, key=lambda p: p["updated_at"])][:15],
        "by_week": dict(sorted(by_week.items())),
        "by_repo": dict(Counter(p["repo"] for p in merged).most_common()),
    }


def code(merged: list[dict], events: list[dict]) -> dict:
    areas: Counter = Counter()
    tests = 0
    for p in merged:
        files = [f["filename"] for f in p.get("files") or []]
        if any("test" in f.lower() for f in files):
            tests += 1
        for f in files:
            areas[f.split("/", 1)[0] if "/" in f else "(root)"] += 1
    states = Counter(e["event"] for e in events)
    return {
        "merged": len(merged),
        "additions": sum(p.get("additions") or 0 for p in merged),
        "deletions": sum(p.get("deletions") or 0 for p in merged),
        "files_changed": sum(p.get("changed_files") or len(p.get("files") or []) for p in merged),
        "sizes": dict(Counter(size(p) for p in merged)),
        "reverts": sum(1 for p in merged if p["title"].lower().startswith("revert")),
        "with_tests": tests,
        "top_areas": areas.most_common(8),
        "gitzoid": {"opened": states.get("opened", 0), "fixed": states.get("fixed", 0),
                    "dismissed": states.get("dismissed", 0), "deferred": states.get("deferred", 0)},
    }


def people(merged: list[dict], open_prs: list[dict]) -> list[dict]:
    """Counts per person: merged, reviews given, reviews waiting on them. No
    scores, no rankings."""
    rows: dict[str, dict] = defaultdict(lambda: {"merged": 0, "reviews_given": 0, "waiting_on": 0, "open": 0,
                                                  "cycle": []})
    for p in merged:
        rows[p["author"]]["merged"] += 1
        rows[p["author"]]["cycle"].append(hours(p["created_at"], p["merged_at"]))
        for r in {r["author"] for r in p.get("reviews") or [] if human_review(r, p)}:
            rows[r]["reviews_given"] += 1
    for p in open_prs:
        if not p.get("draft"):
            rows[p["author"]]["open"] += 1
        for who in p.get("requested") or []:
            rows[who]["waiting_on"] += 1
    return [{"person": k, "merged": v["merged"], "reviews_given": v["reviews_given"], "waiting_on": v["waiting_on"],
             "open": v["open"], "cycle_hours_median": _med(v["cycle"])}
            for k, v in sorted(rows.items()) if not k.endswith("[bot]")]


def change(now_value, before_value) -> str:
    if now_value is None or before_value in (None, 0):
        return ""
    pct = round((now_value - before_value) / before_value * 100)
    return f"{'+' if pct >= 0 else ''}{pct}%"
