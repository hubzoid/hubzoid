"""Which secret names the store accepts, and which values are bound to a
destination.

A stored name that is a valid environment variable name joins the
environment when a bridge starts (``hubzoid.secretstore.layer``). Any other
name is only read through a ``secret:`` reference.

Bootstrap and operator settings never come from the store: the deployment key
encrypts the store itself, database URLs open it, and sign-in, bridge and
Hubzoid configuration belong to the operator (GitHub #49).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

from .. import appmode
from .. import config_secrets as cs

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Prefixes an operator owns. Compared upper-cased.
_OPERATOR_PREFIXES = (
    "HUBZOID_", "BRIDGE_", "DATABASE_", "AWS_", "LD_", "DYLD_",
    "WEBUI_", "OAUTH_", "OPENID_", "GOOGLE_CLIENT_", "MICROSOFT_CLIENT_", "GITHUB_CLIENT_",
)
_OPERATOR_KEYS = (cs.SECRET_NAME_KEYS | cs.SERVICE_SECRET_KEYS | cs.BRIDGE_DEPLOYMENT_KEYS
                  | {cs.INHERITED_MARKER, "PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
                     "NODE_OPTIONS", "HOME", "USER", "SHELL", "MODEL", "PORT", "UI_PORT"})
# Process control: a stored value joins the environment every subprocess of
# the bridge inherits (git for scheduled commits and hub changes, the claude
# and codex CLIs, shells), so these would let whoever writes a secret choose
# what runs (git config and hooks, a shell's start file, interpreter
# options), whom to trust (CA bundles, proxies) or where the operator's model
# keys are sent (provider base URLs). Compared upper-cased.
_PROCESS_PREFIXES = (
    "GIT_", "SSH_", "BASH_", "PYTHON", "PERL", "RUBY", "JAVA_", "_JAVA_", "JDK_", "NODE_", "NPM_CONFIG_",
    "CLAUDE_CODE_", "CLAUDE_CONFIG", "CODEX_", "SSL_CERT", "CURL_", "REQUESTS_",
)
_PROCESS_KEYS = {
    "ENV", "BROWSER", "EDITOR", "VISUAL", "PAGER", "PROMPT_COMMAND", "PS4", "SHELLOPTS", "IFS", "TMPDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
    "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENROUTER_API_BASE",
}
# Credentials under those prefixes that people do store (a Claude
# subscription token for unattended work).
_PROCESS_ALLOWED = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY",
                    "CLAUDE_CODE_OAUTH_TOKEN_TERTIARY"}

# A value that is sent somewhere only the named settings decide. Storing it
# records where they point, and a changed destination stops it being used.
BINDINGS: dict[str, tuple[str, ...]] = {
    "AZURE_API_KEY": ("AZURE_API_BASE", "AZURE_API_VERSION"),
}

# Names worth offering before anything stores them: the key MODEL needs, and
# the keys of capabilities and webhooks a hub declares.
_PROVIDER_KEYS = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "azure": "AZURE_API_KEY",
}
_SECRET_LIKE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE)", re.I)


def env_name(name: str) -> bool:
    """Whether the name can be an environment variable (and so joins it)."""
    return bool(ENV_NAME_RE.match(name or ""))


def operator_only(name: str) -> bool:
    """A name only an operator source may set."""
    upper = (name or "").upper()
    if upper in _PROCESS_ALLOWED:
        return False
    # A retired Open WebUI name would stop the hub at start (appmode.RETIRED).
    return (upper in _OPERATOR_KEYS or upper.startswith(_OPERATOR_PREFIXES) or upper in appmode.RETIRED
            or upper in _PROCESS_KEYS or upper.startswith(_PROCESS_PREFIXES))


def problem(name: str) -> str | None:
    """Why the store refuses this name, or None when it accepts it."""
    if not NAME_RE.match(name or ""):
        return ("Use 1 to 128 letters, digits, dots, dashes or underscores, starting with a letter "
                "or a digit.")
    if env_name(name) and operator_only(name):
        return (f"{name} is set by the operator only, in the environment, a .env file or an AWS "
                "secret. It cannot be stored here.")
    return None


def secret_like(name: str) -> bool:
    return bool(_SECRET_LIKE.search(name or ""))


def destination(name: str, env: Mapping[str, str]) -> str | None:
    """The digest of where a bound value may be sent, from `env`. None for a
    name with no binding."""
    keys = BINDINGS.get(name)
    if not keys:
        return None
    where = {k: (env.get(k) or "").strip() for k in keys}
    raw = json.dumps(where, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def provider_key(model: str | None) -> str | None:
    """The API key environment variable the configured MODEL needs, if any."""
    prefix = (model or "").split("/", 1)[0].strip().lower()
    return _PROVIDER_KEYS.get(prefix)
