"""The one way to refer to a secret: ``secret:<scope>/<name>`` or ``env:<NAME>``.

    secret:deployment/<name>                   the deployment's store
    secret:hub/<hub_id>/<name>                 one hub's store
    secret:channel/<hub_id>/<channel_id>/<name>  one channel's slot
    env:<NAME>                                 the layered configuration
                                               (environment, .env files, AWS)

In YAML a key that holds a reference ends in ``_ref``, for example
``client_secret_ref: secret:deployment/google-oauth``. The object form
``{provider, name}`` some files use is the parsed shape of the same reference,
never a second syntax.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .names import ENV_NAME_RE, NAME_RE

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SCOPES = ("deployment", "hub", "channel")


class RefError(ValueError):
    """A reference that does not follow the grammar. ``code`` is stable."""

    code = "invalid_ref"


@dataclass(frozen=True)
class Ref:
    provider: str               # secret | env
    name: str
    scope: str | None = None    # deployment | hub | channel, for secret:
    hub: str | None = None
    channel: str | None = None

    @property
    def target(self) -> str:
        """The store's target column: '' for the deployment, the hub key, or
        ``<hub>/<channel>``."""
        if self.scope == "hub":
            return self.hub or ""
        if self.scope == "channel":
            return f"{self.hub}/{self.channel}"
        return ""

    def __str__(self) -> str:
        if self.provider == "env":
            return f"env:{self.name}"
        if self.scope == "deployment":
            return f"secret:deployment/{self.name}"
        if self.scope == "hub":
            return f"secret:hub/{self.hub}/{self.name}"
        return f"secret:channel/{self.hub}/{self.channel}/{self.name}"


def parse(text: str) -> Ref:
    """Parse a reference. Raises RefError with a message that says what to write."""
    raw = (text or "").strip()
    provider, sep, rest = raw.partition(":")
    if not sep:
        raise RefError("A secret reference starts with secret: or env:, for example "
                       "secret:hub/<hub>/<name> or env:<NAME>.")
    if provider == "env":
        if not ENV_NAME_RE.match(rest):
            raise RefError("env: names an environment variable, for example env:OPENAI_API_KEY.")
        return Ref("env", rest)
    if provider != "secret":
        raise RefError(f"Unknown reference kind {provider!r}. Use secret: or env:.")
    parts = rest.split("/")
    scope = parts[0]
    expected = {"deployment": 2, "hub": 3, "channel": 4}.get(scope)
    if expected is None:
        raise RefError("The scope after secret: is deployment, hub or channel.")
    if len(parts) != expected:
        shape = {"deployment": "secret:deployment/<name>", "hub": "secret:hub/<hub_id>/<name>",
                 "channel": "secret:channel/<hub_id>/<channel_id>/<name>"}[scope]
        raise RefError(f"A {scope} reference looks like {shape}.")
    name = parts[-1]
    if not NAME_RE.match(name):
        raise RefError("A secret name has 1 to 128 letters, digits, dots, dashes or underscores.")
    for part in parts[1:-1]:
        if not _ID_RE.match(part):
            raise RefError(f"{part!r} is not a valid hub or channel ID (lowercase letters, digits, "
                           "dots, dashes or underscores).")
    if scope == "deployment":
        return Ref("secret", name, "deployment")
    if scope == "hub":
        return Ref("secret", name, "hub", hub=parts[1])
    return Ref("secret", name, "channel", hub=parts[1], channel=parts[2])


def from_object(value: dict) -> Ref:
    """The object form ``{provider, name}``: ``{"provider": "env", "name": "X"}``
    or ``{"provider": "secret", "name": "hub/<hub>/<name>"}``."""
    if not isinstance(value, dict) or set(value) != {"provider", "name"}:
        raise RefError("A reference object has exactly provider and name.")
    return parse(f"{value['provider']}:{value['name']}")
