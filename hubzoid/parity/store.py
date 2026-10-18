"""Parity candidates, reviews and immutable freezes.

A **candidate** is an exact bundle (manifest, ledger, sources, providers,
deployment inventories) with its digest and validation report. A **review**
binds an authorised person's decision to that digest. A **freeze** seals an
approved candidate with no blockers. A frozen bundle never changes: a later
discovery is a new candidate whose ``base_id`` points at the freeze, and
``diff`` shows what changed. A blocked candidate stays a valid record; it is
never turned into a freeze.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import sqlalchemy as sa

from ..control import deploy, idempotency
from .validate import Bundle, ParityError, check

metadata = sa.MetaData()

candidates = sa.Table(
    "hz_parity_candidates", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("deployment_id", sa.String(64), nullable=False),
    sa.Column("base_id", sa.String(64)),
    sa.Column("digest", sa.String(80), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("created_by", sa.String(320), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("bundle_json", sa.Text, nullable=False),
    sa.Column("report_json", sa.Text, nullable=False),
)

reviews = sa.Table(
    "hz_parity_reviews", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("candidate_id", sa.String(64), nullable=False),
    sa.Column("digest", sa.String(80), nullable=False),
    sa.Column("actor", sa.String(320), nullable=False),
    sa.Column("decision", sa.String(16), nullable=False),
    sa.Column("reason", sa.Text),
    sa.Column("signed", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)

freezes = sa.Table(
    "hz_parity_freezes", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("deployment_id", sa.String(64), nullable=False),
    sa.Column("candidate_id", sa.String(64), nullable=False),
    sa.Column("digest", sa.String(80), nullable=False),
    sa.Column("review_id", sa.String(64), nullable=False),
    sa.Column("actor", sa.String(320), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("candidate_id", name="hz_parity_freezes_candidate"),
)


def _bundle_from(data: dict) -> Bundle:
    return Bundle(data["manifest"], data["ledger"], data["sources"], data["providers"], data["deployments"])


class ParityService:
    def __init__(self, hub_dir: Path, engine=None):
        from .. import db, migrations

        self.hub_dir = Path(hub_dir)
        self.root = deploy.root(self.hub_dir)
        self.deployment_id = deploy.deployment_id(self.hub_dir)
        if engine is None:
            engine = db.operational_engine(self.hub_dir)
            migrations.upgrade(engine, "operational")
        self.engine = engine

    def create(self, bundle: Bundle, *, actor: str, base_id: str | None = None,
               idempotency_key: str | None = None) -> dict:
        report = check(bundle)
        if not report["valid"]:
            raise ParityError(422, "invalid_schema", "The bundle has errors. Fix them before making a candidate.",
                              errors=report["errors"][:50])
        data = bundle.canonical()

        def change(conn):
            if base_id is not None:
                base = conn.execute(sa.select(candidates.c.id).where(
                    candidates.c.id == base_id, candidates.c.deployment_id == self.deployment_id)).first()
                if base is None:
                    raise ParityError(404, "not_found", "The base candidate was not found.")
            cid = str(uuid.uuid4())
            status = "needs_review" if not report["blockers"] else "blocked"
            conn.execute(candidates.insert().values(
                id=cid, deployment_id=self.deployment_id, base_id=base_id, digest=report["digest"], status=status,
                revision=1, created_by=actor, created_at=time.time(), bundle_json=json.dumps(data),
                report_json=json.dumps(report)))
            from ..control import audit

            audit.write(conn, actor, "parity_candidate.created", subject=cid, detail={"digest": report["digest"]},
                        surface="parity")
            return 201, {"id": cid, "digest": report["digest"], "status": status, "revision": 1,
                         "blockers": report["blockers"], "counts": report["counts"]}

        return idempotency.run(self.engine, deployment_id=self.deployment_id, actor_id=actor,
                               route="POST /api/parity/candidates", key=idempotency_key,
                               body={"digest": report["digest"], "base_id": base_id}, change=change,
                               kind="parity.candidate").body

    def get(self, candidate_id: str) -> dict:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(candidates).where(candidates.c.id == candidate_id,
                                                           candidates.c.deployment_id == self.deployment_id)).first()
            if row is None:
                raise ParityError(404, "not_found", "That candidate was not found.")
            frozen = conn.execute(sa.select(freezes).where(freezes.c.candidate_id == candidate_id)).first()
            revs = conn.execute(sa.select(reviews).where(reviews.c.candidate_id == candidate_id)
                                .order_by(reviews.c.created_at)).all()
        report = json.loads(row.report_json)
        return {"id": row.id, "digest": row.digest, "status": "frozen" if frozen else row.status,
                "revision": row.revision, "base_id": row.base_id, "created_by": row.created_by,
                "created_at": row.created_at, "counts": report["counts"], "blockers": report["blockers"],
                "reviews": [{"id": r.id, "digest": r.digest, "decision": r.decision, "actor": r.actor,
                             "signed": bool(r.signed), "created_at": r.created_at} for r in revs],
                "freeze": None if frozen is None else {"freeze_id": frozen.id, "created_at": frozen.created_at},
                "note": report["note"]}

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(candidates.c.id).where(candidates.c.deployment_id == self.deployment_id)
                                .order_by(candidates.c.created_at.desc()).limit(100)).all()
        return [self.get(r.id) for r in rows]

    def bundle(self, candidate_id: str) -> Bundle:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(candidates.c.bundle_json).where(
                candidates.c.id == candidate_id, candidates.c.deployment_id == self.deployment_id)).first()
        if row is None:
            raise ParityError(404, "not_found", "That candidate was not found.")
        return _bundle_from(json.loads(row.bundle_json))

    def review(self, candidate_id: str, *, digest: str, expected_revision: int, decision: str, actor: str,
               reason: str | None = None, signed: bool = True, idempotency_key: str | None = None) -> dict:
        if decision not in ("approve", "reject"):
            raise ParityError(400, "invalid_request", "decision is approve or reject.")

        def change(conn):
            row = conn.execute(sa.select(candidates).where(candidates.c.id == candidate_id,
                                                           candidates.c.deployment_id == self.deployment_id)).first()
            if row is None:
                raise ParityError(404, "not_found", "That candidate was not found.")
            if row.digest != digest or row.revision != expected_revision:
                raise ParityError(409, "revision_conflict", "Review the candidate's current digest.",
                                  current_revision=row.revision, current_digest=row.digest)
            rid = str(uuid.uuid4())
            conn.execute(reviews.insert().values(id=rid, candidate_id=candidate_id, digest=digest, actor=actor,
                                                 decision=decision, reason=reason, signed=1 if signed else 0,
                                                 created_at=time.time()))
            from ..control import audit

            audit.write(conn, actor, "parity.reviewed", subject=candidate_id,
                        detail={"digest": digest, "decision": decision}, surface="parity")
            return 201, {"review_id": rid, "candidate_id": candidate_id, "digest": digest, "decision": decision,
                         "actor": actor, "signed": signed}

        return idempotency.run(self.engine, deployment_id=self.deployment_id, actor_id=actor,
                               route=f"POST /api/parity/candidates/{candidate_id}/reviews", key=idempotency_key,
                               body={"digest": digest, "decision": decision, "reason": reason},
                               change=change, kind="parity.review").body

    def freeze(self, candidate_id: str, *, digest: str, review_id: str, actor: str,
               idempotency_key: str | None = None) -> dict:
        def change(conn):
            row = conn.execute(sa.select(candidates).where(candidates.c.id == candidate_id,
                                                           candidates.c.deployment_id == self.deployment_id)).first()
            if row is None:
                raise ParityError(404, "not_found", "That candidate was not found.")
            if row.digest != digest:
                raise ParityError(409, "revision_conflict", "The digest is not this candidate's.",
                                  current_digest=row.digest)
            rv = conn.execute(sa.select(reviews).where(reviews.c.id == review_id,
                                                       reviews.c.candidate_id == candidate_id)).first()
            if rv is None or rv.decision != "approve" or rv.digest != digest:
                raise ParityError(409, "review_required", "Freezing needs an approving review of this exact digest.")
            if not rv.signed:
                raise ParityError(409, "review_required", "An unsigned offline review can't freeze. Publish the "
                                  "review through an authorised session.")
            if row.status == "blocked":
                raise ParityError(409, "missing_evidence", "The candidate still has blockers. Resolve them in a new "
                                  "candidate.", blockers=json.loads(row.report_json)["blockers"][:20])
            existing = conn.execute(sa.select(freezes).where(freezes.c.candidate_id == candidate_id)).first()
            if existing is not None:
                return 201, {"freeze_id": existing.id, "digest": existing.digest}
            fid = str(uuid.uuid4())
            conn.execute(freezes.insert().values(id=fid, deployment_id=self.deployment_id, candidate_id=candidate_id,
                                                 digest=digest, review_id=review_id, actor=actor,
                                                 created_at=time.time()))
            from ..control import audit

            audit.write(conn, actor, "parity.frozen", subject=candidate_id, detail={"digest": digest},
                        surface="parity")
            return 201, {"freeze_id": fid, "digest": digest}

        return idempotency.run(self.engine, deployment_id=self.deployment_id, actor_id=actor,
                               route=f"POST /api/parity/candidates/{candidate_id}/freeze", key=idempotency_key,
                               body={"digest": digest, "review_id": review_id}, change=change,
                               kind="parity.freeze").body

    def diff(self, base_id: str, candidate_id: str) -> dict:
        a = {o["id"]: o for o in self.bundle(base_id).ledger}
        b = {o["id"]: o for o in self.bundle(candidate_id).ledger}
        return {"base": base_id, "candidate": candidate_id,
                "added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
                "changed": sorted(k for k in set(a) & set(b) if a[k] != b[k])}


_services: dict[str, ParityService] = {}
_lock = threading.Lock()


def service_for(hub_dir: Path) -> ParityService:
    key = str(Path(hub_dir).resolve())
    with _lock:
        svc = _services.get(key)
        if svc is None:
            svc = ParityService(Path(hub_dir))
            _services[key] = svc
        return svc
