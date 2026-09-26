"""Layered configuration: deployment, hub and restricted-tool settings, each from
a local file or the process environment plus an optional AWS Secrets Manager
secret.

Precedence, lowest first. A later row overrides an earlier one, for the
processes it reaches.

    Layer              Source                     Named by
    environment        the process environment    (none)
    deployment secret  JSON secret in AWS         AWS_SECRET_NAME
    hub                <hub>/.env                 (none)
    hub secret         JSON secret in AWS         HUBZOID_HUB_SECRET_NAME in <hub>/.env
    restricted         <hub>/restricted/.env      (none)
    restricted secret  JSON secret in AWS         HUBZOID_RESTRICTED_SECRET_NAME in restricted/.env

A standalone hub (not registered in a gateway manifest) treats its own `.env` as
part of the deployment. There the deployment secret is applied after
`<hub>/.env` and wins over it, as in prs-facade. A hub registered in a gateway
takes the deployment secret from the manifest (`deployment_secret`). It is
applied before `<hub>/.env` and filtered to `BRIDGE_DEPLOYMENT_KEYS`. A hub
`.env` that names `AWS_SECRET_NAME` is ignored in that case, so one hub cannot
redirect deployment settings.

Within a layer the secret wins over the file. With no secret named, nothing here
imports boto3 or touches the network, and the files load exactly as before
(`override=True`, hub first, then restricted).

Values are read once, at process start. After rotating a secret, restart the
processes that read it.

Nothing in this module logs or raises with a value. Messages name the secret,
the layer, a key name or an AWS error class only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Mapping

log = logging.getLogger(__name__)

DEPLOYMENT, HUB, RESTRICTED = "deployment", "hub", "restricted"

# Layer names as recorded per load and shown by `layer_report`.
L_ENV = "environment"
L_DEPLOYMENT_SECRET = "deployment secret"
L_HUB = "hub"
L_HUB_SECRET = "hub secret"
L_RESTRICTED = "restricted"
L_RESTRICTED_SECRET = "restricted secret"

SECRET_NAME_KEYS = frozenset({"AWS_SECRET_NAME", "HUBZOID_HUB_SECRET_NAME", "HUBZOID_RESTRICTED_SECRET_NAME"})

# Set by `hubzoid gateway` in the environment of the bridges it launches. The
# gateway already resolved the deployment secret for them (and pinned per-hub
# values such as HUBZOID_PUBLIC_URL), so they must not fetch and re-apply it.
INHERITED_MARKER = "HUBZOID_DEPLOYMENT_SECRET_INHERITED"

# Deployment keys a bridge takes from the deployment secret. Everything else in
# that secret is for the gateway and Open WebUI only (OAuth client secrets and
# the like). `bridge_deployment_key` also accepts every HUBZOID_GATEWAY_ADMIN_*,
# OAUTH_*_ENCRYPTION_KEY and OTEL_* key.
BRIDGE_DEPLOYMENT_KEYS = frozenset({
    # Workflow identity, report links and email delivery (deployment-wide).
    "HUBZOID_WORKFLOW_USER",
    "HUBZOID_SMTP_HOST",
    "HUBZOID_SMTP_PORT",
    "HUBZOID_SMTP_USERNAME",
    "HUBZOID_SMTP_PASSWORD",
    "HUBZOID_SMTP_FROM",
    "HUBZOID_SMTP_STARTTLS",
    "HUBZOID_SMTP_SSL",
    "HUBZOID_SMTP_TIMEOUT",
    "HUBZOID_EMAIL_DELIVERY",
    "HUBZOID_ARTIFACT_LINK_DAYS",
    "HUBZOID_ARTIFACT_ALLOW_ORIGINS",
    "HUBZOID_GATEWAY_ADMIN_EMAIL",
    "HUBZOID_GATEWAY_ADMIN_PASSWORD",
    "WEBUI_SECRET_KEY",
    "OAUTH_SESSION_TOKEN_ENCRYPTION_KEY",
    "OAUTH_CLIENT_INFO_ENCRYPTION_KEY",
    "DATABASE_URL",
    "DATABASE_SCHEMA",
    "HUBZOID_OPERATIONAL_DB",
    "HUBZOID_PUBLIC_URL",
    "WEBUI_URL",
    "OWUI_NATIVE_MCP",
    "HUBZOID_OTEL_ENDPOINT",
})

# Blanked in agent child processes (the `claude` CLI and the stdio MCP servers
# it starts). These AWS keys carry credential material or point at a credential
# endpoint. AWS_PROFILE, AWS_REGION and the config file paths are selectors, not
# credentials, and a blank AWS_PROFILE makes boto3 raise ProfileNotFound.
AWS_CREDENTIAL_KEYS = frozenset({
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_BEARER_TOKEN_BEDROCK",
})
# Hubzoid and Open WebUI service secrets no agent child needs. Every
# OAUTH_*_ENCRYPTION_KEY is blanked too.
SERVICE_SECRET_KEYS = frozenset({
    "BRIDGE_API_KEYS",
    "HUBZOID_ARTIFACT_SECRET",
    "HUBZOID_GATEWAY_ADMIN_PASSWORD",
    "WEBUI_SECRET_KEY",
    "DATABASE_URL",
    "HUBZOID_OPERATIONAL_DB",
    "HUBZOID_DBOS_DB",
    "HUBZOID_SMTP_USERNAME",
    "HUBZOID_SMTP_PASSWORD",
})
# Restricted-layer keys a child keeps: the claude CLI's own auth and settings,
# and the basics every process needs.
_CHILD_KEEP_PREFIXES = ("ANTHROPIC_", "CLAUDE_")
_CHILD_KEEP_KEYS = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TZ"})
_VERTEX_KEYS = frozenset({"GOOGLE_APPLICATION_CREDENTIALS", "CLOUD_ML_REGION"})

# Keys a secret may not set: AWS settings (no credentials or chaining through a
# secret), the secret names, the gateway marker, and process-control keys that
# would let whoever can write a secret run code on the host.
_RESERVED_PREFIXES = ("AWS_", "LD_", "DYLD_")
_RESERVED_KEYS = SECRET_NAME_KEYS | {INHERITED_MARKER, "PATH", "PYTHONPATH", "PYTHONHOME",
                                     "PYTHONSTARTUP", "NODE_OPTIONS"}
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ARN_RE = re.compile(r"^arn:aws[\w-]*:secretsmanager:([a-z0-9-]+):")
_TRUTHY = ("1", "true", "yes", "on")

# Open WebUI and sign-in settings (the gateway's 0.9.x compatibility set). Such
# a key set in a hub-only layer of a standalone hub still reaches Open WebUI.
_OWUI_PREFIXES = ("WEBUI_", "OAUTH_", "OPENID_", "GOOGLE_CLIENT_", "MICROSOFT_CLIENT_", "GITHUB_CLIENT_")
_OWUI_KEYS = frozenset({"DEFAULT_USER_ROLE", "ENABLE_SIGNUP", "ENABLE_LOGIN_FORM", "ENABLE_OAUTH_SIGNUP"})


class SecretFetchError(RuntimeError):
    """A configured secret could not be loaded. Never carries a value."""

    def __init__(self, name: str, layer: str, reason: str):
        self.name, self.layer, self.reason = name, layer, reason
        super().__init__(f"Could not load the {layer} secret {name!r}: {reason}")


def _encryption_key(key: str) -> bool:
    return key.startswith("OAUTH_") and key.endswith("_ENCRYPTION_KEY")


def bridge_deployment_key(key: str) -> bool:
    """Whether a bridge takes this key from the deployment secret."""
    return (key in BRIDGE_DEPLOYMENT_KEYS or key.startswith("OTEL_")
            or key.startswith("HUBZOID_GATEWAY_ADMIN_") or _encryption_key(key))


def owui_setting(key: str) -> bool:
    """An Open WebUI or sign-in setting (WEBUI_*, OAUTH_*, ENABLE_SIGNUP and so on)."""
    return key != "WEBUI_NAME" and (key in _OWUI_KEYS or key.startswith(_OWUI_PREFIXES))


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def region_for(name: str, env: Mapping[str, str]) -> str | None:
    """The region to call: the one in a secret ARN, else AWS_REGION, else
    AWS_DEFAULT_REGION, else None (boto3's own configuration decides)."""
    match = _ARN_RE.match(name or "")
    if match:
        return match.group(1)
    return (env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "").strip() or None


def _client(region: str | None):
    """A Secrets Manager client from boto3's default credential chain. Tests
    replace this function. boto3 is imported here and nowhere else."""
    import boto3

    return boto3.session.Session().client("secretsmanager", region_name=region)


_HINTS = {
    "AccessDeniedException": "the AWS identity in use may not call secretsmanager:GetSecretValue "
                             "on this secret (or kms:Decrypt on its key)",
    "ResourceNotFoundException": "no secret with this name exists in the region",
    "DecryptionFailure": "KMS could not decrypt the secret (check kms:Decrypt on its key)",
    "InvalidRequestException": "the secret cannot be read now (it may be scheduled for deletion)",
    "NoCredentialsError": "no AWS credentials found (use an instance or task role, AWS_PROFILE, "
                          "or AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY)",
    "NoRegionError": "no region configured (set AWS_REGION)",
    "EndpointConnectionError": "Secrets Manager could not be reached",
    "ProfileNotFound": "the AWS profile named by AWS_PROFILE does not exist",
}


def _explain(code: str, region: str | None) -> str:
    hint = _HINTS.get(code)
    where = f" in {region}" if region else ""
    return f"{code}{where}" + (f" ({hint})" if hint else "")


def fetch(name: str, *, region: str | None, layer: str) -> dict[str, str]:
    """Read one JSON secret and return it as environment strings.

    Credentials come only from boto3's default chain. Raises SecretFetchError
    (naming the secret, the layer and the AWS error class, never a value) when
    the secret cannot be read or is not a flat JSON object."""
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        raise SecretFetchError(name, layer, "boto3 is not installed") from None
    try:
        response = _client(region).get_secret_value(SecretId=name)
    except ClientError as exc:
        code = ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code") or "ClientError"
        raise SecretFetchError(name, layer, _explain(str(code), region)) from None
    except BotoCoreError as exc:
        raise SecretFetchError(name, layer, _explain(type(exc).__name__, region)) from None
    except Exception as exc:  # noqa: BLE001 - reported by class only, never by message
        raise SecretFetchError(name, layer, _explain(type(exc).__name__, region)) from None

    raw = (response or {}).get("SecretString")
    if raw is None:
        raise SecretFetchError(name, layer, "the secret has no text value (binary secrets are not supported)")
    try:
        data = json.loads(raw)
    except ValueError:
        raise SecretFetchError(name, layer, "the secret is not valid JSON") from None
    if not isinstance(data, dict):
        raise SecretFetchError(name, layer, "the secret must be a JSON object of NAME: value pairs")
    values = _coerce(data, name=name, layer=layer)
    log.info("Loaded %d key(s) from the %s secret %r", len(values), layer, name)
    log.debug("Keys in the %s secret %r: %s", layer, name, ", ".join(sorted(values)))
    return values


def _coerce(data: dict, *, name: str, layer: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for index, (key, value) in enumerate(data.items(), start=1):
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise SecretFetchError(name, layer, f"key number {index} is not a valid environment variable name")
        if key.upper().startswith(_RESERVED_PREFIXES) or key in _RESERVED_KEYS:
            raise SecretFetchError(name, layer, f"key {key} may not be set from a secret "
                                                "(AWS settings, secret names and process-control keys are reserved)")
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (int, float)):
            text = str(value)
        elif isinstance(value, str):
            text = value
        else:
            kind = "null" if value is None else "a nested value"
            raise SecretFetchError(name, layer, f"the value of {key} is {kind}. "
                                                "Only strings, numbers and booleans are allowed")
        if "\x00" in text:
            raise SecretFetchError(name, layer, f"the value of {key} contains a NUL character")
        out[key] = text
    return out


_cache: dict[tuple[str, str | None], dict[str, str]] = {}
_lock = threading.Lock()
_disabled = 0
_in_use = False


def load_secret(name: str, *, region: str | None, layer: str) -> dict[str, str]:
    """`fetch`, once per process for each (name, region). Values are read at
    start, so a rotation needs a restart."""
    global _in_use
    key = (name, region)
    with _lock:
        cached = _cache.get(key)
    if cached is None:
        cached = fetch(name, region=region, layer=layer)
        with _lock:
            _cache[key] = cached
    _in_use = True
    return dict(cached)


def clear_cache() -> None:
    """Forget fetched secrets and recorded loads (for tests)."""
    global _in_use, _base_env
    with _lock:
        _cache.clear()
    _loads.clear()
    _once.clear()
    _in_use = False
    _base_env = None


def secrets_in_use() -> bool:
    """Whether this process loaded any AWS secret."""
    return _in_use


def fetch_enabled() -> bool:
    return _disabled == 0


@contextmanager
def fetching_disabled() -> Iterator[None]:
    """Within this block settings load files only and never call AWS."""
    global _disabled
    _disabled += 1
    try:
        yield
    finally:
        _disabled -= 1


# ---------------------------------------------------------------------------
# Applying layers (settings.load)
# ---------------------------------------------------------------------------
@dataclass
class _Load:
    order: list[str] = field(default_factory=list)
    values: dict[str, dict[str, str]] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)


# The process environment before the first hub layer was applied here.
_base_env: dict[str, str] | None = None
# The latest load per hub: which layer set which key, with the value applied.
_loads: dict[str, _Load] = {}
_once: set[str] = set()


def _log_once(level: int, message: str, *args) -> None:
    text = message % args if args else message
    if text not in _once:
        _once.add(text)
        log.log(level, text)


def _read_file(path: Path) -> dict[str, str]:
    """A dotenv file as load_dotenv(override=True) would apply it now."""
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    return {k: v for k, v in (dotenv_values(path) or {}).items() if v is not None}


@dataclass(frozen=True)
class Pointer:
    layer: str          # deployment | hub | restricted
    name: str
    region: str | None
    source: str         # where the name was found
    filtered: bool = False  # deployment secret applied through BRIDGE_DEPLOYMENT_KEYS


def registered(hub_dir: Path, env: Mapping[str, str], hub_file: Mapping[str, str] | None = None) -> bool:
    """Whether the hub belongs to a gateway deployment (has a manifest)."""
    if (Path(hub_dir) / ".hubzoid" / "deployment.json").exists():
        return True
    return bool(env.get("HUBZOID_DEPLOYMENT") or (hub_file or {}).get("HUBZOID_DEPLOYMENT"))


def _manifest(hub_dir: Path, env: Mapping[str, str]) -> dict:
    from . import deployment

    try:
        data = deployment.read(hub_dir, env)
    except Exception:  # noqa: BLE001 - manifest problems are reported by the database checks
        log.debug("deployment manifest unreadable while looking for a deployment secret", exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _manifest_pointer(hub_dir: Path, env: Mapping[str, str]) -> dict | None:
    pointer = _manifest(hub_dir, env).get("deployment_secret")
    if isinstance(pointer, dict) and pointer.get("name"):
        return pointer
    return None


def _public_url_for_hub(base: str, hub_dir: Path, env: Mapping[str, str]) -> str:
    """The per-hub public URL a gateway bridge advertises, `<base>/b/<slug>`,
    as `hubzoid gateway` pins it for the bridges it launches."""
    slug = ""
    for entry in _manifest(hub_dir, env).get("hubs") or []:
        try:
            if Path(entry["path"]).resolve() == Path(hub_dir).resolve():
                slug = str(entry.get("slug") or "")
                break
        except (KeyError, TypeError, OSError):
            continue
    if not slug:
        from .gateway import _slugify

        slug = _slugify(Path(hub_dir).resolve().name)
    return base.rstrip("/") + f"/b/{slug}"


def _start_env() -> dict[str, str]:
    return dict(os.environ) if _base_env is None else dict(_base_env)


def pointers(hub_dir: Path, env: Mapping[str, str] | None = None) -> list[Pointer]:
    """The secrets this hub's settings load, without fetching anything.

    `env` is the process environment before any hub layer (default: the
    environment this process had before its first hub load)."""
    hub_dir = Path(hub_dir)
    env = _start_env() if env is None else dict(env)
    hub_file = _read_file(hub_dir / ".env")
    restricted_file = _read_file(hub_dir / "restricted" / ".env")
    out: list[Pointer] = []
    if registered(hub_dir, env, hub_file):
        if not env.get(INHERITED_MARKER):
            manifest = _manifest_pointer(hub_dir, {**env, **hub_file})
            if manifest:
                out.append(Pointer(DEPLOYMENT, str(manifest["name"]), manifest.get("region") or None,
                                   "deployment manifest", filtered=True))
            elif (env.get("AWS_SECRET_NAME") or "").strip():
                name = env["AWS_SECRET_NAME"].strip()
                out.append(Pointer(DEPLOYMENT, name, region_for(name, env), "process environment", filtered=True))
    else:
        merged = {**env, **hub_file}
        name = (merged.get("AWS_SECRET_NAME") or "").strip()
        if name:
            source = str(hub_dir / ".env") if hub_file.get("AWS_SECRET_NAME") else "process environment"
            out.append(Pointer(DEPLOYMENT, name, region_for(name, merged), source))
    name = (hub_file.get("HUBZOID_HUB_SECRET_NAME") or "").strip()
    if name:
        out.append(Pointer(HUB, name, region_for(name, {**env, **hub_file}), str(hub_dir / ".env")))
    name = (restricted_file.get("HUBZOID_RESTRICTED_SECRET_NAME") or "").strip()
    if name:
        out.append(Pointer(RESTRICTED, name, region_for(name, {**env, **hub_file, **restricted_file}),
                           str(hub_dir / "restricted" / ".env")))
    return out


def apply_layers(hub_dir: Path, *, secrets: bool = True) -> None:
    """Apply the hub's layers to the process environment (settings.load).

    Files load exactly as before: `<hub>/.env`, then `restricted/.env`, both
    overriding. Secrets are fetched only when named and `secrets` is true.
    Raises SecretFetchError when a named secret cannot be loaded."""
    global _base_env
    env = os.environ
    if _base_env is None:
        _base_env = dict(env)
    start = dict(_base_env)
    hub_dir = Path(hub_dir)
    secrets = secrets and fetch_enabled()
    record = _Load()

    def apply(layer: str, values: Mapping[str, str], source: str,
              within: Mapping[str, str] | None = None, within_source: str = "") -> None:
        if not values:
            return
        for key in sorted(values):
            if within is not None and key in within and within[key] != values[key]:
                _log_once(logging.INFO, "hub %s: the %s sets %s over %s", hub_dir.name, layer, key, within_source)
        env.update(values)
        record.order.append(layer)
        record.values[layer] = dict(values)
        record.sources[layer] = source

    hub_path = hub_dir / ".env"
    restricted_path = hub_dir / "restricted" / ".env"
    hub_file = _read_file(hub_path)
    is_registered = registered(hub_dir, start, hub_file)
    deployed: dict[str, str] = {}

    # Deployment secret for a gateway hub: before the hub file, bridge keys only.
    if is_registered and secrets:
        deployed = {k: v for k, v in start.items() if k == "WEBUI_SECRET_KEY" or _encryption_key(k)}
        if hub_file.get("AWS_SECRET_NAME"):
            _log_once(logging.WARNING, "hub %s: AWS_SECRET_NAME in %s is ignored for a gateway hub. Name a "
                      "hub secret with HUBZOID_HUB_SECRET_NAME instead.", hub_dir.name, hub_path)
        pointer = next((p for p in pointers(hub_dir, start) if p.layer == DEPLOYMENT), None)
        if pointer is not None:
            values = load_secret(pointer.name, region=pointer.region, layer=DEPLOYMENT)
            kept = {k: v for k, v in values.items() if bridge_deployment_key(k)}
            if kept.get("HUBZOID_PUBLIC_URL"):
                # The secret holds the gateway's public base. A bridge links
                # through its own prefix, as the gateway pins for its bridges.
                kept["HUBZOID_PUBLIC_URL"] = _public_url_for_hub(kept["HUBZOID_PUBLIC_URL"], hub_dir,
                                                                 {**start, **hub_file})
            ignored = sorted(set(values) - set(kept))
            if ignored:
                _log_once(logging.INFO, "hub %s: deployment secret keys left to the gateway and Open WebUI: %s",
                          hub_dir.name, ", ".join(ignored))
            apply(L_DEPLOYMENT_SECRET, kept, f"aws:{pointer.name}", start, "the process environment")
            deployed.update({k: v for k, v in kept.items() if k == "WEBUI_SECRET_KEY" or _encryption_key(k)})
            if kept:
                hub_file = _read_file(hub_path)  # ${VAR} may now refer to deployment values

    # The hub file, exactly as before.
    apply(L_HUB, hub_file, str(hub_path))

    # Deployment secret for a standalone hub: after the hub file, every key.
    if secrets and not is_registered:
        name = (env.get("AWS_SECRET_NAME") or "").strip()
        if name:
            values = load_secret(name, region=region_for(name, env), layer=DEPLOYMENT)
            apply(L_DEPLOYMENT_SECRET, values, f"aws:{name}", {**start, **hub_file},
                  str(hub_path) if hub_file else "the process environment")

    # Hub secret, named only in the hub file.
    name = (hub_file.get("HUBZOID_HUB_SECRET_NAME") or "").strip() if secrets else ""
    if name:
        values = load_secret(name, region=region_for(name, env), layer=HUB)
        apply(L_HUB_SECRET, values, f"aws:{name}", hub_file, str(hub_path))

    # The restricted file, exactly as before. Read now, so ${VAR} references
    # resolve against the same environment they always did.
    restricted_file = _read_file(restricted_path)
    apply(L_RESTRICTED, restricted_file, str(restricted_path))

    # Restricted secret, named only in the restricted file.
    name = (restricted_file.get("HUBZOID_RESTRICTED_SECRET_NAME") or "").strip() if secrets else ""
    if name:
        values = load_secret(name, region=region_for(name, env), layer=RESTRICTED)
        apply(L_RESTRICTED_SECRET, values, f"aws:{name}", restricted_file, str(restricted_path))

    # Deployment-locked keys. A hub value that differs from the deployment's
    # means Open WebUI and this bridge disagree. Warn, never fail.
    for key, value in sorted(deployed.items()):
        if value and env.get(key) != value:
            _log_once(logging.WARNING, "hub %s: %s from a hub layer differs from the deployment value. "
                      "Open WebUI and this bridge disagree, so connected tools cannot decrypt their "
                      "tokens. Remove it from the hub layers.", hub_dir.name, key)

    _loads[str(hub_dir.resolve())] = record


# ---------------------------------------------------------------------------
# Views for other processes
# ---------------------------------------------------------------------------
def _without(env: Mapping[str, str], drop: set[str], keep: Callable[[str], bool] | None = None,
             kept: list[str] | None = None) -> dict[str, str]:
    """`env` as if the `drop` layers had never been applied. A key they set
    falls back to the last earlier layer that set it, else to the process
    environment before any hub layer, else it is removed. A key changed since
    the load (no longer the dropped layer's value) is left alone."""
    out = dict(env)
    base = _base_env or {}
    for record in list(_loads.values()):
        for layer in record.order:
            if layer not in drop:
                continue
            for key, applied in record.values[layer].items():
                if out.get(key) != applied:
                    continue
                if keep is not None and keep(key):
                    if kept is not None and key not in kept:
                        kept.append(key)
                    continue
                lower = base.get(key)
                for other in record.order:
                    if other not in drop and key in record.values[other]:
                        lower = record.values[other][key]
                if lower is None:
                    out.pop(key, None)
                else:
                    out[key] = lower
    return out


_HUB_ONLY = {L_HUB_SECRET, L_RESTRICTED, L_RESTRICTED_SECRET}
_RESTRICTED_ONLY = {L_RESTRICTED, L_RESTRICTED_SECRET}


def for_hub_children(env: Mapping[str, str]) -> dict[str, str]:
    """The environment `hubzoid run` hands its bridge, Slack and inbound
    children: without the hub secret and restricted layers, which each child
    loads for itself."""
    return _without(env, _HUB_ONLY)


def deployment_view(env: Mapping[str, str]) -> dict[str, str]:
    """The environment for Open WebUI and the edge under `hubzoid run`: the
    deployment layer and the hub file, never the hub secret or restricted
    layers. An Open WebUI or sign-in setting kept in one of those still passes
    (0.9.x compatibility), with a warning."""
    kept: list[str] = []
    out = _without(env, _HUB_ONLY, keep=owui_setting, kept=kept)
    if kept:
        _log_once(logging.WARNING, "Open WebUI settings found in a hub secret or restricted/.env: %s. "
                  "Move them to the hub .env or the deployment secret.", ", ".join(sorted(kept)))
    return out


def owui_env(env: Mapping[str, str]) -> dict[str, str]:
    """Open WebUI's environment: `env` minus HUBZOID_* keys and secret names,
    and minus AWS credentials when this process loaded an AWS secret (they are
    there for Hubzoid's own fetch)."""
    strip_aws = secrets_in_use()
    return {k: v for k, v in env.items()
            if not k.startswith("HUBZOID_") and k not in SECRET_NAME_KEYS
            and not (strip_aws and k in AWS_CREDENTIAL_KEYS)}


def child_env_overrides(env: Mapping[str, str]) -> dict[str, str]:
    """Values to set in an agent child's environment. {key: ""} blanks a key.

    Blanks, when present: the secret names, SERVICE_SECRET_KEYS and every
    OAUTH_*_ENCRYPTION_KEY, and AWS_CREDENTIAL_KEYS unless
    CLAUDE_CODE_USE_BEDROCK is on (the claude CLI then signs in with them).
    Every key the restricted layers set in this process goes back to the hub
    layers' value, or is blanked when those layers do not set it. The claude
    CLI's own ANTHROPIC_* and CLAUDE_* keys are always kept."""
    bedrock = _truthy(env.get("CLAUDE_CODE_USE_BEDROCK"))
    vertex = _truthy(env.get("CLAUDE_CODE_USE_VERTEX"))
    out: dict[str, str] = {}
    for key in env:
        if (key in SECRET_NAME_KEYS or key in SERVICE_SECRET_KEYS or _encryption_key(key)
                or (not bedrock and key in AWS_CREDENTIAL_KEYS)):
            out[key] = ""

    def keep(key: str) -> bool:
        return (key.startswith(_CHILD_KEEP_PREFIXES) or key in _CHILD_KEEP_KEYS
                or (bedrock and key.startswith("AWS_")) or (vertex and key in _VERTEX_KEYS))

    lowered = _without(env, _RESTRICTED_ONLY, keep=keep)
    for key, value in env.items():
        if key in out:
            continue
        if key not in lowered:
            out[key] = ""
        elif lowered[key] != value:
            out[key] = lowered[key]
    return out


# ---------------------------------------------------------------------------
# Reporting (names and sources, never values)
# ---------------------------------------------------------------------------
def _layers(hub_dir: Path, env: Mapping[str, str], *, fetch_secrets: bool,
            strict: bool) -> list[tuple[str, str, Mapping[str, str]]]:
    """The hub's layers, lowest first: (layer, source, values). The same order
    `apply_layers` applies them in. `strict` raises SecretFetchError for a named
    secret that cannot be read; otherwise that secret is left out."""
    hub_file = _read_file(hub_dir / ".env")
    restricted_file = _read_file(hub_dir / "restricted" / ".env")
    ptrs = {p.layer: p for p in pointers(hub_dir, env)}
    reg = registered(hub_dir, env, hub_file)

    def secret(layer: str) -> tuple[str, dict[str, str]] | None:
        p = ptrs.get(layer)
        if p is None or not fetch_secrets or not fetch_enabled():
            return None
        try:
            values = load_secret(p.name, region=p.region, layer=layer)
        except SecretFetchError:
            if strict:
                raise
            return None
        if p.filtered:
            values = {k: v for k, v in values.items() if bridge_deployment_key(k)}
        return f"aws:{p.name}", values

    layers: list[tuple[str, str, Mapping[str, str]]] = []
    dep = secret(DEPLOYMENT)
    if reg and dep:
        layers.append((L_DEPLOYMENT_SECRET, *dep))
    layers.append((L_HUB, str(hub_dir / ".env"), hub_file))
    if not reg and dep:
        layers.append((L_DEPLOYMENT_SECRET, *dep))
    hub_secret = secret(HUB)
    if hub_secret:
        layers.append((L_HUB_SECRET, *hub_secret))
    layers.append((L_RESTRICTED, str(hub_dir / "restricted" / ".env"), restricted_file))
    restricted_secret = secret(RESTRICTED)
    if restricted_secret:
        layers.append((L_RESTRICTED_SECRET, *restricted_secret))
    return layers


def resolve_key(hub_dir: Path, key: str, *, env: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """(value, layer) for `key` from the hub's files and secrets, with the same
    precedence `apply_layers` uses (a hub secret over the hub `.env`; for a
    standalone hub the deployment secret over its `.env`). Restricted layers
    are not consulted. (None, None) when no file or secret sets it: the caller
    then looks at the deployment's own environment. Raises SecretFetchError
    when a named secret cannot be read, never guessing around it."""
    hub_dir = Path(hub_dir)
    env = _start_env() if env is None else dict(env)
    found: tuple[str | None, str | None] = (None, None)
    for layer, _source, values in _layers(hub_dir, env, fetch_secrets=True, strict=True):
        if layer in (L_RESTRICTED, L_RESTRICTED_SECRET):
            continue
        value = (values.get(key) or "").strip()
        if value:
            found = (value, layer)
    return found


def layer_report(hub_dir: Path, *, env: Mapping[str, str] | None = None,
                 fetch_secrets: bool = True) -> list[dict]:
    """Which layer each configured key comes from, for `hubzoid doctor`.

    One row per key a file or a secret sets: {"key", "layer", "source",
    "shadows"}. `shadows` lists the lower layers that also set it. A secret
    that cannot be read, or is not fetched, is left out (doctor reports it).
    Never includes a value."""
    hub_dir = Path(hub_dir)
    env = _start_env() if env is None else dict(env)
    layers = _layers(hub_dir, env, fetch_secrets=fetch_secrets, strict=False)

    rows: dict[str, dict] = {}
    for layer, source, values in layers:
        for key in values:
            previous = rows.get(key)
            if previous is None:
                shadows = [L_ENV] if key in env else []
            else:
                shadows = [*previous["shadows"], previous["layer"]]
            rows[key] = {"key": key, "layer": layer, "source": source, "shadows": shadows}
    return [rows[k] for k in sorted(rows)]
