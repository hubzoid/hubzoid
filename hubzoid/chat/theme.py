"""The theme token contract shared by the chat app and the Console.

One resolved token set per appearance mode (light, dark, OLED) drives both
surfaces: the chat app's CSS variables and the Console's Ant Design tokens are
both generated from it (``css_variables``, ``antd_tokens``), so choosing an
agent never recolours the application and the two surfaces never drift.

A deployment may override tokens in ``branding/theme.json``::

    {"version": 1,
     "palettes": {"light": {"action": "#B5471F", "actionText": "#FFFFFF"}},
     "typography": {"fontFamily": "system", "monoFamily": "system-mono",
                    "baseSize": 16, "lineHeight": 1.5},
     "shape": {"controlRadius": 8, "cardRadius": 14}}

Everything not given inherits the built-in Hubzoid theme. Colours are opaque
six-digit hex only: no alpha, URLs, CSS expressions or variable names. Every
mode is checked for contrast (4.5:1 text, 3:1 focus and control boundaries)
and a failure names the mode, the pair and the measured ratio. Customer colours
are never adjusted silently.

The default palette is the Hubzoid Studio design seed (HubzoidStudio
``tokens/tokens.json``), extended with the semantic pairs a full contract needs.
"""
from __future__ import annotations

import re
from typing import Any

MODES = ("light", "dark", "oled")
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")

TOKENS = (
    "canvas", "surface", "elevated", "sunken", "hover", "pressed",
    "text", "bodyText", "mutedText", "border", "borderSoft", "input",
    "action", "actionText", "actionHover", "actionActive", "actionSoft", "link",
    "focus", "selection", "selectionText", "danger", "dangerText",
    "successText", "successBackground", "successBorder",
    "warningText", "warningBackground", "warningBorder",
    "errorText", "errorBackground", "errorBorder",
    "infoText", "infoBackground", "infoBorder",
)

_LIGHT = {
    # Warm paper: a quiet page, a slightly deeper sidebar, white raised surfaces.
    "canvas": "#FAFAF8", "surface": "#F3F2EF", "elevated": "#FFFFFF", "sunken": "#EEECE7",
    "hover": "#ECEBE7", "pressed": "#E3E1DB",
    "text": "#242421", "bodyText": "#2B2B27", "mutedText": "#69665E",
    "border": "#DEDCD4", "borderSoft": "#E8E6E0", "input": "#898479",
    "action": "#A94224", "actionText": "#FFFFFF", "actionHover": "#90361C",
    "actionActive": "#7D2E18", "actionSoft": "#F8E8DF", "link": "#A94224",
    "focus": "#A94224", "selection": "#F8E8DF", "selectionText": "#242421",
    "danger": "#B3312C", "dangerText": "#FFFFFF",
    "successText": "#286841", "successBackground": "#EAF4EC", "successBorder": "#C9E2D1",
    "warningText": "#795416", "warningBackground": "#FAF0DB", "warningBorder": "#EFDCA6",
    "errorText": "#B3312C", "errorBackground": "#FBEAE7", "errorBorder": "#F0CBC5",
    "infoText": "#345E91", "infoBackground": "#EEF3FA", "infoBorder": "#C9D9EE",
}

_DARK = {
    # Warm graphite, in the style of desktop chat tools: the page, a slightly
    # darker sidebar, raised menus and composer, quiet hairlines, a soft coral.
    "canvas": "#1C1C1B", "surface": "#171716", "elevated": "#252523", "sunken": "#30302E",
    "hover": "#2D2D2B", "pressed": "#363633",
    "text": "#F0EFE9", "bodyText": "#E8E6DF", "mutedText": "#AAA79D",
    "border": "#363630", "borderSoft": "#2C2C29", "input": "#777369",
    "action": "#E79A78", "actionText": "#211A16", "actionHover": "#F0AA8B",
    "actionActive": "#D98B69", "actionSoft": "#382820", "link": "#E79A78",
    "focus": "#F0AA8B", "selection": "#3A2A22", "selectionText": "#F0EFE9",
    "danger": "#E0716A", "dangerText": "#1C1C1B",
    "successText": "#A1CEAF", "successBackground": "#223127", "successBorder": "#2D4A39",
    "warningText": "#E6C283", "warningBackground": "#352E20", "warningBorder": "#5A4826",
    "errorText": "#F0A09A", "errorBackground": "#382423", "errorBorder": "#633838",
    "infoText": "#A9C6EA", "infoBackground": "#18263A", "infoBorder": "#32496A",
}

# Black: true black surfaces for OLED screens, otherwise the dark palette.
_OLED = dict(_DARK, **{
    "canvas": "#000000", "surface": "#000000", "elevated": "#151515", "sunken": "#222220",
    "hover": "#1E1E1D", "pressed": "#2A2A28", "border": "#2A2A27", "borderSoft": "#1C1C1A",
    "actionSoft": "#2E211B", "selection": "#2E211B",
})

BUILTIN = {"light": _LIGHT, "dark": _DARK, "oled": _OLED}
#: Background tokens a dark override does not carry into OLED.
_OLED_OWN = ("canvas", "surface", "elevated", "sunken", "hover", "pressed", "border",
             "borderSoft")

FONTS = {
    "inter": 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
    "system": '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
    "serif": 'Georgia, "Times New Roman", serif',
}
MONO_FONTS = {
    "jetbrains-mono": '"JetBrains Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
    "system-mono": 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
}
TYPOGRAPHY_DEFAULTS = {"fontFamily": "inter", "monoFamily": "jetbrains-mono", "baseSize": 14,
                       "lineHeight": 1.55}
SHAPE_DEFAULTS = {"controlRadius": 8, "cardRadius": 12}

# (foreground, background, minimum ratio, what it is)
PAIRS = (
    ("text", "canvas", 4.5, "text on the page"),
    ("text", "surface", 4.5, "text on panels"),
    ("text", "elevated", 4.5, "text on cards and dialogs"),
    ("bodyText", "canvas", 4.5, "message text"),
    ("mutedText", "canvas", 4.5, "secondary text on the page"),
    ("mutedText", "surface", 4.5, "secondary text on panels"),
    ("mutedText", "elevated", 4.5, "secondary text on cards"),
    ("actionText", "action", 4.5, "button label"),
    ("actionText", "actionHover", 4.5, "button label on hover"),
    ("link", "canvas", 4.5, "links on the page"),
    ("link", "elevated", 4.5, "links on cards"),
    ("selectionText", "selection", 4.5, "selected item"),
    ("dangerText", "danger", 4.5, "destructive button label"),
    ("successText", "successBackground", 4.5, "success message"),
    ("warningText", "warningBackground", 4.5, "warning message"),
    ("errorText", "errorBackground", 4.5, "error message"),
    ("infoText", "infoBackground", 4.5, "information message"),
    ("errorText", "canvas", 4.5, "error text on the page"),
    ("focus", "canvas", 3.0, "focus ring"),
    ("input", "canvas", 3.0, "input boundary"),
    ("action", "canvas", 3.0, "button boundary"),
)

LIMITS = {"manifest_bytes": 64 * 1024}


class ThemeError(ValueError):
    def __init__(self, code: str, message: str, *, pointer: str | None = None, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.pointer = pointer
        self.extra = extra


def _luminance(hex_colour: str) -> float:
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _check_object(value: Any, allowed: set[str], pointer: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ThemeError("validation_failed", f"{pointer or '/'} must be an object.", pointer=pointer)
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ThemeError("unknown_key", f"Unknown key {unknown[0]!r} at {pointer or '/'}.",
                         pointer=f"{pointer}/{unknown[0]}")
    return value


def validate_manifest(manifest: Any) -> dict:
    """Check a theme manifest's shape. Returns it normalised (colours upper
    case). Raises ``ThemeError``. Contrast is checked by ``resolve``."""
    manifest = _check_object(manifest, {"version", "palettes", "typography", "shape"}, "")
    if manifest.get("version") != 1:
        raise ThemeError("unsupported_schema", "theme.json needs \"version\": 1.", pointer="/version")
    out: dict = {"version": 1}
    palettes = _check_object(manifest.get("palettes"), set(MODES), "/palettes")
    out["palettes"] = {}
    for mode, colours in palettes.items():
        colours = _check_object(colours, set(TOKENS), f"/palettes/{mode}")
        clean = {}
        for token, colour in colours.items():
            if not isinstance(colour, str) or not HEX.match(colour):
                raise ThemeError("validation_failed",
                                 f"{mode}.{token} must be a six-digit hex colour like #B5471F.",
                                 pointer=f"/palettes/{mode}/{token}")
            clean[token] = colour.upper()
        out["palettes"][mode] = clean
    typo = _check_object(manifest.get("typography"), set(TYPOGRAPHY_DEFAULTS), "/typography")
    if "fontFamily" in typo and typo["fontFamily"] not in FONTS:
        raise ThemeError("validation_failed", f"fontFamily must be one of {', '.join(FONTS)}.",
                         pointer="/typography/fontFamily")
    if "monoFamily" in typo and typo["monoFamily"] not in MONO_FONTS:
        raise ThemeError("validation_failed", f"monoFamily must be one of {', '.join(MONO_FONTS)}.",
                         pointer="/typography/monoFamily")
    if "baseSize" in typo and (not isinstance(typo["baseSize"], int) or isinstance(typo["baseSize"], bool)
                               or not 14 <= typo["baseSize"] <= 20):
        raise ThemeError("validation_failed", "baseSize is a whole number of pixels from 14 to 20.",
                         pointer="/typography/baseSize")
    if "lineHeight" in typo and (not isinstance(typo["lineHeight"], (int, float))
                                 or isinstance(typo["lineHeight"], bool)
                                 or not 1.4 <= typo["lineHeight"] <= 2):
        raise ThemeError("validation_failed", "lineHeight is from 1.4 to 2.", pointer="/typography/lineHeight")
    out["typography"] = dict(typo)
    shape = _check_object(manifest.get("shape"), set(SHAPE_DEFAULTS), "/shape")
    for key, top in (("controlRadius", 16), ("cardRadius", 24)):
        if key in shape and (not isinstance(shape[key], int) or isinstance(shape[key], bool)
                             or not 0 <= shape[key] <= top):
            raise ThemeError("validation_failed", f"{key} is a whole number of pixels from 0 to {top}.",
                             pointer=f"/shape/{key}")
    out["shape"] = dict(shape)
    return out


def contrast_failures(palettes: dict[str, dict[str, str]]) -> list[dict]:
    failures = []
    for mode in MODES:
        pal = palettes[mode]
        for fg, bg, minimum, what in PAIRS:
            ratio = contrast(pal[fg], pal[bg])
            if ratio + 1e-9 < minimum:
                failures.append({"mode": mode, "foreground": fg, "background": bg,
                                 "ratio": round(ratio, 2), "minimum": minimum, "what": what})
    return failures


def resolve(manifest: dict | None = None, *, check: bool = True) -> dict:
    """The complete theme: every token in every mode, typography and shape.
    Raises ``ThemeError(contrast_failure)`` when ``check`` and a pair fails."""
    manifest = validate_manifest(manifest) if manifest else {"version": 1, "palettes": {},
                                                               "typography": {}, "shape": {}}
    given = manifest["palettes"]
    palettes = {
        "light": {**_LIGHT, **given.get("light", {})},
        "dark": {**_DARK, **given.get("dark", {})},
        "oled": {**_OLED,
                 **{k: v for k, v in given.get("dark", {}).items() if k not in _OLED_OWN},
                 **given.get("oled", {})},
    }
    if check:
        failures = contrast_failures(palettes)
        if failures:
            first = failures[0]
            raise ThemeError(
                "contrast_failure",
                (f"{first['mode']}: {first['foreground']} on {first['background']} "
                 f"({first['what']}) is {first['ratio']}:1, it needs {first['minimum']}:1."),
                pointer=f"/palettes/{first['mode']}/{first['foreground']}", failures=failures)
    typography = {**TYPOGRAPHY_DEFAULTS, **manifest["typography"]}
    shape = {**SHAPE_DEFAULTS, **manifest["shape"]}
    return {"version": 1, "palettes": palettes, "typography": typography, "shape": shape}


def css_variables(resolved: dict, mode: str) -> dict[str, str]:
    """CSS custom properties for one mode, for the chat app and the Console."""
    p = resolved["palettes"][mode]
    t = resolved["typography"]
    s = resolved["shape"]
    return {
        # chat app (app.css)
        "--hz-bg": p["canvas"], "--hz-canvas": p["surface"], "--hz-raised": p["elevated"],
        "--hz-sunken": p["sunken"], "--hz-hover": p["hover"], "--hz-active": p["pressed"],
        "--hz-ink": p["text"], "--hz-body": p["bodyText"], "--hz-mute": p["mutedText"],
        "--hz-line": p["border"], "--hz-line-soft": p["borderSoft"], "--hz-input": p["input"],
        "--hz-accent": p["action"], "--hz-accent-hover": p["actionHover"],
        "--hz-accent-ink": p["actionText"], "--hz-accent-soft": p["actionSoft"],
        "--hz-accent-text": p["link"], "--hz-danger": p["errorText"],
        "--hz-danger-solid": p["danger"], "--hz-danger-ink": p["dangerText"],
        "--hz-danger-soft": p["errorBackground"],
        "--hz-success": p["successText"], "--hz-success-soft": p["successBackground"],
        "--hz-warning": p["warningText"], "--hz-warning-soft": p["warningBackground"],
        "--hz-info": p["infoText"], "--hz-info-soft": p["infoBackground"],
        "--hz-code-bg": p["surface"], "--hz-focus": p["focus"],
        "--hz-selection": p["selection"], "--hz-selection-ink": p["selectionText"],
        "--hz-font-sans": FONTS[t["fontFamily"]], "--hz-font-mono": MONO_FONTS[t["monoFamily"]],
        "--hz-line-height": str(t["lineHeight"]),
        "--hz-radius-control": f"{s['controlRadius']}px", "--hz-radius-card": f"{s['cardRadius']}px",
        # Console (portal.css)
        "--paper": p["elevated"], "--canvas": p["surface"], "--ink": p["text"],
        "--mute": p["mutedText"], "--line": p["border"], "--accent": p["link"],
        "--subtle": p["surface"], "--subtle-border": p["borderSoft"],
    }


def public(resolved: dict) -> dict:
    """What ``GET /api/chat/theme`` returns: tokens, typography, shape and the
    CSS variables of each mode. No source paths or draft data."""
    return {
        "version": 1,
        "palettes": resolved["palettes"],
        "typography": resolved["typography"],
        "shape": resolved["shape"],
        "css": {mode: css_variables(resolved, mode) for mode in MODES},
        "fonts": {"sans": FONTS[resolved["typography"]["fontFamily"]],
                  "mono": MONO_FONTS[resolved["typography"]["monoFamily"]]},
    }
