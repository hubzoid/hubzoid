"""Devices: install, offline shell, connection, input and keyboard shortcuts.

The ``devices`` namespace of ``chat/config.yaml`` holds the deployment's
defaults; ``allow_personal`` says which of them a person may change in their
account preferences (``send_key``, ``touch_enter``, ``follow_generation``,
``scroll_on_branch_change``, ``contrast``, ``haptics``, ``shortcuts``).

Shortcuts bind action ids to chords such as ``$mod+Shift+K`` (``$mod`` is
Cmd on Apple platforms, Ctrl elsewhere). Reserved browser chords and
conflicts are refused; single-character keys need ``shortcuts.single_key``.

The service worker (``/sw.js``) caches only the public app shell so the page
opens offline and says so; it never caches an API response, and with
``offline.shell_cache: false`` it removes itself.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import graph as g

Err = g.GraphError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Install(_Strict):
    enabled: bool = True
    display: Literal["standalone", "minimal-ui", "browser"] = "standalone"
    start_path: str = "/"
    share_target: bool = True

    @field_validator("start_path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not v.startswith("/") or v.startswith("//") or ".." in v.split("/"):
            raise ValueError("start_path is a path inside this site, starting with /")
        return v


class Offline(_Strict):
    shell_cache: bool = True
    navigation_timeout_ms: int = Field(3000, ge=1000, le=10000)


class Connection(_Strict):
    banner_delay_ms: int = Field(2000, ge=0, le=10000)
    resume_grace_ms: int = Field(8000, ge=0, le=30000)
    probe_max_interval_seconds: int = Field(30, ge=5, le=120)


class Shortcuts(_Strict):
    enabled: bool = True
    single_key: bool = False
    defaults: dict[str, str | None] = Field(default_factory=dict)


class Defaults(_Strict):
    send_key: Literal["enter", "mod_enter"] = "enter"
    touch_enter: Literal["newline", "send"] = "newline"
    follow_generation: bool = True
    scroll_on_branch_change: bool = True
    contrast: Literal["system", "standard", "more"] = "system"
    haptics: bool = False


class AllowPersonal(_Strict):
    send_key: bool = True
    touch_enter: bool = True
    follow_generation: bool = True
    scroll_on_branch_change: bool = True
    contrast: bool = True
    haptics: bool = True
    shortcuts: bool = True


class DevicesSettings(_Strict):
    install: Install = Field(default_factory=Install)
    offline: Offline = Field(default_factory=Offline)
    connection: Connection = Field(default_factory=Connection)
    shortcuts: Shortcuts = Field(default_factory=Shortcuts)
    defaults: Defaults = Field(default_factory=Defaults)
    allow_personal: AllowPersonal = Field(default_factory=AllowPersonal)


#: action id -> (label, default chord or None)
ACTIONS: dict[str, tuple[str, str | None]] = {
    "chat.new": ("New chat", "$mod+Shift+O"),
    "chat.search": ("Search chats", "$mod+K"),
    "chat.focus_composer": ("Focus the message box", "Shift+Escape"),
    "chat.toggle_sidebar": ("Show or hide the sidebar", "$mod+Shift+S"),
    "chat.copy_last": ("Copy the last answer", "$mod+Shift+C"),
    "chat.stop": ("Stop the reply", "$mod+Shift+X"),
    "chat.shortcuts": ("Show keyboard shortcuts", "$mod+/"),
}
#: chords the browser or operating system owns
RESERVED = {"$mod+W", "$mod+T", "$mod+N", "$mod+Q", "$mod+R", "$mod+L", "$mod+Shift+T", "$mod+Shift+N",
            "$mod+Tab", "$mod+Shift+Tab", "Alt+F4", "$mod+P", "$mod+F", "$mod+C", "$mod+V", "$mod+X", "$mod+Z",
            "$mod+A"}
_MODS = ("$mod", "Ctrl", "Alt", "Shift", "Meta")
_KEY = re.compile(r"^(?:[A-Z0-9]|F(?:[1-9]|1[0-2])|Escape|Enter|Space|Tab|Backspace|Delete|Arrow(?:Up|Down|Left|Right)|"
                  r"[/\\.,;'\[\]=`-])$")


def normalize_chord(chord: str) -> str:
    parts = [p.strip() for p in str(chord).split("+") if p.strip()]
    if chord.endswith("++"):
        parts.append("+")
    if not parts:
        raise ValueError("an empty chord")
    *mods, key = parts
    canon = {m.lower(): m for m in _MODS}
    mods = [canon.get(m.lower(), m) for m in mods]
    if any(m not in _MODS for m in mods) or len(set(mods)) != len(mods):
        raise ValueError(f"unknown or repeated modifier in {chord!r}")
    key = key.upper() if len(key) == 1 else key
    if not _KEY.match(key):
        raise ValueError(f"{key!r} is not a key name")
    ordered = [m for m in _MODS if m in mods]
    return "+".join(ordered + [key])


def check_shortcuts(bindings: dict, *, single_key: bool) -> dict[str, str | None]:
    """Normalised bindings, or ``Err(422)`` naming the first problem."""
    if not isinstance(bindings, dict):
        raise Err(422, "validation_failed", "shortcuts map action ids to chords (or null).")
    out: dict[str, str | None] = {}
    for action, chord in bindings.items():
        if action not in ACTIONS:
            raise Err(422, "validation_failed", f"{action!r} is not a shortcut action.", fields={"action": action})
        if chord is None:
            out[action] = None
            continue
        try:
            norm = normalize_chord(str(chord))
        except ValueError as exc:
            raise Err(422, "validation_failed", f"{action}: {exc}.", fields={"action": action})
        if norm in RESERVED:
            raise Err(422, "reserved_chord", f"{norm} belongs to the browser. Choose another.",
                      fields={"action": action})
        if "+" not in norm and not single_key:
            raise Err(422, "validation_failed", f"{action}: single keys are turned off. Add a modifier.",
                      fields={"action": action})
        out[action] = norm
    return out


def effective_shortcuts(document: dict, personal: dict | None) -> dict:
    cfg = document["devices"]["shortcuts"]
    allow = document["devices"]["allow_personal"]["shortcuts"]
    items, used = {}, {}
    deployment = cfg["defaults"] or {}
    mine = (personal or {}).get("shortcuts") or {}
    for action, (label, default) in ACTIONS.items():
        chord, source = default, "default"
        if action in deployment:
            chord, source = deployment[action], "deployment"
        if allow and action in mine:
            chord, source = mine[action], "account"
        items[action] = {"label": label, "chord": chord if cfg["enabled"] else None, "source": source}
        if chord and cfg["enabled"]:
            used.setdefault(chord, []).append(action)
    conflicts = {c: a for c, a in used.items() if len(a) > 1}
    return {"enabled": cfg["enabled"], "single_key": cfg["single_key"], "actions": items, "conflicts": conflicts}


_CHOICES = {"send_key": ("enter", "mod_enter"), "touch_enter": ("newline", "send"),
            "contrast": ("system", "standard", "more")}
_BOOLS = ("follow_generation", "scroll_on_branch_change", "haptics")


def _personal(key: str):
    def check(value: Any, document: dict) -> Any:
        from .config import ConfigError

        dev = document["devices"]
        if not dev["allow_personal"].get(key, False):
            raise ConfigError(422, "policy_locked", f"Your administrator manages {key.replace('_', ' ')}.")
        if key in _CHOICES:
            if value not in _CHOICES[key]:
                raise ConfigError(422, "validation_failed", f"{key} is one of {', '.join(_CHOICES[key])}.")
            return value
        if key in _BOOLS:
            if not isinstance(value, bool):
                raise ConfigError(422, "validation_failed", f"{key} is true or false.")
            return value
        if key == "shortcuts":
            if not dev["shortcuts"]["enabled"]:
                raise ConfigError(422, "policy_locked", "Custom shortcuts are turned off for this deployment.")
            try:
                clean = check_shortcuts(value, single_key=dev["shortcuts"]["single_key"])
            except Err as exc:
                raise ConfigError(422, exc.code, exc.message)
            merged = effective_shortcuts(document, {"shortcuts": clean})
            if merged["conflicts"]:
                chord, actions = next(iter(merged["conflicts"].items()))
                raise ConfigError(422, "shortcut_conflict", f"{chord} is used by {' and '.join(actions)}.")
            return clean
        raise ConfigError(422, "unknown_key", f"{key} is not a personal setting.")

    return check


def register() -> None:
    from .config import namespaces, register_namespace, register_personal_key

    ns = namespaces().get("devices")
    if ns is None or not ns.installed:
        register_namespace("devices", DevicesSettings, owner="HUB-83",
                           personal_keys=tuple(_CHOICES) + _BOOLS + ("shortcuts",))
    for key in tuple(_CHOICES) + _BOOLS + ("shortcuts",):
        register_personal_key(key, _personal(key))


register()

SUPPORT = {
    "automated": ["Chromium desktop", "Chromium Android (emulated Pixel 7)", "WebKit iPhone (emulated iPhone 14)"],
    "advisory": ["Physical iOS and Android devices", "VoiceOver, TalkBack and NVDA"],
    "note": "Automated and emulated runs gate a release; physical observations are advisory (no WCAG claim).",
}

SW_SHELL = r"""// Hubzoid offline shell. Caches the public app shell only; never an API response.
const CACHE = "hz-shell-v1";
const TIMEOUT = __TIMEOUT__;
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(["/offline-shell"])).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("message", (e) => {
  if (e.data === "hz:logout") e.waitUntil(caches.delete(CACHE));
});
self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/portal/api/")) return; // never cached
  if (req.mode === "navigate") {
    e.respondWith(new Promise((resolve) => {
      let done = false;
      const timer = setTimeout(() => { if (!done) caches.match("/offline-shell").then((r) => r && resolve(r)); }, TIMEOUT);
      fetch(req).then((r) => { done = true; clearTimeout(timer); resolve(r); })
        .catch(() => caches.match("/offline-shell").then((r) => resolve(r || Response.error())));
    }));
  }
});
"""

SW_REMOVE = r"""// Offline shell turned off: remove this worker and its cache.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.map((k) => caches.delete(k))))
    .then(() => self.registration.unregister())
    .then(() => self.clients.matchAll()).then((cs) => cs.forEach((c) => c.navigate(c.url))));
});
"""

OFFLINE_SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Offline</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#1c1c1b;color:#ececea;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:360px;padding:24px;text-align:center}button{margin-top:12px;padding:6px 14px;border-radius:7px;
border:1px solid #2f2f2d;background:#232322;color:#ececea}</style></head>
<body><main><h1 style="font-size:18px;margin:0 0 8px">You're offline</h1>
<p style="margin:0;color:#9d9d97">Nothing is sent while offline. Your chats are safe on the server.</p>
<button onclick="location.reload()">Try again</button></main></body></html>"""


def service_worker(document: dict) -> str:
    off = document["devices"]["offline"]
    if not off["shell_cache"]:
        return SW_REMOVE
    return SW_SHELL.replace("__TIMEOUT__", str(int(off["navigation_timeout_ms"])))


def doctor(document: dict, *, public_url: str) -> list[dict]:
    dev = document["devices"]
    secure = public_url.startswith("https://") or "://127.0.0.1" in public_url or "://localhost" in public_url
    out = [
        {"check": "install.manifest", "status": "ok" if dev["install"]["enabled"] else "info",
         "detail": "Served at /manifest.webmanifest." if dev["install"]["enabled"] else "Install is turned off."},
        {"check": "offline.worker", "status": "ok" if secure else "warn",
         "detail": ("The offline shell needs HTTPS (or localhost). Service workers are refused on plain HTTP."
                    if not secure else ("Shell cache on." if dev["offline"]["shell_cache"] else
                                        "Shell cache off: the worker removes itself."))},
        {"check": "install.share_target", "status": "ok" if (dev["install"]["share_target"] and
                                                            dev["install"]["enabled"] and dev["offline"]["shell_cache"])
         else "info", "detail": "Shared text opens in the message box; it is never sent automatically."},
    ]
    return out
