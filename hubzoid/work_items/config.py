"""`work-items.yaml`: limits for durable work and the write mode of each action.

The file is optional and lives at the root of the hub. A hub without it behaves
as before. Example:

```yaml
version: 1
work_items:
  admission_paused: false
  max_pending: 10000
  max_input_bytes: 262144
  max_result_bytes: 1048576
write_modes:
  email.send: draft
  erp.submit: "off"
  customer_reply.send: approve
write_mode_overrides:
  whatsapp-desk:
    customer_reply.send: "off"
```

Write modes, per registered consequential action:

* ``off``: the action is not offered. This is the default for any action the
  file does not name.
* ``draft``: only a real draft in the target system, which a person finishes
  there. An action without a real draft adapter cannot use it.
* ``approve``: sent or submitted only after a person approves the exact
  content.

Nothing is ever sent automatically. When several actions apply to one write
(a customer reply that is also an email), the strictest mode wins.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import WorkItemError

FILE = "work-items.yaml"
MODES = ("off", "draft", "approve")
_ORDER = {"off": 0, "draft": 1, "approve": 2}
ACTION_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: Consequential actions: they reach customers, suppliers or books of record,
#: so each has a write mode and is off until enabled. Hubzoid registers these;
#: hubs and channels may add their own (see `register_action`). Any other
#: action a work item declares is still limited by its route, but has no mode.
BUILTIN_ACTIONS = {"email.send": True, "erp.submit": True, "customer_reply.send": True}
_ACTIONS: dict[str, bool] = dict(BUILTIN_ACTIONS)   # id -> has a real draft somewhere
#: Channels that cannot hold a real draft (a chat message has no drafts).
#: Filled by channel adapters as they register.
NO_DRAFT_CHANNELS: set[str] = set()

_LIMITS = {
    "max_pending": (10000, 100000),
    "max_input_bytes": (262144, 1048576),
    "max_result_bytes": (1048576, 8388608),
}


def is_consequential(action: str) -> bool:
    return action in _ACTIONS


def register_action(action: str, *, supports_draft: bool = True) -> str:
    """Register a consequential action id, such as ``crm.update``: it gets a
    write mode and is off until the hub's work-items.yaml enables it."""
    if not ACTION_RE.match(action):
        raise ValueError(f"Action ids look like 'area.verb', for example 'email.send', not {action!r}.")
    _ACTIONS[action] = supports_draft
    return action


@dataclass(frozen=True)
class Config:
    present: bool = False
    admission_paused: bool = False
    max_pending: int = _LIMITS["max_pending"][0]
    max_input_bytes: int = _LIMITS["max_input_bytes"][0]
    max_result_bytes: int = _LIMITS["max_result_bytes"][0]
    write_modes: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)
    digest: str = ""

    def to_dict(self) -> dict:
        return {"version": 1,
                "work_items": {"admission_paused": self.admission_paused,
                               "max_pending": self.max_pending,
                               "max_input_bytes": self.max_input_bytes,
                               "max_result_bytes": self.max_result_bytes},
                "write_modes": dict(self.write_modes),
                "write_mode_overrides": {k: dict(v) for k, v in self.overrides.items()}}


def path(hub_dir) -> Path:
    return Path(hub_dir) / FILE


def _line(node, *keys) -> int | None:
    """The 1-based line of a nested key in a composed YAML document."""
    for key in keys:
        if not isinstance(node, yaml.MappingNode):
            return None
        found = None
        for k, v in node.value:
            if getattr(k, "value", None) == key:
                found = (k, v)
                break
        if found is None:
            return None
        if key == keys[-1]:
            return found[0].start_mark.line + 1
        node = found[1]
    return None


def _fail(source: str, root, keys, message: str):
    line = _line(root, *keys) if root is not None else None
    where = f"{source}:{line}" if line else source
    raise WorkItemError("invalid_definition", f"{where}: {message}", status=422,
                        file=source, line=line)


def parse(text: str, source: str = FILE) -> Config:
    """Validate the file's text. Errors name the file and line."""
    try:
        root = yaml.compose(text) if text.strip() else None
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark else None
        raise WorkItemError("invalid_definition", f"{source}:{line or '?'}: not valid YAML",
                            status=422, file=source, line=line)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        _fail(source, root, (), "the file must be a mapping")
    unknown = set(data) - {"version", "work_items", "write_modes", "write_mode_overrides"}
    for key in sorted(unknown):
        _fail(source, root, (key,), f"unknown key {key!r}")
    if data and data.get("version") != 1:
        _fail(source, root, ("version",), "version must be 1")
    items = data.get("work_items") or {}
    if not isinstance(items, dict):
        _fail(source, root, ("work_items",), "work_items must be a mapping")
    known = {"admission_paused", "enabled", *_LIMITS}
    for key in sorted(set(items) - known):
        _fail(source, root, ("work_items", key), f"unknown key {key!r}")
    limits = {}
    for key, (default, ceiling) in _LIMITS.items():
        value = items.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
            _fail(source, root, ("work_items", key), f"{key} must be a whole number from 1 to {ceiling}")
        limits[key] = value
    paused = items.get("admission_paused", False)
    if not isinstance(paused, bool):
        _fail(source, root, ("work_items", "admission_paused"), "admission_paused must be true or false")
    modes = data.get("write_modes") or {}
    if not isinstance(modes, dict):
        _fail(source, root, ("write_modes",), "write_modes must be a mapping of action to mode")
    for action, mode in modes.items():
        _check_mode(source, root, ("write_modes", action), action, mode)
    overrides = data.get("write_mode_overrides") or {}
    if not isinstance(overrides, dict):
        _fail(source, root, ("write_mode_overrides",), "write_mode_overrides must be a mapping")
    for channel, per in overrides.items():
        if not isinstance(channel, str) or not CHANNEL_RE.match(channel):
            _fail(source, root, ("write_mode_overrides", channel), "channel ids are lowercase letters, digits, - and _")
        if not isinstance(per, dict):
            _fail(source, root, ("write_mode_overrides", channel), "each channel maps actions to modes")
        for action, mode in per.items():
            _check_mode(source, root, ("write_mode_overrides", channel, action), action, mode)
            if mode == "draft" and channel in NO_DRAFT_CHANNELS:
                _fail(source, root, ("write_mode_overrides", channel, action),
                      f"channel {channel!r} has no real drafts. Use off or approve.")
    digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    return Config(present=True, admission_paused=paused, write_modes=dict(modes),
                  overrides={c: dict(v) for c, v in overrides.items()}, digest=digest, **limits)


def _check_mode(source, root, keys, action, mode):
    if not isinstance(action, str) or not ACTION_RE.match(action):
        _fail(source, root, keys, f"{action!r} is not an action id like 'email.send'")
    if mode not in MODES:
        _fail(source, root, keys, f"mode must be off, draft or approve (quote off as \"off\"), not {mode!r}")
    if mode == "draft" and not _ACTIONS.get(action, True):
        _fail(source, root, keys, f"{action} has no real draft in its target system. Use off or approve.")


def load(hub_dir) -> Config:
    """The hub's configuration, or the defaults when it has no file."""
    p = path(hub_dir)
    if not p.is_file():
        return Config()
    return parse(p.read_text(encoding="utf-8"), source=FILE)


def mode_for(cfg: Config, actions, channel: str | None = None) -> str:
    """The effective write mode for one write that performs `actions`.

    For each action, a channel override replaces that action's default. The
    strictest mode across all applicable actions wins: any `off` hides the
    write, any `draft` restricts it to a draft, and it is sent only when every
    action is `approve`."""
    actions = [actions] if isinstance(actions, str) else list(actions)
    if not actions:
        raise ValueError("mode_for needs at least one action")
    actions = [a for a in actions if is_consequential(a)]
    if not actions:
        return "direct"      # no consequential action: governed by the route only
    per_channel = cfg.overrides.get(channel or "", {})
    modes = [per_channel.get(a, cfg.write_modes.get(a, "off")) for a in actions]
    return min(modes, key=_ORDER.__getitem__)
