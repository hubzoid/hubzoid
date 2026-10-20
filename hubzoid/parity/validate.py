"""Validate a parity bundle and measure its coverage.

``load(root, manifest_path)`` reads the manifest and every file it names
(paths stay inside the deployment folder; symlinks out, ``..``, absolute paths
and credential folders are refused). ``check(bundle)`` returns a report:

* ``errors`` stop a candidate from existing (bad schema, unsafe path, a
  sensitive value, an unknown source, a duplicate obligation, an owner outside
  the 2.0 map, an incomplete acceptance case),
* ``blockers`` stop a freeze but not a candidate (missing evidence, an
  unmapped locator, an unverified upstream identity, a changed source file, a
  required provider without an adapter owner, an incomplete deployment
  attestation),
* ``counts`` use the counting unit of the ledger: one obligation is one
  testable outcome; browser, provider and runtime variants are cases, not
  features.

The same functions back the CLI and the API, so both give the same digest and
the same failures for the same input.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..control import canonical, deploy, yamlfile
from . import models

MAX_ARTIFACT = 10 * 1024 * 1024
MAX_RECORD = 256 * 1024
MAX_OBLIGATIONS = 100_000

_RESERVED = ("keys", ".env", "restricted", ".ssh", ".aws", "credentials", "secrets")
_SECRET_KEY = re.compile(r"(secret|token|password|passwd|api[_-]?key|private[_-]?key|credential|client[_-]?secret)",
                         re.I)
_SECRET_VALUE = re.compile(r"^(sk-|ghp_|gho_|xox[abp]-|AKIA|eyJ)|-----BEGIN", re.I)
_SAFE_SECRET_VALUES = (None, True, False, "present", "absent", "unset", "redacted", "set", "missing")


class ParityError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = {k: v for k, v in extra.items() if v is not None}


@dataclass
class Bundle:
    manifest: dict
    ledger: list[dict]
    sources: dict
    providers: list[dict]
    deployments: dict[str, dict]
    errors: list[dict] = field(default_factory=list)

    def canonical(self) -> dict:
        return {"manifest": self.manifest,
                "ledger": sorted(self.ledger, key=lambda o: str(o.get("id"))),
                "sources": self.sources,
                "providers": sorted(self.providers, key=lambda p: str(p.get("id"))),
                "deployments": {k: self.deployments[k] for k in sorted(self.deployments)}}

    def digest(self) -> str:
        return canonical.digest(self.canonical())


def _err(code: str, message: str, **extra) -> dict:
    return {"code": code, "message": message, **{k: v for k, v in extra.items() if v is not None}}


def _reserved(parts) -> bool:
    return any(str(p).lower() in _RESERVED for p in parts)


def safe_path(root: Path, rel: str) -> Path:
    """``rel`` under ``root``: no absolute paths, ``..``, symlinks or
    credential locations, checked on the path as written and as resolved."""
    if _reserved(Path(rel).parts):
        raise ParityError(422, "unsafe_path", f"{rel} is in a credential location. Copy a redacted file under "
                                              "parity/input instead.", path=rel)
    try:
        target = deploy.resolve_inside(root, rel)
    except ValueError as exc:
        raise ParityError(422, "unsafe_path", f"{rel}: {exc} Copy the file under the deployment folder.",
                          path=rel) from exc
    base = Path(root).resolve()
    walk = Path(root)
    for part in Path(rel).parts:
        walk = walk / part
        if walk.is_symlink():
            raise ParityError(422, "unsafe_path", f"{rel} goes through a symbolic link. Copy the file instead.",
                              path=rel)
    if _reserved(target.relative_to(base).parts):
        raise ParityError(422, "unsafe_path", f"{rel} is in a credential location.", path=rel)
    return target


def _read(root: Path, rel: str) -> bytes:
    path = safe_path(root, rel)
    if not path.is_file():
        raise ParityError(422, "missing_evidence", f"{rel} does not exist.", path=rel)
    data = path.read_bytes()
    if len(data) > MAX_ARTIFACT:
        raise ParityError(413, "too_large", f"{rel} is larger than 10 MiB. Split it into parts.", path=rel)
    return data


def parse_jsonl(data: bytes, where: str) -> list[dict]:
    out = []
    for n, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        if len(line) > MAX_RECORD:
            raise ParityError(413, "too_large", f"{where} line {n} is larger than 256 KiB.", path=where, line=n)
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise ParityError(422, "invalid_schema", f"{where} line {n} is not JSON: {exc.msg}.", path=where,
                              line=n) from exc
        if not isinstance(record, dict):
            raise ParityError(422, "invalid_schema", f"{where} line {n} is not an object.", path=where, line=n)
        out.append(record)
        if len(out) > MAX_OBLIGATIONS:
            raise ParityError(413, "too_large", f"{where} has more than 100,000 records.", path=where)
    return out


def load(root: Path, manifest_path: Path | str = "parity.yaml") -> Bundle:
    root = Path(root)
    rel = str(manifest_path)
    if Path(rel).is_absolute():
        try:
            rel = str(Path(rel).resolve().relative_to(root.resolve()))
        except ValueError:
            raise ParityError(422, "unsafe_path", "The manifest must be inside the deployment folder.")
    try:
        loaded = yamlfile.loads(_read(root, rel).decode("utf-8"), path=rel)
    except yamlfile.YamlError as exc:
        raise ParityError(422, "invalid_schema", exc.message, path=exc.path, line=exc.line)
    try:
        manifest = models.Manifest.model_validate(loaded.data).model_dump(mode="json")
    except ValidationError as exc:
        e = exc.errors()[0]
        raise ParityError(422, "invalid_schema", f"parity.yaml: {e.get('msg')} at "
                          + "/".join(str(x) for x in e.get("loc", ())), path=rel)
    ledger = parse_jsonl(_read(root, manifest["ledger"]), manifest["ledger"])
    try:
        sources = json.loads(_read(root, manifest["sources"]))
    except ValueError as exc:
        raise ParityError(422, "invalid_schema", f"{manifest['sources']} is not JSON.") from exc
    providers = parse_jsonl(_read(root, manifest["providers"]), manifest["providers"])
    deployments = {}
    for ref in manifest["deployments"]:
        try:
            deployments[ref["id"]] = json.loads(_read(root, ref["inventory"]))
        except ValueError as exc:
            raise ParityError(422, "invalid_schema", f"{ref['inventory']} is not JSON.") from exc
    return Bundle(manifest, ledger, sources, providers, deployments)


_CREDENTIAL_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s@]*:[^/\s@]*@", re.I)
_SECRET_QUERY = re.compile(r"[?&](token|access_token|api[_-]?key|key|secret|password|sig|signature|auth)=",
                           re.I)
_MAX_SETTING_TEXT = 500


def _unsafe_value(key: str, value: Any, path: str, out: list[tuple[str, str]], depth: int = 0) -> None:
    """Collect every part of a setting value outside the safe representations:
    plain scalars and short text with no credential inside, and for secret
    keys only the presence words."""
    if depth > 6:
        out.append((path, "nests too deeply"))
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _unsafe_value(f"{key}.{k}", v, f"{path}/{k}", out, depth + 1)
        return
    if isinstance(value, list):
        for i, v in enumerate(value):
            _unsafe_value(key, v, f"{path}/{i}", out, depth + 1)
        return
    if _SECRET_KEY.search(key) and value not in _SAFE_SECRET_VALUES:
        out.append((path, "is a secret setting; record only present, absent or redacted"))
        return
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            out.append((path, "looks like a credential"))
        elif _CREDENTIAL_URL.search(value):
            out.append((path, "is a URL with a user name or password in it; record it without them"))
        elif _SECRET_QUERY.search(value):
            out.append((path, "is a URL carrying a token or key; record it without the query"))
        elif len(value) > _MAX_SETTING_TEXT:
            out.append((path, f"is longer than {_MAX_SETTING_TEXT} characters; record a summary"))
    elif value is not None and not isinstance(value, (bool, int, float)):
        out.append((path, "is not a plain value"))


def _sensitive(dep_id: str, inventory: dict) -> list[dict]:
    found = []
    for i, row in enumerate(inventory.get("settings") or []):
        if not isinstance(row, dict):
            continue
        key = str(row.get("key", ""))
        for field_name in ("declared_value", "upstream_default", "sdk_default", "persisted_override",
                           "effective_value"):
            bad: list[tuple[str, str]] = []
            _unsafe_value(key, row.get(field_name), f"/deployments/{dep_id}/settings/{i}/{field_name}", bad)
            for pointer, why in bad:
                found.append(_err("sensitive_field", f"{dep_id}: settings[{i}] {key}.{field_name} {why}.",
                                  pointer=pointer))
    return found


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ARCHIVE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _immutable(s: models.Source) -> bool:
    if not s.files:
        return False
    if s.identity_kind == "upstream_commit":
        return bool(s.upstream_commit and _COMMIT.match(s.upstream_commit))
    if s.identity_kind == "archive":
        return bool(s.archive_digest and _ARCHIVE.match(s.archive_digest))
    return False


def source_root_of(root: Path, manifest: dict) -> Path | None:
    rel = manifest.get("source_root")
    return safe_path(Path(root), rel) if rel else None


def check(bundle: Bundle, *, source_root: Path | None = None, require_sources: bool = False) -> dict:
    errors: list[dict] = list(bundle.errors)
    blockers: list[dict] = []
    obligations: list[models.Obligation] = []
    for i, raw in enumerate(bundle.ledger):
        try:
            obligations.append(models.Obligation.model_validate(raw))
        except ValidationError as exc:
            e = exc.errors()[0]
            loc = "/".join(str(x) for x in e.get("loc", ()))
            code = "invalid_owner" if loc.startswith("owner_issue") else "invalid_schema"
            errors.append(_err(code, f"ledger record {raw.get('id', i)}: {e.get('msg')} at {loc}",
                               pointer=f"/ledger/{i}/{loc}"))
    try:
        sources = models.SourcesFile.model_validate(bundle.sources)
    except ValidationError as exc:
        e = exc.errors()[0]
        errors.append(_err("invalid_schema", f"sources: {e.get('msg')} at {'/'.join(map(str, e.get('loc', ())))}"))
        sources = models.SourcesFile(version=1, sources=[])
    providers = []
    for i, raw in enumerate(bundle.providers):
        try:
            providers.append(models.Provider.model_validate(raw))
        except ValidationError as exc:
            errors.append(_err("invalid_schema", f"provider {raw.get('id', i)}: {exc.errors()[0].get('msg')}"))
    deployments = {}
    for dep_id, inv in bundle.deployments.items():
        errors.extend(_sensitive(dep_id, inv))
        try:
            deployments[dep_id] = models.Deployment.model_validate(inv)
        except ValidationError as exc:
            e = exc.errors()[0]
            errors.append(_err("invalid_schema", f"deployment {dep_id}: {e.get('msg')} at "
                               + "/".join(map(str, e.get("loc", ())))))

    source_ids = {s.id for s in sources.sources}
    ids: dict[str, int] = {}
    semantics: dict[tuple, str] = {}
    for o in obligations:
        if o.id in ids:
            errors.append(_err("duplicate_obligation", f"{o.id} appears twice.", ids=[o.id]))
        ids[o.id] = 1
        key = (o.outcome.strip().lower(), o.authority_context.strip().lower(),
               o.configuration_context.strip().lower())
        if key in semantics:
            errors.append(_err("duplicate_obligation",
                               f"{o.id} and {semantics[key]} describe the same outcome in the same context. Merge "
                               "them and keep the other as an alias.", ids=[semantics[key], o.id]))
        semantics[key] = o.id
        for occ in o.source_occurrences:
            if occ.source not in source_ids:
                errors.append(_err("unknown_source", f"{o.id} cites source {occ.source!r}, which has no record.",
                                   ids=[o.id]))
        for case in o.acceptance:
            if not (case.selector and case.fixture and case.expected and case.path):
                errors.append(_err("invalid_fixture", f"{o.id} case {case.id} needs a path, selector, fixture and "
                                   "expected result.", ids=[o.id]))
        if not o.acceptance:
            errors.append(_err("invalid_fixture", f"{o.id} has no acceptance case.", ids=[o.id]))
        if o.migration.kind == "approved_omission":
            if o.migration.founder_decision is None:
                errors.append(_err("invalid_schema", f"{o.id}: an approved omission needs the founder decision "
                                   "that approved it.", ids=[o.id]))
            elif o.migration.founder_decision == "F4" and o.migration.omitted_destination != "community":
                errors.append(_err("invalid_schema", f"{o.id}: F4 omits only the Open WebUI Community destination.",
                                   ids=[o.id]))
        if o.release_scope == "non_release":
            continue  # dated evidence with a reason; never gates the release
        if o.status == "evidence_missing" or not o.source_occurrences:
            blockers.append(_err("missing_evidence", f"{o.id} has no verified source evidence yet.", ids=[o.id]))
        if o.status == "exception_pending":
            blockers.append(_err("missing_evidence", f"{o.id} waits for a founder disposition.", ids=[o.id]))

    # Coverage: every locator maps to an obligation or carries a reasoned classification.
    cited = {(occ.source, occ.path, occ.line_start) for o in obligations for occ in o.source_occurrences}
    cited_paths = {(occ.source, occ.path) for o in obligations for occ in o.source_occurrences}
    unmapped = []
    for loc in sources.locators:
        if loc.classification:
            continue
        if (loc.source, loc.path, loc.line) in cited or (loc.line is None and (loc.source, loc.path) in cited_paths):
            continue
        unmapped.append(loc.id)
    if unmapped:
        blockers.append(_err("coverage_gap", f"{len(unmapped)} source locator(s) map to no obligation.",
                             locators=unmapped[:200]))

    # Source identity and drift.
    for s in sources.sources:
        if s.identity_kind == "parent_repo_head":
            errors.append(_err("invalid_schema", f"source {s.id}: a parent repository's HEAD is not the reference's "
                               "own identity. Record the archive digest or the upstream commit."))
        if not s.upstream_verified:
            blockers.append(_err("missing_evidence", f"source {s.id} is not matched to an immutable upstream commit "
                                 "yet.", source=s.id))
        elif not _immutable(s):
            errors.append(_err("invalid_schema", f"source {s.id} says upstream_verified but records no immutable "
                               "identity: a 40-character upstream_commit or a sha256: archive_digest, and the "
                               "hash of every inspected file.", source=s.id))
        if source_root is None:
            if require_sources:
                blockers.append(_err("source_unverified", f"source {s.id}: its files were not re-hashed. Set "
                                     "source_root in parity.yaml (or pass --source-root) to verify them.",
                                     source=s.id))
        else:
            changed = []
            for rel, expected in s.files.items():
                try:
                    path = safe_path(Path(source_root), rel)
                except ParityError:
                    errors.append(_err("unsafe_path", f"source {s.id}: {rel} is not a safe relative path.",
                                       source=s.id))
                    continue
                actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
                if actual != expected:
                    changed.append(rel)
            if changed:
                blockers.append(_err("source_changed", f"source {s.id}: {len(changed)} file(s) changed since they "
                                     "were inspected. Inspect them again.", files=changed[:50]))

    for p in providers:
        selected = p.disposition in ("required", "key_provider")
        if selected and p.customer_required and not p.adapter_owner:
            blockers.append(_err("missing_evidence", f"provider {p.id} is required but has no adapter owner.",
                                 provider=p.id))
        if p.adapter_owner and p.adapter_owner not in models.OWNERS:
            errors.append(_err("invalid_owner", f"provider {p.id}: {p.adapter_owner} is not a 2.0 owner."))

    for ref in bundle.manifest.get("deployments", []):
        dep = deployments.get(ref["id"])
        if dep is None:
            continue
        if dep.attestation.status != "complete":
            blockers.append(_err("missing_evidence", f"deployment {ref['id']}: the owner has not attested a complete "
                                 "inventory.", deployment=ref["id"]))
        unknown = [r.key for r in dep.settings if r.evidence_kind == "unknown"]
        if unknown:
            blockers.append(_err("missing_evidence", f"deployment {ref['id']}: {len(unknown)} setting(s) unknown.",
                                 deployment=ref["id"], keys=unknown[:50]))
        unknown_counts = [c.category for c in dep.categories if c.count == "unknown"]
        if unknown_counts:
            blockers.append(_err("missing_evidence", f"deployment {ref['id']}: {len(unknown_counts)} object "
                                 "categor(ies) not counted.", deployment=ref["id"], categories=unknown_counts))

    by_owner: dict[str, int] = {}
    for o in obligations:
        if o.release_scope == "selected":
            by_owner[o.owner_issue] = by_owner.get(o.owner_issue, 0) + 1
    settings_summary = {}
    for dep_id, dep in deployments.items():
        kinds: dict[str, int] = {}
        for r in dep.settings:
            kinds[r.evidence_kind] = kinds.get(r.evidence_kind, 0) + 1
        settings_summary[dep_id] = kinds
    return {
        "valid": not errors,
        "digest": bundle.digest(),
        "counts": {"obligations": len(obligations),
                   "selected": sum(1 for o in obligations if o.release_scope == "selected"),
                   "non_release": sum(1 for o in obligations if o.release_scope == "non_release"),
                   "by_owner": dict(sorted(by_owner.items())),
                   "acceptance_cases": sum(len(o.acceptance) for o in obligations),
                   "sources": len(sources.sources), "locators": len(sources.locators),
                   "unmapped_locators": len(unmapped), "providers": len(providers),
                   "deployments": len(deployments), "settings_by_evidence": settings_summary,
                   "missing_owner": 0, "blockers": len(blockers), "errors": len(errors)},
        "errors": errors,
        "blockers": blockers,
        "status": "invalid" if errors else ("needs_evidence" if blockers else "ready_for_review"),
        "note": "Ledger completeness only. This is not a product parity pass.",
    }


def ineffective(bundle: Bundle) -> list[dict]:
    """Settings whose intended effect differs from the observed effect."""
    out = []
    for dep_id, inv in bundle.deployments.items():
        for row in inv.get("settings") or []:
            if row.get("evidence_kind") == "ineffective" or (
                    row.get("declared_value") is not None and row.get("effective_value") is not None
                    and row.get("declared_value") != row.get("effective_value")):
                out.append({"deployment": dep_id, "key": row.get("key"), "declared": row.get("declared_value"),
                            "effective": row.get("effective_value"), "mapping": row.get("mapping")})
    return out
