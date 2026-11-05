"""Get started cards, from an optional ``get-started.yaml`` at the hub root:

    version: 1
    title: Get started            # optional, at most 40 characters
    cards:                        # 1 to 6, ids unique
      - id: model
        title: Model              # at most 24 characters
        check: model              # built in: the agent's model sign-in
      - id: code
        title: Code
        check: factory.github     # or a doctor check in hub.manifest.yaml
      - id: results
        title: Results
        line: Reviewed branches   # a static card: a line (at most 60) and a detail (at most 80)
        detail: After you approve the plan

A card has a ``check`` or a ``line``, not both. A checked card shows the
check's status and its ``card`` line and detail (``probes.Result.card``), else
its own, else the check's summary. ``build`` runs only the checks the cards
name, when a manager opens the cards (``GET /api/hubs/<key>/get-started``).
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import probes
from .yamlfile import HubFileError, load as load_yaml

log = logging.getLogger(__name__)

FILE = "get-started.yaml"
MODEL = "model"  # the built-in check: the agent's model sign-in
_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class Card(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = Field(min_length=1, max_length=24)
    check: str | None = None
    line: str | None = Field(default=None, min_length=1, max_length=60)
    detail: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError("an id is lowercase letters, digits, dots, dashes or underscores")
        return value

    @field_validator("check")
    @classmethod
    def _check(cls, value: str | None) -> str | None:
        if value is not None and not _ID_RE.match(value):
            raise ValueError(f"a check is {MODEL} or the id of a check in {probes.MANIFEST}")
        return value

    @model_validator(mode="after")
    def _check_or_line(self) -> Card:
        if (self.check is None) == (self.line is None):
            raise ValueError("a card has a check or a line, not both")
        return self


class GetStarted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    title: str = Field(default="Get started", min_length=1, max_length=40)
    cards: list[Card] = Field(min_length=1, max_length=6)


def present(hub_dir: Path) -> bool:
    """Whether the hub has the file. Reads nothing else."""
    return (Path(hub_dir) / FILE).is_file()


def _where(data: dict, loc: tuple) -> str:
    """``card 'code': title`` for a card's field, else the dotted location."""
    if len(loc) >= 2 and loc[0] == "cards" and isinstance(loc[1], int):
        entries = data.get("cards")
        entry = entries[loc[1]] if isinstance(entries, list) and loc[1] < len(entries) else None
        ident = entry.get("id") if isinstance(entry, dict) else None
        name = f"card {ident!r}" if isinstance(ident, str) else f"card {loc[1] + 1}"
        return ": ".join([name, ".".join(map(str, loc[2:]))]) if loc[2:] else name
    return ".".join(map(str, loc))


def load(hub_dir: Path) -> GetStarted | None:
    """The hub's cards, or None without the file. Raises HubFileError with one
    line naming the file (never the hub's path) and the card."""
    hub_dir = Path(hub_dir)
    path = hub_dir / FILE
    if not path.is_file():
        return None
    try:
        data = load_yaml(path)
        parsed = GetStarted.model_validate(data)
    except HubFileError as exc:
        raise HubFileError(str(exc).replace(str(path), FILE)) from None
    except ValidationError as exc:
        first = exc.errors()[0]
        message = str(first["msg"]).removeprefix("Value error, ")
        raise HubFileError(f"{FILE}: {_where(data, tuple(first['loc']))}: {message}") from None
    ids = [c.id for c in parsed.cards]
    if len(ids) != len(set(ids)):
        raise HubFileError(f"{FILE}: two cards share an id")
    named = [c for c in parsed.cards if c.check and c.check != MODEL]
    if named:
        try:
            manifest = probes.manifest(hub_dir)
        except HubFileError as exc:
            raise HubFileError(str(exc).replace(str(hub_dir / probes.MANIFEST), probes.MANIFEST)) from None
        known = [p.id for p in manifest.doctor] if manifest else []
        for card in named:
            if card.check not in known:
                raise HubFileError(f"{FILE}: card {card.id!r}: no check named {card.check}. Use {MODEL}"
                                   + (f" or one of {', '.join(known)}" if known else "") + ".")
    return parsed


def _also(login, *, agent_signed_in: bool) -> str:
    """"Codex also signed in (Plus plan)", from ``logins.describe``."""
    from .. import logins

    name, _, how = logins.describe(login).partition(", ")
    return f"{name} {'also' if agent_signed_in else 'is'} signed in" + (f" ({how})" if how else "")


def model_result(hub_dir: Path, *, fresh: bool = False) -> probes.Result:
    """The built-in ``model`` card: the ``model.signin`` diagnostic's status and
    fix. The line names the login the agent uses (an API model: its name), the
    detail another signed-in login. Each CLI is asked once; ``fresh`` asks
    again instead of using the login kept for a few minutes."""
    from .. import logins
    from ..admin import diagnostics

    try:
        model = (os.environ.get("MODEL") or "claude-local").strip().lower()
        found = logins.detect(fresh=fresh) if model.startswith(("claude-local", "codex-local")) else None
        check = diagnostics.check_model_auth(Path(hub_dir), found=found)
        runtime = (check.details or {}).get("runtime")
        line = detail = None
        if runtime in ("claude-local", "codex-local"):
            usable = [lg for lg in found or [] if lg.signed_in and not lg.problem]
            own = next((lg for lg in usable if lg.runtime == runtime), None)
            other = next((lg for lg in usable if lg.runtime != runtime), None)
            line = logins.describe(own) if own else None
            detail = _also(other, agent_signed_in=check.status == "ok") if other else None
        else:
            line = (os.environ.get("MODEL") or "").strip() or None
    except Exception as exc:  # noqa: BLE001 - one card never breaks the others
        log.warning("get started: the model sign-in could not be checked (%s)", type(exc).__name__)
        return probes.Result(MODEL, "warn", f"The model sign-in could not be checked ({type(exc).__name__}).")
    return probes.Result(MODEL, check.status, check.summary, check.remediation, None,
                         probes.card_of({"line": line, "detail": detail}))


def _card(card: Card, result: probes.Result | None) -> dict:
    if card.check is None or result is None:
        return {"id": card.id, "title": card.title, "status": None, "line": card.line,
                "detail": card.detail, "fix": None}
    own = result.card or {}
    return {"id": card.id, "title": card.title, "status": result.status,
            "line": own.get("line") or card.line or probes.cap(result.summary, 60),
            "detail": own.get("detail") or card.detail, "fix": result.recovery}


def build(hub_dir: Path, *, fresh: bool = False) -> dict | None:
    """What the chat's Get started cards show: runs the checks the cards name,
    and the model card, now (``fresh``: the logins are read again too). None
    without the file; an invalid file gives ``{"error": "<one line>", "cards": []}``."""
    hub_dir = Path(hub_dir)
    try:
        parsed = load(hub_dir)
    except HubFileError as exc:
        return {"error": str(exc), "cards": []}
    if parsed is None:
        return None
    checks = list(dict.fromkeys(c.check for c in parsed.cards if c.check and c.check != MODEL))
    results: dict[str, probes.Result] = {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        model = pool.submit(model_result, hub_dir, fresh=fresh) \
            if any(c.check == MODEL for c in parsed.cards) else None
        if checks:
            try:
                results.update((r.id, r) for r in probes.run(hub_dir, only=checks))
            except HubFileError as exc:  # the manifest changed since load
                return {"error": str(exc).replace(f"{hub_dir}{os.sep}", ""), "cards": []}
        if model is not None:
            results[MODEL] = model.result()
    cards = [_card(c, results.get(c.check or "")) for c in parsed.cards]
    return {"title": parsed.title, "ready": not any(c["status"] in ("warn", "fail") for c in cards),
            "cards": cards, "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
