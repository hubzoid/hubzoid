"""Open-core license keys for Hubzoid's enterprise (`ee/`) features.

The open core runs free for everyone. Enterprise features check
``load_license().has_feature(name)`` before activating; with no valid key the
runtime stays on the community tier and those features stay dark.

Keys are ED25519-signed and verified **offline** (works air-gapped):

  * The PRIVATE signing key lives only with Hubzoid (a secret manager), never
    in this repo. It is used by ``hubzoid license issue`` to mint a customer key.
  * The PUBLIC key is embedded in the shipped code (``EMBEDDED_PUBLIC_KEY``) or
    supplied via ``HUBZOID_LICENSE_PUBKEY``. It can only *verify*, never forge,
    so it is safe to publish.

A customer key is a compact token ``<payload_b64>.<signature_b64>`` where the
payload is JSON: ``{"customer", "tier", "features": [...], "expiry": "YYYY-MM-DD"}``.
Customers set ``LICENSE_KEY=<token>`` in their hub's ``.env``.

This is a speed bump backed by a contract, not DRM: the source is visible, so a
determined party could patch out the check. The protection is legal (the
Hubzoid Enterprise License) plus practical (real buyers will not run forked,
unsupported code). The check only has to make paying the easier path.

NOTE: distinct from ``_signing.py`` (the symmetric HMAC used for artifact
download links). Licensing uses its own asymmetric keypair on purpose.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import date

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# Production public key. EMPTY in the open-source repo on purpose: an operator
# runs `hubzoid license keygen`, keeps the private key secret, and pastes the
# public key here (or sets HUBZOID_LICENSE_PUBKEY). With no public key
# configured, the runtime is community-only and cannot validate any key.
EMBEDDED_PUBLIC_KEY = "xYfzpDv9InktZ294WzDqfQMfrgpwZbJdIvaSj7dj-gI"


class InvalidLicense(Exception):
    """Raised by verify() when a token cannot be trusted."""


@dataclass(frozen=True)
class License:
    tier: str
    customer: str | None
    features: tuple[str, ...]
    expiry: date | None
    valid: bool
    reason: str

    def has_feature(self, name: str) -> bool:
        """True only for a valid license that grants ``name`` (``*`` = all)."""
        if not self.valid:
            return False
        return "*" in self.features or name in self.features


COMMUNITY = License(
    tier="community",
    customer=None,
    features=(),
    expiry=None,
    valid=True,
    reason="no license key (community tier)",
)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def generate_keypair() -> tuple[str, str]:
    """Return a fresh ``(private_b64, public_b64)`` ED25519 keypair."""
    priv = ed25519.Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return _b64(priv_raw), _b64(pub_raw)


def issue(payload: dict, private_key_b64: str) -> str:
    """Sign ``payload`` with the private key; return the ``token``."""
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(_unb64(private_key_b64))
    payload_b64 = _b64(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    sig = priv.sign(payload_b64.encode("ascii"))
    return f"{payload_b64}.{_b64(sig)}"


def verify(token: str, public_key_b64: str) -> dict:
    """Verify ``token`` against the public key; return the payload or raise."""
    if not public_key_b64:
        raise InvalidLicense("no public key configured")
    if "." not in token:
        raise InvalidLicense("malformed token")
    payload_b64, sig_b64 = token.split(".", 1)
    try:
        pub = ed25519.Ed25519PublicKey.from_public_bytes(_unb64(public_key_b64))
    except Exception as exc:  # noqa: BLE001
        raise InvalidLicense("malformed public key") from exc
    try:
        pub.verify(_unb64(sig_b64), payload_b64.encode("ascii"))
    except InvalidSignature as exc:
        raise InvalidLicense("invalid signature") from exc
    except Exception as exc:  # noqa: BLE001
        raise InvalidLicense("malformed signature") from exc
    try:
        return json.loads(_unb64(payload_b64))
    except Exception as exc:  # noqa: BLE001
        raise InvalidLicense("malformed payload") from exc


def load_license(
    token: str | None = None,
    public_key_b64: str | None = None,
    today: date | None = None,
) -> License:
    """Load the active license. Never raises: a bad key degrades to invalid.

    ``token`` defaults to ``$LICENSE_KEY``; ``public_key_b64`` defaults to
    ``$HUBZOID_LICENSE_PUBKEY`` then ``EMBEDDED_PUBLIC_KEY``.
    """
    if token is None:
        token = os.environ.get("LICENSE_KEY", "").strip()
    if not token:
        return COMMUNITY

    if public_key_b64 is None:
        public_key_b64 = (
            os.environ.get("HUBZOID_LICENSE_PUBKEY", "").strip() or EMBEDDED_PUBLIC_KEY
        )

    try:
        payload = verify(token, public_key_b64)
    except InvalidLicense as exc:
        return License("invalid", None, (), None, valid=False, reason=str(exc))

    customer = payload.get("customer")
    tier = payload.get("tier", "enterprise")
    features = tuple(payload.get("features", []))

    expiry: date | None = None
    raw_expiry = payload.get("expiry")
    if raw_expiry:
        try:
            expiry = date.fromisoformat(str(raw_expiry))
        except ValueError:
            return License(
                tier, customer, features, None, valid=False, reason="bad expiry format"
            )

    today = today or date.today()
    if expiry is not None and today > expiry:
        return License(
            tier,
            customer,
            features,
            expiry,
            valid=False,
            reason=f"expired on {expiry.isoformat()}",
        )

    return License(tier, customer, features, expiry, valid=True, reason="ok")
