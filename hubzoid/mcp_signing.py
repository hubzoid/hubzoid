"""Signing keys and JWT access tokens for the hosted MCP authorization server.

Access tokens follow RFC 9068 (``typ: at+jwt``, RS256) so any verifier that
reads the issuer's discovery document can check them: AWS AgentCore's JWT
authorizer, a load balancer, another replica. Hubzoid still checks the grant
and the person's live access on every call (``mcp_oauth.HubOAuth``); the
signature only proves the token is ours and unexpired.

Keys (one set per deployment, in the operational database):

* ``HUBZOID_MCP_SIGNING_KEY``: a PEM RSA private key from the environment or the
  secret store. When set it is always the signer.
* otherwise the newest row of ``hz_mcp_signing_keys``, created on first use
  (private key encrypted with the deployment key, ``secretbox``).

Rotation (``hubzoid mcp signing-keys rotate``) adds a key that signs from then
on. The previous key keeps verifying for one access-token lifetime plus clock
skew, so nobody is signed out; ``--now`` stops it verifying at once.

How others do it: Agno's built-in MCP authorization server issues JWT access
tokens with FastMCP's ``JWTIssuer`` (HS256, rotation by ``kid``,
``agno/os/mcp_auth_builtin.py``). HS256 needs the shared secret to verify, so
an outside verifier cannot use it; we sign with RS256 and publish the public
keys. Refresh tokens, API keys and browser sessions stay opaque.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

import sqlalchemy as sa

ALG = "RS256"
TYP = "at+jwt"
SKEW = 60
ENV = "HUBZOID_MCP_SIGNING_KEY"
ENV_PREVIOUS = "HUBZOID_MCP_SIGNING_KEYS_PREVIOUS"
_CACHE_SECONDS = 30

_meta = sa.MetaData()
keys_table = sa.Table(
    "hz_mcp_signing_keys", _meta,
    sa.Column("kid", sa.String(64), primary_key=True),
    sa.Column("private_key", sa.Text, nullable=False),
    sa.Column("public_jwk", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("superseded_at", sa.Float),
    sa.Column("retired_at", sa.Float),
)


class SigningError(RuntimeError):
    """The signing keys cannot be read (database or deployment key)."""


def _engine(hub_dir: Path):
    from . import db, migrations

    engine = db.operational_engine(hub_dir)
    migrations.upgrade(engine, "operational")
    return engine


def _thumbprint(key) -> str:
    return key.thumbprint()


def _overlap(access_seconds: int) -> float:
    return access_seconds + SKEW


class KeyRing:
    """The signer and the keys that still verify, cached for a few seconds so a
    rotation on one replica reaches the others quickly."""

    def __init__(self, hub_dir: Path, access_seconds: int, env=None):
        self.hub_dir = Path(hub_dir)
        self.access_seconds = int(access_seconds)
        self.env = env if env is not None else os.environ
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._signer = None
        self._verifiers: dict[str, object] = {}

    # -- loading ---------------------------------------------------------------------
    def _env_key(self):
        raw = (self.env.get(ENV) or "").strip()
        if not raw:
            return None
        key = _pem(raw, ENV)
        if not key.is_private:
            raise SigningError(f"{ENV} must be a private key.")
        return key

    def _env_previous(self) -> list:
        """Operator-managed keys that still verify: PEMs (public or private)."""
        import re

        raw = (self.env.get(ENV_PREVIOUS) or "").replace("\\n", "\n")
        blocks = re.findall(r"-----BEGIN [A-Z ]+-----.+?-----END [A-Z ]+-----", raw, flags=re.S)
        return [_pem(b, ENV_PREVIOUS) for b in blocks]

    def _rows(self, conn):
        return conn.execute(sa.select(keys_table).where(keys_table.c.retired_at.is_(None))
                            .order_by(keys_table.c.created_at.desc(), keys_table.c.kid)).mappings().all()

    def _create(self, conn):
        from joserfc.jwk import RSAKey

        from . import secretbox

        key = RSAKey.generate_key(2048)
        kid = _thumbprint(key)
        conn.execute(keys_table.insert().values(
            kid=kid,
            private_key=secretbox.encrypt(self.hub_dir, key.as_pem(private=True)),
            public_jwk=json.dumps(key.as_dict(private=False)),
            created_at=time.time(), superseded_at=None, retired_at=None))
        return kid

    def _load(self) -> None:
        from joserfc.jwk import RSAKey

        from . import secretbox

        now = time.time()
        env_key = self._env_key()
        try:
            engine = _engine(self.hub_dir)
            with engine.begin() as conn:
                rows = self._rows(conn)
                if env_key is None and not [r for r in rows if r["superseded_at"] is None]:
                    self._create(conn)
                    rows = self._rows(conn)
        except sa.exc.SQLAlchemyError as exc:
            raise SigningError("The MCP signing keys can't be read.") from exc
        verifiers: dict[str, object] = {}
        signer = None
        for r in rows:
            if r["superseded_at"] is not None and r["superseded_at"] + _overlap(self.access_seconds) <= now:
                continue
            verifiers[r["kid"]] = RSAKey.import_key(json.loads(r["public_jwk"]))
            if signer is None and r["superseded_at"] is None and env_key is None:
                try:
                    signer = RSAKey.import_key(secretbox.decrypt(self.hub_dir, r["private_key"]))
                except Exception as exc:  # noqa: BLE001
                    raise SigningError("The MCP signing key can't be decrypted with the deployment key.") from exc
        if env_key is not None:
            # Operator-managed keys replace the database keys entirely.
            signer = env_key
            verifiers = {_thumbprint(env_key): RSAKey.import_key(env_key.as_dict(private=False))}
            for old in self._env_previous():
                verifiers[_thumbprint(old)] = RSAKey.import_key(old.as_dict(private=False))
        self._signer, self._verifiers, self._loaded_at = signer, verifiers, now

    def _fresh(self) -> None:
        with self._lock:
            if self._signer is None or time.time() - self._loaded_at > _CACHE_SECONDS:
                self._load()

    def ready(self) -> None:
        """Load the keys now (creating the first one if needed)."""
        self._fresh()

    def reload(self) -> None:
        with self._lock:
            self._load()

    # -- use ---------------------------------------------------------------------------
    def jwks(self) -> dict:
        self._fresh()
        out = []
        for kid, key in self._verifiers.items():
            jwk = dict(key.as_dict(private=False))
            jwk.update(kid=kid, use="sig", alg=ALG)
            out.append(jwk)
        return {"keys": out}

    def sign(self, claims: dict) -> str:
        from joserfc import jwt

        self._fresh()
        key = self._signer
        return jwt.encode({"alg": ALG, "typ": TYP, "kid": _thumbprint(key)}, claims, key)

    def verify(self, token: str, *, issuer: str, audience: str) -> dict | None:
        """The claims of a valid access token for this issuer and audience, else
        None. Only RS256 with one of our keys; ``typ`` must be ``at+jwt``."""
        from joserfc import jwt
        from joserfc.errors import JoseError
        from joserfc.jwk import KeySet

        self._fresh()
        try:
            header = json.loads(_b64(token.split(".", 1)[0]))
        except (ValueError, IndexError):
            return None
        kid = header.get("kid") if isinstance(header, dict) else None
        if header.get("alg") != ALG or str(header.get("typ", "")).lower() not in (TYP, "application/" + TYP):
            return None
        key = self._verifiers.get(kid)
        if key is None:
            # A key rotated in on another replica: look once more.
            self.reload()
            key = self._verifiers.get(kid)
            if key is None:
                return None
        try:
            decoded = jwt.decode(token, KeySet([key]), algorithms=[ALG])
            jwt.JWTClaimsRegistry(
                iss={"essential": True, "value": issuer},
                aud={"essential": True, "value": audience},
                exp={"essential": True}, iat={"essential": True},
                sub={"essential": True}, jti={"essential": True},
                leeway=SKEW,
            ).validate(decoded.claims)
        except (JoseError, ValueError, TypeError):
            return None
        return dict(decoded.claims)


def _pem(raw: str, name: str):
    from joserfc.jwk import RSAKey

    try:
        return RSAKey.import_key(raw.replace("\\n", "\n"))
    except Exception as exc:  # noqa: BLE001
        raise SigningError(f"{name} is not a PEM RSA key.") from exc


def _b64(part: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


_rings: dict[tuple[str, int], KeyRing] = {}
_rings_lock = threading.Lock()


def ring(hub_dir: Path, access_seconds: int) -> KeyRing:
    """The key ring of this deployment (shared by every hub of a gateway, since
    the operational database and the deployment key are shared)."""
    key = (str(Path(hub_dir).resolve()), int(access_seconds))
    with _rings_lock:
        found = _rings.get(key)
        if found is None:
            found = _rings[key] = KeyRing(Path(hub_dir), access_seconds)
        return found


def reset_cache() -> None:
    with _rings_lock:
        _rings.clear()


def new_jti() -> str:
    return secrets.token_urlsafe(16)


# ---- operator commands -------------------------------------------------------------------

def list_keys(hub_dir: Path) -> list[dict]:
    engine = _engine(Path(hub_dir))
    with engine.connect() as conn:
        rows = conn.execute(sa.select(keys_table.c.kid, keys_table.c.created_at, keys_table.c.superseded_at,
                                      keys_table.c.retired_at).order_by(keys_table.c.created_at.desc())).mappings()
        out = [dict(r) for r in rows]
    if (os.environ.get(ENV) or "").strip():
        out.insert(0, {"kid": "(from " + ENV + ")", "created_at": None, "superseded_at": None,
                       "retired_at": None})
    return out


def rotate(hub_dir: Path, *, now: bool = False) -> str:
    """Add a key that signs from now on. The others stop signing; they keep
    verifying for one access lifetime plus skew, or stop at once with ``now``."""
    hub_dir = Path(hub_dir)
    if (os.environ.get(ENV) or "").strip():
        raise SigningError(f"The signing key comes from {ENV}. To rotate it, move the current PEM to "
                           f"{ENV_PREVIOUS}, set a new one in {ENV} and restart.")
    ring_ = KeyRing(hub_dir, 0)
    engine = _engine(hub_dir)
    t = time.time()
    with engine.begin() as conn:
        conn.execute(keys_table.update().where(keys_table.c.superseded_at.is_(None))
                     .values(superseded_at=t))
        if now:
            conn.execute(keys_table.update().where(keys_table.c.retired_at.is_(None))
                         .values(retired_at=t))
        kid = ring_._create(conn)
    reset_cache()
    return kid
