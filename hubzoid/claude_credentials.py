"""Bounded Claude credential selection. No SDK, tokens in storage, or alerts.

The deployment's existing operational metadata coordinates cooldown and one
recovery probe across bridges. SDK construction stays in factory_claude.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text

from . import db, migrations

log = logging.getLogger(__name__)
TOKEN_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY",
              "CLAUDE_CODE_OAUTH_TOKEN_TERTIARY")
COOLDOWN = 300
PROBE_LEASE = 300


def redact(value: str) -> str:
    for key in TOKEN_KEYS:
        token = os.environ.get(key, "").strip()
        if token:
            value = value.replace(token, "[redacted]")
    return value


class Unavailable(RuntimeError):
    def __init__(self, kind="usage_limit", reset_at=None):
        self.kind, self.reset_at = kind, reset_at
        self.api_error_status = 401 if kind == "auth" else 429
        detail = "Claude authentication_error" if kind == "auth" else "Claude usage limit reached"
        if kind == "usage_limit" and reset_at:
            detail += f"|{int(reset_at)}"
        super().__init__(detail)


@dataclass(frozen=True)
class Credential:
    slot: int
    token: str = field(repr=False)

    @property
    def key(self):
        return "claude_credential:" + hashlib.sha256(self.token.encode()).hexdigest()


class Pool:
    def __init__(self, engine, credentials):
        self.engine, self.credentials = engine, credentials

    @classmethod
    def configured(cls, hub_dir):
        tokens = [os.environ.get(key, "").strip() for key in TOKEN_KEYS]
        if not any(tokens[1:]):
            return None  # preserve the single-credential/login path exactly
        if not tokens[0]:
            raise ValueError("Claude backup credentials require CLAUDE_CODE_OAUTH_TOKEN")
        if hub_dir is None:
            raise ValueError("Claude credential failover requires a Hub directory")
        if any(os.environ.get(k, "").strip() for k in (
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")) or any(
                os.environ.get(k, "").lower() in {"1", "true", "yes", "on"} for k in (
                    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")):
            raise ValueError("Claude OAuth failover cannot be combined with API-key or alternate-provider authentication")
        credentials, seen = [], set()
        for slot, token in enumerate(tokens):
            if token and token not in seen:
                credentials.append(Credential(slot, token))
                seen.add(token)
        engine = db.operational_engine(hub_dir)
        migrations.upgrade(engine, "operational")
        return cls(engine, credentials)

    def _read(self, key):
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT v FROM hz_meta WHERE k=:k"), {"k": key}).scalar()

    def _cas(self, key, previous, value):
        with self.engine.begin() as conn:
            if previous is None:
                result = conn.execute(text("INSERT INTO hz_meta(k,v) VALUES (:k,:v) "
                                           "ON CONFLICT(k) DO NOTHING"), {"k": key, "v": value})
            elif value is None:
                result = conn.execute(text("DELETE FROM hz_meta WHERE k=:k AND v=:old"),
                                      {"k": key, "old": previous})
            else:
                result = conn.execute(text("UPDATE hz_meta SET v=:v WHERE k=:k AND v=:old"),
                                      {"k": key, "v": value, "old": previous})
            return result.rowcount == 1

    def select(self, attempted):
        now = time.time()
        for credential in self.credentials:
            if credential.key in attempted:
                continue
            raw = self._read(credential.key)
            if raw is None:
                return credential, None
            state = json.loads(raw)
            if state["kind"] == "auth" or state["until"] > now or state.get("lease", 0) > now:
                continue
            # Only one bridge tests a recovered credential. Other turns continue
            # down the ordered list without waiting behind a model request.
            claimed = json.dumps({**state, "lease": now + PROBE_LEASE, "probe": uuid.uuid4().hex})
            if self._cas(credential.key, raw, claimed):
                return credential, claimed
        return None

    def failed(self, credential, failure):
        now = time.time()
        until = failure.reset_at if failure.reset_at and failure.reset_at > now else now + COOLDOWN
        for _ in range(8):
            raw = self._read(credential.key)
            old = json.loads(raw) if raw else {}
            kind = "auth" if "auth" in (old.get("kind"), failure.kind) else "usage_limit"
            value = json.dumps({"kind": kind, "until": max(until, old.get("until", 0))})
            if self._cas(credential.key, raw, value):
                log.warning("Claude credential slot %s unavailable (%s)", credential.slot, kind)
                return
        raise RuntimeError("Claude credential cooldown changed concurrently; please retry")

    def recovered(self, credential, claim):
        if claim and self._cas(credential.key, claim, None):
            log.info("Claude credential slot %s recovered", credential.slot)

    def release(self, credential, claim):
        if claim:
            # An inconclusive/cancelled probe is not proof of recovery. A crash
            # expires its lease; a clean stop keeps a short cooldown instead.
            state = json.loads(claim)
            self._cas(credential.key, claim, json.dumps({
                "kind": state["kind"], "until": max(state["until"], time.time() + COOLDOWN)}))

    def unavailable(self):
        states = [json.loads(raw) for c in self.credentials if (raw := self._read(c.key))]
        resets = [max(s["until"], s.get("lease", 0)) for s in states if s["kind"] == "usage_limit"]
        return Unavailable("usage_limit" if resets else "auth", min(resets) if resets else None)
