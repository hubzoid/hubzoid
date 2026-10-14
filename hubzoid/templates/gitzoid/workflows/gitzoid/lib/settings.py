"""The `gitzoid:` block of workflows/settings.yaml, validated.

Workflows pass `hub.setting("gitzoid")`; the chat tools read the file. Every
key is optional and the defaults below are what an empty block means. A wrong
value fails with a message that names the key, so a typo never silently turns
a check off.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Severity = Literal["high", "medium", "low"]
Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
StatusCheck = Literal["off", "report", "gate"]
REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SAMPLE_REPO = "kestrel-oak/storefront"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepoSettings(_Strict):
    name: str
    severity_floor: Severity | None = None
    model: str | None = None
    context: str = ""
    commands: bool | None = None
    status_check: StatusCheck | None = None
    exclude_paths: list[str] = []   # added to security.exclude_paths for this repo

    @field_validator("name")
    @classmethod
    def _owner_name(cls, v: str) -> str:
        if not REPO_NAME.match(v):
            raise ValueError(f"{v!r} is not owner/name")
        return v


class SecurityGroup(_Strict):
    repos: list[str]
    email_list: str | None = None
    audit_day: Weekday = "wednesday"


class DigestGroup(_Strict):
    repos: list[str]
    email_list: str | None = None
    monthly_reports: list[Literal["code", "delivery"]] = []


class ReviewSettings(_Strict):
    severity_floor: Severity = "high"
    backlog: Literal["all", "latest", "new"] = "all"
    first_run_limit: int = Field(6, ge=0)
    max_prs_per_run: int = Field(5, ge=1)
    max_findings: int = Field(8, ge=1, le=30)
    max_age_days: int = Field(0, ge=0)
    max_diff_chars: int = Field(200_000, ge=5_000)  # code first; bigger changes are reviewed in part, and say so
    verify: bool = True
    post_to_github: bool = False
    summary_on_clean: bool = True
    auto_pause_after: int = Field(0, ge=0)
    skip_label: str = "gitzoid-skip"
    skip_drafts: bool = True
    skip_bots: bool = True
    status_check: StatusCheck = "report"
    model: str | None = None


class SecuritySettings(_Strict):
    severity_floor: Literal["critical", "high", "medium", "low"] = "high"
    model: str | None = None
    # Globs the security watch and the deep audit skip, on top of the built-in
    # test, sample, fixture, vendored and build folders. `docs/` or `docs/**`
    # is a folder; other patterns are fnmatch, where * also crosses `/`.
    exclude_paths: list[str] = []


class AuditSettings(_Strict):
    budget_minutes: int = Field(20, ge=1, le=120)
    safety_days: int = Field(8, ge=1)
    max_files_per_repo: int = Field(40, ge=1)
    verify: bool = True  # a second pass tries to refute each finding before it is recorded or emailed
    model: str | None = None


class DigestSettings(_Strict):
    days: int = Field(7, ge=1, le=31)
    poem: bool = False
    model: str | None = None


class ReportSettings(_Strict):
    people: bool = True
    evidence_prs: int = Field(100, ge=0, le=1000)
    evidence_chars_per_pr: int = Field(24000, ge=1000, le=200000)
    synthesis_chars: int = Field(90000, ge=5000, le=500000)
    model: str | None = None


class Limits(_Strict):
    model_calls_per_day: int = Field(400, ge=1)
    replies_per_person_per_hour: int = Field(10, ge=1)


class Analyzer(_Strict):
    name: str
    run: list[str] = Field(min_length=1)
    timeout: int = 120
    allow_project_code: bool = False


class Settings(_Strict):
    source: Literal["sample", "github"] = "sample"
    trigger: Literal["poll", "webhook"] = "poll"
    commands: bool = True
    context: str = ""
    repos: list[RepoSettings] = []
    security_groups: dict[str, SecurityGroup] = {}
    digest_groups: dict[str, DigestGroup] = {}
    owner_list: str | None = None
    review: ReviewSettings = ReviewSettings()
    security: SecuritySettings = SecuritySettings()
    audit: AuditSettings = AuditSettings()
    digest: DigestSettings = DigestSettings()
    reports: ReportSettings = ReportSettings()
    limits: Limits = Limits()
    analyzers: list[Analyzer] = []

    @field_validator("repos", mode="before")
    @classmethod
    def _names_or_dicts(cls, v):
        return [{"name": r} if isinstance(r, str) else r for r in (v or [])]

    @model_validator(mode="after")
    def _groups_name_known_repos(self):
        names = self.repo_names()
        for kind, groups in (("security_groups", self.security_groups), ("digest_groups", self.digest_groups)):
            for gname, group in groups.items():
                if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", gname):
                    raise ValueError(f"{kind}.{gname}: group names are lowercase letters, digits and '-'")
                if kind == "security_groups" and gname == "unassigned":
                    raise ValueError("security_groups.unassigned is reserved for repos in no group; choose another name")
                unknown = [r for r in group.repos if r not in names]
                if unknown:
                    raise ValueError(f"{kind}.{gname} names repos not under repos: {', '.join(unknown)}")
        owner: dict[str, str] = {}
        for gname, group in self.security_groups.items():
            for r in group.repos:
                if r in owner:
                    raise ValueError(f"{r} is in security groups {owner[r]} and {gname}; a repo belongs to at most one")
                owner[r] = gname
        return self

    def repo_names(self) -> list[str]:
        return [r.name for r in self.repos]

    def repo(self, name: str) -> RepoSettings:
        for r in self.repos:
            if r.name == name:
                return r
        raise KeyError(f"{name} is not one of GitZoid's repos")

    def floor_for(self, name: str) -> Severity:
        return self.repo(name).severity_floor or self.review.severity_floor

    def commands_for(self, name: str) -> bool:
        r = self.repo(name)
        return self.commands if r.commands is None else r.commands

    def status_for(self, name: str) -> StatusCheck:
        return self.repo(name).status_check or self.review.status_check

    def exclude_for(self, name: str) -> list[str]:
        """The globs the security watch and deep audit skip in this repo."""
        return [*self.security.exclude_paths, *(self.repo(name).exclude_paths if name in self.repo_names() else [])]

    def model_for(self, name: str) -> str | None:
        return self.repo(name).model or self.review.model

    def security_group_of(self, name: str) -> str | None:
        return next((g for g, grp in self.security_groups.items() if name in grp.repos), None)

    def security_scope(self) -> dict[str, SecurityGroup]:
        """Every repo is watched: repos in no group alert to owner_list."""
        groups = dict(self.security_groups)
        rest = [r for r in self.repo_names() if self.security_group_of(r) is None]
        if rest:
            groups["unassigned"] = SecurityGroup(repos=rest, email_list=self.owner_list)
        return groups


class SettingsError(ValueError):
    pass


def load(raw: dict | None) -> Settings:
    try:
        return Settings.model_validate(raw or {})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'gitzoid'}: {e['msg']}" for e in exc.errors())
        raise SettingsError(f"workflows/settings.yaml gitzoid: {problems}") from None


def read_file(hub_dir: Path) -> tuple[Settings, dict]:
    """The gitzoid block and the whole file (for email_lists), for chat tools."""
    path = Path(hub_dir) / "workflows" / "settings.yaml"
    data = (yaml.safe_load(path.read_text()) or {}) if path.is_file() else {}
    return load(data.get("gitzoid")), data
