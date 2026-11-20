"""Amazon Bedrock as a model provider: ``MODEL=bedrock/<tier-or-id>``.

Opt in per hub. ``claude-local`` with the subscription stays the default and
does not change. A Claude model on Bedrock runs on the Claude Agent SDK runtime
with the CLI's own Bedrock settings in the child's environment. Any other
Bedrock model, and an id with a LiteLLM route (``bedrock/converse/<id>``),
runs on the OpenAI Agents SDK through LiteLLM.

This module holds the id rules, the tier pins, region resolution, the child
environment and the doctor check. No runtime SDK is imported here: boto3 only,
for the profile lookup and doctor, and only when they run.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping

log = logging.getLogger(__name__)

PREFIX = "bedrock/"
TIERS = ("opus", "sonnet", "haiku")

#: What each tier means on ``claude-local`` with the bundled CLI (2.1.285), as
#: Bedrock model ids. The region prefix is added (``apac.``, ``us.`` ...).
#: Change with the claude-agent-sdk floor, checked against the CLI's own model
#: table (its ``provider_ids.bedrock``).
TIER_MODELS = {
    "opus": "anthropic.claude-opus-5-5",
    "sonnet": "anthropic.claude-sonnet-5-5",
    "haiku": "anthropic.claude-haiku-4-5-20251001-v1:0",
}
_TIER_ENV = {t: f"ANTHROPIC_DEFAULT_{t.upper()}_MODEL" for t in TIERS}

#: A cross-region profile's geography prefix (us, us-gov, eu, apac, in, jp, au, global ...).
_GEO_RE = r"[a-z]{2,6}(?:-[a-z]{2,4})?"
_CLAUDE_ID = re.compile(r"^(?:" + _GEO_RE + r"\.)?anthropic\.claude-[a-z0-9.\-]+(?::\d+)?$")
_ARN = re.compile(r"^arn:aws(?:-[a-z]+)*:bedrock:(?P<region>[a-z0-9-]*):(?P<account>\d*):"
                  r"(?P<kind>foundation-model|inference-profile|application-inference-profile)/(?P<id>[^\s\[]+)$")
_ONE_M = "[1m]"

#: Keys a Bedrock child must not inherit: billing goes to AWS only.
_BLANK = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", "CLAUDE_CODE_OAUTH_TOKEN_TERTIARY",
          "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
          "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE")

_profiles: dict[str, str] = {}      # application inference profile ARN -> backing model id


class BedrockError(RuntimeError):
    """A Bedrock hub cannot run as configured. The message says what to set."""


# ---------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------
def is_bedrock(model: str | None) -> bool:
    return (model or "").strip().lower().startswith(PREFIX)


def inner(model: str) -> str:
    """The id after ``bedrock/``."""
    s = (model or "").strip()
    return s[len(PREFIX):] if s.lower().startswith(PREFIX) else s


def _bare(x: str) -> str:
    x = x.strip()
    return x[:-len(_ONE_M)] if x.endswith(_ONE_M) else x


def is_claude_name(x: str | None) -> bool:
    """A tier, a ``claude-*`` id, a Bedrock Claude id or a Bedrock ARN: a name
    that follows the hub's provider when given as an override."""
    s = _bare((x or "").strip().lower())
    return s in TIERS or s.startswith("claude-") or bool(_CLAUDE_ID.match(s)) or s.startswith("arn:aws")


def _claude_model_id(x: str) -> bool:
    s = _bare(x.strip().lower())
    return s in TIERS or s.startswith("claude-") or bool(_CLAUDE_ID.match(s))


def arn(x: str):
    """The parts of a Bedrock ARN (``region``, ``kind``, ``id``), or None."""
    return _ARN.match(_bare(x.strip()))


def litellm_route(model: str) -> bool:
    """``bedrock/converse/<id>`` and the other LiteLLM route prefixes: kept on LiteLLM."""
    x = inner(model)
    return "/" in x and not x.lower().startswith("arn:")


def resource(model: str) -> str:
    """The Bedrock model id or ARN, without ``bedrock/`` or a LiteLLM route prefix."""
    x = inner(model)
    return x.split("/", 1)[1] if litellm_route(model) else x


def is_claude(model: str | None, *, resolve: bool = True) -> bool:
    """Whether a ``bedrock/`` model runs on the Claude Agent SDK. An application
    inference profile shows no model, so it is looked up once per process
    (``resolve``); without ``resolve`` an unresolved one counts as not Claude."""
    if not is_bedrock(model) or litellm_route(model):
        return False
    x = inner(model or "")
    if _claude_model_id(x):
        return True
    parts = arn(x)
    if parts is None:
        return False
    if parts["kind"] != "application-inference-profile":
        return _claude_model_id(parts["id"])
    backing = _profiles.get(_bare(x)) if not resolve else backing_model(_bare(x))
    return bool(backing) and _claude_model_id(backing)


def backing_model(profile_arn: str) -> str:
    """The model behind an application inference profile (``GetInferenceProfile``),
    cached for the process. Raises BedrockError when it cannot be read."""
    if profile_arn in _profiles:
        return _profiles[profile_arn]
    parts = arn(profile_arn)
    try:
        import boto3

        client = boto3.client("bedrock", region_name=(parts and parts["region"]) or require_region(None))
        got = client.get_inference_profile(inferenceProfileIdentifier=profile_arn)
        model_arn = ((got.get("models") or [{}])[0] or {}).get("modelArn") or ""
        backing = model_arn.rsplit("/", 1)[-1]
    except BedrockError:
        raise
    except Exception as exc:  # noqa: BLE001 - denied, unknown or no network: say what to do
        raise BedrockError(
            f"Could not read the application inference profile {profile_arn} ({type(exc).__name__}: {exc}). "
            "Grant bedrock:GetInferenceProfile on it, or write MODEL=bedrock/converse/<arn> to run it on "
            "LiteLLM instead.") from exc
    if not backing:
        raise BedrockError(f"The application inference profile {profile_arn} names no model.")
    _profiles[profile_arn] = backing
    return backing


def cli_model(model: str) -> str:
    """What the Claude CLI takes as ``--model``: the tier alias or the id itself."""
    x = inner(model)
    return x.lower() if x.lower() in TIERS else x


# ---------------------------------------------------------------------------
# Region
# ---------------------------------------------------------------------------
def region(model: str | None = None, env: Mapping[str, str] | None = None) -> tuple[str | None, str]:
    """(region, where it came from). Same order LiteLLM uses, so both engines
    agree: the region in an ARN model id, AWS_REGION_NAME, AWS_REGION,
    AWS_DEFAULT_REGION, then the AWS profile's region."""
    env = os.environ if env is None else env
    parts = arn(resource(model)) if model else None
    if parts and parts["region"]:
        return parts["region"], "the model ARN"
    for key in ("AWS_REGION_NAME", "AWS_REGION", "AWS_DEFAULT_REGION"):
        if (env.get(key) or "").strip():
            return env[key].strip(), key
    try:
        import boto3

        profile = (env.get("AWS_PROFILE") or "").strip() or None
        found = boto3.session.Session(profile_name=profile).region_name
    except Exception:  # noqa: BLE001 - no profile, no config: no region
        found = None
    if found:
        return found, f"the AWS profile {profile or 'default'}"
    return None, ""


def require_region(model: str | None, env: Mapping[str, str] | None = None) -> str:
    found, _source = region(model, env)
    if not found:
        raise BedrockError(
            "A Bedrock hub needs an AWS region. Set AWS_REGION in the hub's .env (for example "
            "AWS_REGION=ap-south-1), or a region on the AWS profile. Without one the Claude CLI would use "
            "us-east-1 and LiteLLM us-west-2.")
    return found


def geo_prefix(region_name: str, env: Mapping[str, str] | None = None) -> str:
    """The inference profile prefix the CLI uses for a region, or the operator's
    ANTHROPIC_BEDROCK_REGION_PREFIX."""
    env = os.environ if env is None else env
    chosen = (env.get("ANTHROPIC_BEDROCK_REGION_PREFIX") or "").strip()
    if chosen:
        return chosen
    r = region_name.lower()
    for start, prefix in (("us-gov-", "us-gov"), ("us-", "us"), ("eu-", "eu"), ("ap-", "apac")):
        if r.startswith(start):
            return prefix
    return "global"


def tier_pins(region_name: str, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """{tier: Bedrock id}. A tier the operator set in .env (ANTHROPIC_DEFAULT_<TIER>_MODEL) is theirs."""
    env = os.environ if env is None else env
    prefix = geo_prefix(region_name, env)
    return {t: (env.get(_TIER_ENV[t]) or "").strip() or f"{prefix}.{TIER_MODELS[t]}" for t in TIERS}


def primary_id(model: str, region_name: str, env: Mapping[str, str] | None = None) -> str:
    x = inner(model)
    return tier_pins(region_name, env)[x.lower()] if x.lower() in TIERS else x


# ---------------------------------------------------------------------------
# The Claude child's environment
# ---------------------------------------------------------------------------
def child_env(model: str, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Variables for a Claude CLI child on Bedrock, on top of
    ``config_secrets.child_env_overrides(..., bedrock=True)``."""
    env = os.environ if env is None else env
    where = require_region(model, env)
    out = {k: "" for k in _BLANK}
    pins = tier_pins(where, env)
    out.update({
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": where,
        "ANTHROPIC_MODEL": primary_id(model, where, env),
        "CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK": "1",
        "CLAUDE_CODE_PROMPT_CACHE_TTL": (env.get("HUBZOID_CACHE_TTL") or "1h").strip().lower(),
    })
    out.update({_TIER_ENV[t]: pins[t] for t in TIERS})
    if (env.get("HUBZOID_DISABLE_PROMPT_CACHE") or "").strip().lower() in ("true", "1", "yes"):
        out["DISABLE_PROMPT_CACHING"] = "1"
    return out


def subscription_scrub(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """For a subscription call on a hub that also uses Bedrock: blank model pins
    in the environment that name Bedrock ids, which the subscription cannot run."""
    env = os.environ if env is None else env
    keys = ("ANTHROPIC_MODEL", *_TIER_ENV.values())
    return {k: "" for k in keys if _bedrock_only((env.get(k) or "").strip())}


def _bedrock_only(value: str) -> bool:
    low = _bare(value.lower())
    return bool(low) and (low.startswith("arn:") or (bool(_CLAUDE_ID.match(low)) and not low.startswith("claude-")))


def valid_pin(pin: str) -> bool:
    """A pin the CLI can take on Bedrock: a tier, a ``claude-*`` id, a Bedrock
    Claude id or a Bedrock ARN (each optionally with ``[1m]``)."""
    return _claude_model_id(pin) or arn(pin) is not None


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------
def check(model: str, *, used_tiers: set[str] | None = None) -> list[tuple[str, str]]:
    """[(status, message)] for doctor and the Settings diagnostics: region,
    identity and the model's lookup. Inference access is proven only by a real
    call (the active reply check)."""
    out: list[tuple[str, str]] = []
    where, source = region(model)
    if not where:
        try:
            require_region(model)
        except BedrockError as exc:
            return [("fail", str(exc))]
    out.append(("ok", f"Region {where} (from {source}), inference profile prefix {geo_prefix(where)}."))
    try:
        import boto3

        if (os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip():
            out.append(("ok", "AWS_BEARER_TOKEN_BEDROCK is set: a Bedrock API key, so there is no STS identity "
                               "to show."))
        else:
            ident = boto3.client("sts", region_name=where).get_caller_identity()
            out.append(("ok", f"AWS identity {ident.get('Arn')} (account {ident.get('Account')})."))
    except Exception as exc:  # noqa: BLE001
        return out + [("fail", f"No usable AWS credentials ({type(exc).__name__}: {exc}). Use an instance or "
                               "task role, AWS_PROFILE, access keys or AWS_BEARER_TOKEN_BEDROCK.")]
    parts = arn(inner(model))
    try:
        claude = is_claude(model)
    except BedrockError as exc:
        return out + [("fail", str(exc))]
    if parts is not None and parts["kind"] == "application-inference-profile":
        out.append(("ok", f"The application inference profile runs {_profiles.get(_bare(inner(model)))}, "
                          f"so it runs on {'the Claude Agent SDK' if claude else 'LiteLLM'}."))
    ids: list[tuple[str, str, bool]] = []          # (label, id, strict)
    if claude:
        pins = tier_pins(where)
        ids.append(("model", primary_id(model, where) if not litellm_route(model) else resource(model), True))
        for t in TIERS:
            ids.append((t, pins[t], t in (used_tiers or set())))
    else:
        ids.append(("model", resource(model), True))
    client = boto3.client("bedrock", region_name=where)
    for label, ident_, strict in ids:
        status, text = _lookup(client, _bare(ident_), where)
        if status != "ok" and not strict:
            status = "warn" if status == "fail" else status
            text += " Not used by this hub, so it only matters if a task or sub-agent asks for it."
        out.append((status, f"{label}: {text}"))
    return out


def _lookup(client, ident: str, where: str) -> tuple[str, str]:
    """Look an id up by its resource type: a profile (geographic prefix or
    profile ARN) with GetInferenceProfile, a foundation model with GetFoundationModel."""
    parts = arn(ident)
    try:
        if parts is not None and parts["kind"] == "foundation-model":
            client.get_foundation_model(modelIdentifier=parts["id"])
        elif parts is not None or ident.split(":", 1)[0].count(".") >= 2:   # geo.provider.model: a profile
            got = client.get_inference_profile(inferenceProfileIdentifier=ident)
            if parts is not None and parts["kind"] == "application-inference-profile":
                backing = ((got.get("models") or [{}])[0] or {}).get("modelArn", "").rsplit("/", 1)[-1]
                return "ok", f"{ident} is a profile for {backing or 'an unnamed model'}."
        else:
            client.get_foundation_model(modelIdentifier=ident)
        return "ok", f"{ident} is available in {where}."
    except Exception as exc:  # noqa: BLE001
        code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", type(exc).__name__)
        if code in ("AccessDeniedException", "UnauthorizedOperation"):
            return "warn", (f"{ident}: the lookup was denied ({code}). That is the discovery permission "
                            "(bedrock:GetInferenceProfile or bedrock:GetFoundationModel), not inference access. "
                            "The reply check proves inference.")
        hint = _prefixes(client, ident)
        return "fail", f"{ident} was not found in {where} ({code}).{hint}"


def _prefixes(client, ident: str) -> str:
    """Which prefixes the account has for this model, when listing is allowed."""
    base = ident.split("anthropic.", 1)[-1]
    try:
        found = sorted({p["inferenceProfileId"] for page in
                        client.get_paginator("list_inference_profiles").paginate()
                        for p in page.get("inferenceProfileSummaries", [])
                        if p.get("inferenceProfileId", "").endswith(base)})
    except Exception:  # noqa: BLE001 - no list permission: say nothing more
        return ""
    if not found:
        return " The account has no inference profile for this model in this region."
    return (f" Available here: {', '.join(found)}. Set ANTHROPIC_BEDROCK_REGION_PREFIX or the model id to one "
            "of them (global. routes outside the region's geography).")
