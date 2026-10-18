"""Record shapes of the parity ledger (pydantic, unknown keys refused).

* ``Obligation``: one independently testable user or operator outcome under
  an explicit authority and configuration context, with exactly one owning
  issue, its source locations, a migration treatment and acceptance cases.
  ``status`` says whether the *evidence* is complete, never whether the
  feature passed.
* ``Source``: an inspected reference (release, archive digest, upstream
  commit when verified, file hashes, licence classification).
* ``Provider``: one independently operated provider or configuration branch.
* ``Deployment``: an owner-prepared, redacted inventory of one deployment's
  effective settings and object categories. Values are redacted enums or
  presence markers; unknown and absent stay different.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OBLIGATION_ID = re.compile(r"^HZP-\d{6}$")
ISSUE_ID = re.compile(r"^HUB-\d{1,4}$")
#: Issues an obligation may belong to (the 2.0 owner map).
OWNERS = frozenset(f"HUB-{n}" for n in (35, 36, 39, 42, 43, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80,
                                         81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 97, 98, 99,
                                         100, 101, 102, 103, 109))
RUNNERS = ("pytest", "playwright", "manual-device", "real-model")
MIGRATION_KINDS = ("preserve", "translate", "retire_mechanism", "approved_omission", "not_applicable")
STATUSES = ("evidence_missing", "mapped", "exception_pending", "exception_approved")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Occurrence(_Strict):
    source: str
    path: str
    line_start: int | None = None
    line_end: int | None = None
    symbol: str | None = None
    file_hash: str | None = None

    @field_validator("path")
    @classmethod
    def _relative(cls, v: str) -> str:
        if v.startswith("/") or ".." in v.split("/"):
            raise ValueError("source paths are relative to the source root")
        return v


class Acceptance(_Strict):
    id: str
    runner: Literal["pytest", "playwright", "manual-device", "real-model"]
    path: str
    selector: str
    fixture: str
    expected: str
    planned: bool = True


class Migration(_Strict):
    kind: Literal["preserve", "translate", "retire_mechanism", "approved_omission", "not_applicable"]
    destination_owner: str | None = None
    object_category: str | None = None
    verifier: str | None = None
    reason: str
    founder_decision: str | None = None
    omitted_destination: str | None = None


class Obligation(_Strict):
    id: str
    title: str
    outcome: str
    authority_context: str
    configuration_context: str
    source_occurrences: list[Occurrence]
    owner_issue: str
    consumer_issues: list[str] = Field(default_factory=list)
    native_destination: str
    default_semantics: str
    migration: Migration
    acceptance: list[Acceptance]
    status: Literal["evidence_missing", "mapped", "exception_pending", "exception_approved"]
    aliases: list[str] = Field(default_factory=list)
    #: "selected": a 2.0 release obligation. "non_release": kept as dated
    #: evidence with a reason (B037: providers beyond the key ones, deferred
    #: features); it never gates the release.
    release_scope: Literal["selected", "non_release"] = "selected"
    disposition_reason: str | None = None

    @model_validator(mode="after")
    def _scope(self):
        if self.release_scope == "non_release" and not self.disposition_reason:
            raise ValueError("a non_release obligation needs disposition_reason")
        return self

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not OBLIGATION_ID.match(v):
            raise ValueError("obligation ids are HZP- plus six digits")
        return v

    @field_validator("owner_issue")
    @classmethod
    def _owner(cls, v: str) -> str:
        if v not in OWNERS:
            raise ValueError(f"{v} is not an issue that owns chat parity")
        return v


class Source(_Strict):
    id: str
    repository: str
    release: str
    upstream_commit: str | None = None
    upstream_verified: bool = False
    identity_kind: Literal["upstream_commit", "archive", "parent_repo_head"] = "archive"
    archive_digest: str | None = None
    license: str
    files: dict[str, str] = Field(default_factory=dict)  # relative path -> sha256
    inspected_paths: list[str] = Field(default_factory=list)
    excluded_paths: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class Locator(_Strict):
    id: str
    source: str
    path: str
    line: int | None = None
    kind: Literal["appendix_row", "setting", "route", "model", "ui_action", "release_note"]
    classification: str | None = None  # a reasoned non-outcome, when no obligation maps it


class SourcesFile(_Strict):
    version: Literal[1]
    sources: list[Source]
    locators: list[Locator] = Field(default_factory=list)


class Provider(_Strict):
    id: str
    family: Literal["search", "speech", "realtime", "image", "storage", "retrieval", "model", "embedding",
                    "extraction", "notification", "other"]
    reference: str
    protocol: str | None = None
    customer_required: bool = False
    adapter_owner: str | None = None
    secret_reference_kind: str | None = None
    acceptance: list[str] = Field(default_factory=list)
    disposition: Literal["required", "key_provider", "deferred", "approved_omission"]
    decision: str | None = None


class SettingRow(_Strict):
    key: str
    declared_value: Any = None
    upstream_default: Any = None
    sdk_default: Any = None
    persisted_override: Any = None
    policy_scope: Literal["default", "deployment", "hub", "group", "user", "gateway_inherited"] = "default"
    effective_value: Any = None
    evidence_kind: Literal["observed", "declared", "absent", "unknown", "ineffective"]
    mapping: str | None = None
    unknown_reason: str | None = None

    @model_validator(mode="after")
    def _unknown(self):
        if self.evidence_kind == "unknown" and not self.unknown_reason:
            raise ValueError(f"{self.key}: unknown needs unknown_reason")
        return self


class CategoryRow(_Strict):
    category: str
    count: int | Literal["unknown"]


class Attestation(_Strict):
    status: Literal["complete", "partial", "missing"]
    by: str | None = None
    covered: list[str] = Field(default_factory=list)


class Deployment(_Strict):
    version: Literal[1]
    deployment_id: str
    sdk_version: str | None = None
    topology: Literal["single", "gateway"]
    hubs: list[str] = Field(default_factory=list)
    sign_in_methods: list[str] = Field(default_factory=list)
    settings: list[SettingRow] = Field(default_factory=list)
    categories: list[CategoryRow] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    attestation: Attestation

    @field_validator("deployment_id")
    @classmethod
    def _opaque(cls, v: str) -> str:
        if not re.fullmatch(r"dep-[a-z0-9-]{3,40}", v):
            raise ValueError("deployment ids are opaque: dep- plus lowercase letters, digits and '-'")
        return v


class DeploymentRef(_Strict):
    id: str
    inventory: str


class Manifest(_Strict):
    version: Literal[1]
    ledger: str
    sources: str
    providers: str
    deployments: list[DeploymentRef] = Field(default_factory=list)
    source_release: str
    report_dir: str = "parity/reports"
