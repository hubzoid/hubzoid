"""Code, delivery and people reports. Each is a workflow with these inputs, so
the same report runs from chat, MCP, the Console or the CLI, on a person's
own schedule, or monthly for the team."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator

MAX_DAYS = 92


class ReportRequest(BaseModel):
    period: Literal["last_week", "last_month", "last_n_days", "custom"] = Field(
        "last_week", description="last_week, last_month, last_n_days (with days) or custom (with start and end)")
    days: int | None = Field(None, ge=1, le=MAX_DAYS, description="For last_n_days only: 1 to 92")
    start: date | None = Field(None, description="For custom only: first day, YYYY-MM-DD")
    end: date | None = Field(None, description="For custom only: last day, YYYY-MM-DD")
    group: str | None = Field(None, description="A digest group from settings, for its repos")
    repos: list[str] = Field(default_factory=list, description="Specific repos (owner/name). Empty with no group: every repo")

    @model_validator(mode="after")
    def _consistent(self):
        if self.period == "last_n_days" and not self.days:
            raise ValueError("last_n_days needs days (1 to 92)")
        if self.period == "custom":
            if not (self.start and self.end):
                raise ValueError("custom needs start and end dates")
            if self.end < self.start:
                raise ValueError("end is before start")
            if (self.end - self.start).days + 1 > 366:
                raise ValueError("a custom period is at most 366 days")
        if self.group and self.repos:
            raise ValueError("give a group or repos, not both")
        return self

    @property
    def rolling(self) -> bool:
        return self.period != "custom"


def period(req: ReportRequest, now: datetime) -> tuple[datetime, datetime, str]:
    """[start, end) in UTC and a label. last_week is the last full Monday to
    Sunday; last_month the last full calendar month."""
    today = now.astimezone(timezone.utc).date()
    if req.period == "last_week":
        monday = today - timedelta(days=today.weekday() + 7)
        start, end = monday, monday + timedelta(days=7)
    elif req.period == "last_month":
        first = today.replace(day=1)
        start = (first - timedelta(days=1)).replace(day=1)
        end = first
    elif req.period == "last_n_days":
        start, end = today - timedelta(days=req.days), today
    else:
        start, end = req.start, req.end + timedelta(days=1)
    label = f"{start:%d %b %Y} to {(end - timedelta(days=1)):%d %b %Y}"
    as_dt = lambda d: datetime.combine(d, time.min, tzinfo=timezone.utc)  # noqa: E731
    return as_dt(start), as_dt(end), label


def previous(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    return start - (end - start), start


def scope(req: ReportRequest, settings) -> tuple[str, list[str]]:
    """The report's name and repos, limited to this agent's repos."""
    names = settings.repo_names()
    if req.group:
        if req.group not in settings.digest_groups:
            raise ValueError(f"no digest group {req.group!r}; groups: {', '.join(settings.digest_groups) or 'none'}")
        return req.group, list(settings.digest_groups[req.group].repos)
    if req.repos:
        unknown = [r for r in req.repos if r not in names]
        if unknown:
            raise ValueError(f"not GitZoid repos here: {', '.join(unknown)}")
        return ", ".join(req.repos) if len(req.repos) <= 3 else f"{len(req.repos)} repos", list(req.repos)
    return "all repos", names


def slug(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "all"


def monthly_groups(settings, kind: str) -> list[str]:
    return [g for g, grp in settings.digest_groups.items() if kind in grp.monthly_reports]


def manager_groups(attrs: dict, settings) -> list[str]:
    """The digest groups a manager may see people data for, set by an admin as
    the access attribute gitzoid_groups (a list, or names separated by commas)."""
    raw = (attrs or {}).get("gitzoid_groups") or []
    names = [x.strip() for x in (raw.split(",") if isinstance(raw, str) else raw) if str(x).strip()]
    return [g for g in names if g in settings.digest_groups]


def narrative_prompt(kind: str, name: str, label: str, numbers: dict, evidence: list | None = None, coverage: list | None = None) -> str:
    import json

    from .untrusted import fence
    return (f"Write the headline and highlights for the {kind} report of {name}, {label}. Quote only the numbers "
            "below; do not compute new ones. Plain words for a manager. Hours can be written as days when large.\n\n"
            f"<team_numbers>\n{fence(json.dumps(numbers, indent=1, default=str))}\n</team_numbers>"
            "\nExplain concrete code changes, risks and useful follow-ups from these PRs, citing owner/repo#number. "
            "Say merged, not shipped: deployment is unknown. Separate observations from hypotheses. "
            "Current open PRs are a snapshot now; merged PRs are restricted to the selected period. "
            "File names suggesting tests do not establish test coverage or execution.\n"
            + fence(json.dumps({"evidence": evidence or [], "limitations": coverage or []}, default=str)))


NARRATIVE_SYSTEM = "You summarise engineering numbers honestly and briefly. Never invent numbers."
