"""The look of the connection pages: Hubzoid's own design (the web app's
"Studio" tokens: Signal Orange accent, Inter, 1px borders, no shadows), the
brand mark and the /hubzoid footer, in light and dark.

Server-rendered so they work in both UI modes, with no script but a few lines
that follow the person's chosen theme (the web app's ``hz-theme``, or Open
WebUI's ``theme``). Every value put in a page is escaped by the caller or here.
"""
from __future__ import annotations

import html
import secrets
from functools import lru_cache
from pathlib import Path

from fastapi.responses import HTMLResponse

DIST = Path(__file__).resolve().parent.parent / "portal_dist" / "assets"


@lru_cache(maxsize=1)
def _font() -> str:
    """The web app's Inter, served by the Console's static files."""
    try:
        found = sorted(DIST.glob("inter-latin-variable-*.woff2"))
    except OSError:
        found = []
    return f"/portal/assets/{found[0].name}" if found else ""


def _css() -> str:
    font = _font()
    face = (f'@font-face{{font-family:Inter;font-style:normal;font-weight:100 900;'
            f'font-display:swap;src:url("{font}") format("woff2")}}') if font else ""
    return face + _CSS


_TOKENS_DARK = """--bg:#151516;--canvas:#1b1b1d;--raised:#222224;--sunken:#26262a;--hover:#2a2a2e;
--ink:#fafaf8;--body:#e9e9e6;--mute:#b5b5bc;--line:#3c3c40;--line-soft:#2e2e32;
--accent:#e5572a;--accent-hover:#f26b40;--accent-ink:#0b0b0c;--accent-soft:#2a160f;--accent-text:#f28b66;
--danger:#ffb4b4;--danger-solid:#e0605a;--danger-soft:#3c2023;--success:#a1d8b6;--success-soft:#183328;
--warning:#e6bf72;--warning-soft:#352b19;--info:#adccf0;--info-soft:#1c2d43;--focus:#f28b66"""

_CSS = """
:root{--bg:#fff;--canvas:#fafaf8;--raised:#fff;--sunken:#f4f3ef;--hover:#f1f0ec;--ink:#0b0b0c;
--body:#1f1f22;--mute:#6b6b70;--line:#e7e6e2;--line-soft:#edece8;--accent:#b5471f;--accent-hover:#9a3b18;
--accent-ink:#fff;--accent-soft:#fbede7;--accent-text:#b5471f;--brand:#e5572a;--danger:#a3322a;
--danger-solid:#a3322a;--danger-soft:#fbedea;--success:#276444;--success-soft:#eaf4ed;--warning:#805500;
--warning-soft:#fff5da;--info:#345e91;--info-soft:#eef3fa;--focus:#b5471f;color-scheme:light}
:root[data-theme="dark"]{""" + _TOKENS_DARK + """;color-scheme:dark}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){""" + _TOKENS_DARK + """;color-scheme:dark}}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--canvas);color:var(--ink);font:15px/1.5 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
-webkit-font-smoothing:antialiased;display:flex;flex-direction:column;min-height:100vh}
a{color:var(--accent-text)}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:6px}
.top{display:flex;align-items:center;justify-content:space-between;padding:16px 20px}
.brand{display:flex;align-items:center;gap:10px;min-width:0;color:var(--ink);text-decoration:none}
.top-end{display:flex;align-items:center;gap:8px;min-width:0}
.mark{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;flex:none;
border:1px solid var(--line);border-radius:7px;background:var(--raised);
font:600 13px/1 "JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink)}
.mark b,.slash{color:var(--brand);font-weight:600}
.brand-name{font-weight:600;font-size:15px;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
main{flex:1;display:flex;justify-content:center;align-items:flex-start;padding:6vh 16px 48px}
.wrap{width:100%;max-width:420px}.wrap.wide{max-width:640px}
.card{background:var(--raised);border:1px solid var(--line);border-radius:12px;padding:28px}
.eyebrow{font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--mute);margin:0 0 6px}
h1{margin:0;font-size:22px;line-height:1.25;font-weight:650;letter-spacing:-.015em;color:var(--ink)}
.lead{margin:8px 0 0;color:var(--body);font-size:15px}.muted{color:var(--mute)}
p{margin:8px 0 0}
.pair{display:flex;align-items:center;gap:10px;margin-bottom:20px}
.tile{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;flex:none;
border-radius:11px;border:1px solid var(--line);background:var(--sunken);color:var(--ink);font-weight:650;font-size:18px}
.tile.agent{background:var(--raised);font:600 16px/1 "JetBrains Mono",ui-monospace,Menlo,monospace}
.tile.app{background:var(--accent-soft);border-color:transparent;color:var(--accent-text)}
.tile.sm{width:36px;height:36px;border-radius:9px;font-size:15px}
.tile.ok{background:var(--success-soft);border-color:transparent;color:var(--success)}
.tile.warn{background:var(--warning-soft);border-color:transparent;color:var(--warning)}
.tile.bad{background:var(--danger-soft);border-color:transparent;color:var(--danger)}
.link{flex:none;width:28px;height:1px;background:var(--line);position:relative}
.link svg{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);background:var(--raised);
color:var(--mute);padding:0 3px;box-sizing:content-box}
dl{margin:20px 0 0;border-top:1px solid var(--line-soft)}
dl div{display:flex;align-items:center;justify-content:space-between;gap:16px;min-height:44px;padding:8px 0;border-bottom:1px solid var(--line-soft);font-size:14px}
dt{color:var(--mute);flex:none}dd{margin:0;text-align:right;color:var(--ink);min-width:0;overflow-wrap:anywhere}
.chip{display:inline-flex;align-items:center;gap:8px;padding:3px 10px 3px 3px;border:1px solid var(--line);
border-radius:999px;background:var(--raised);font-size:13px;color:var(--body);max-width:100%}
.av{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;flex:none;border-radius:50%;
background:var(--sunken);color:var(--ink);font-size:11px;font-weight:650}
.chip span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
details.help{margin-top:16px;font-size:13px;color:var(--mute)}
details.help summary{cursor:pointer;list-style:none;display:inline-flex;align-items:center;gap:6px;color:var(--mute)}
details.help summary::-webkit-details-marker{display:none}
details.help summary .q{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;
border:1.3px solid currentColor;border-radius:50%;font-size:10px;font-weight:700}
details.help[open] summary{color:var(--ink)}details.help p{margin:8px 0 0;line-height:1.55}
.actions{display:flex;flex-direction:column;gap:8px;margin-top:24px}
.actions.row{flex-direction:row;flex-wrap:wrap}
form{margin:0}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:40px;padding:0 16px;
border-radius:8px;border:1px solid transparent;font:500 14px/1 inherit;font-family:inherit;cursor:pointer;
text-decoration:none;white-space:nowrap;transition:background-color .12s,border-color .12s,color .12s;width:100%}
.actions.row .btn,.row-actions .btn{width:auto}
.btn-primary{background:var(--accent);color:var(--accent-ink)}.btn-primary:hover{background:var(--accent-hover)}
.btn-secondary{background:var(--raised);color:var(--ink);border-color:var(--line)}.btn-secondary:hover{background:var(--hover)}
.btn-ghost{background:transparent;color:var(--mute)}.btn-ghost:hover{background:var(--hover);color:var(--ink)}
.btn-danger{background:var(--danger-solid);color:#fff}:root[data-theme="dark"] .btn-danger{color:#0b0b0c}
.btn-sm{min-height:32px;padding:0 12px;font-size:13px;border-radius:7px}
.badge{display:inline-flex;align-items:center;gap:5px;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:500;
line-height:20px;white-space:nowrap;background:var(--sunken);color:var(--mute)}
.badge.ok{background:var(--success-soft);color:var(--success)}.badge.warn{background:var(--warning-soft);color:var(--warning)}
.badge.info{background:var(--info-soft);color:var(--info)}.badge.bad{background:var(--danger-soft);color:var(--danger)}
.dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.list{margin-top:20px;border:1px solid var(--line);border-radius:12px;background:var(--raised)}
.item{display:flex;gap:14px;align-items:flex-start;padding:16px 18px;border-top:1px solid var(--line-soft)}
.item:first-child{border-top:0}
.item-main{flex:1;min-width:0}.item-title{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-weight:600;font-size:15px}
.item-meta{margin-top:2px;font-size:13px;color:var(--mute)}
.row-actions{display:flex;gap:8px;align-items:center;flex:none}
details.confirm{position:relative}details.confirm summary{list-style:none}
details.confirm summary::-webkit-details-marker{display:none}
details.confirm .pop{position:absolute;right:0;top:calc(100% + 6px);z-index:5;width:260px;padding:14px;
background:var(--raised);border:1px solid var(--line);border-radius:10px;font-size:13px;color:var(--body)}
details.confirm .pop .actions{margin-top:12px}
.banner{display:flex;gap:10px;align-items:center;padding:10px 14px;border-radius:10px;font-size:14px;margin-top:16px}
.banner.ok{background:var(--success-soft);color:var(--success)}.banner.bad{background:var(--danger-soft);color:var(--danger)}
.empty{text-align:center;padding:36px 20px;color:var(--mute);font-size:14px}
.empty .tile{margin:0 auto 12px}
.spin{width:18px;height:18px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;
animation:sp 0.8s linear infinite;flex:none}@keyframes sp{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
.waiting{display:flex;align-items:center;gap:10px;margin-top:12px;color:var(--body)}
footer{padding:20px;text-align:center;font-size:12px;color:var(--mute)}
@media (max-width:520px){.card{padding:22px 18px}.item{flex-wrap:wrap}.row-actions{width:100%;justify-content:flex-end}
details.confirm .pop{right:auto;left:auto;right:0}main{padding-top:3vh}}
"""

_THEME = ("(function(){function a(){try{var m=localStorage.getItem('hz-theme')||localStorage.getItem('theme')"
          "||'system';var d=m.indexOf('dark')>=0||(m==='system'&&matchMedia('(prefers-color-scheme: dark)')"
          ".matches);document.documentElement.setAttribute('data-theme',d?'dark':'light')}catch(e){}}a();"
          "try{matchMedia('(prefers-color-scheme: dark)').addEventListener('change',a)}catch(e){}})();")

ICONS = {
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "alert": '<path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "arrow": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    "back": '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>',
    "plug": '<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8z"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
}


def icon(name: str, size: int = 18) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            f'{ICONS[name]}</svg>')


def esc(value) -> str:
    return html.escape(str(value or ""))


def initial(name: str) -> str:
    s = (name or "?").strip()
    return esc(s[:1].upper() or "?")


def mark(name: str) -> str:
    """The brand mark: '/' and the name's first letter, as the web app shows it."""
    return f'<span class="mark" aria-hidden="true"><b>/</b>{esc((name or "h")[:1].lower())}</span>'


def avatar_chip(email: str) -> str:
    return f'<span class="chip"><span class="av" aria-hidden="true">{initial(email)}</span><span>{esc(email)}</span></span>'


def help_text(summary: str, paragraphs: list[str]) -> str:
    """Details behind a ?: opens by click, tap or keyboard, no script."""
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (f'<details class="help"><summary><span class="q" aria-hidden="true">?</span>{esc(summary)}'
            f"</summary>{body}</details>")


def rows(items: list[tuple[str, str]]) -> str:
    """A short definition list. Values are HTML the caller escaped."""
    if not items:
        return ""
    return "<dl>" + "".join(f"<div><dt>{esc(k)}</dt><dd>{v}</dd></div>" for k, v in items) + "</dl>"


def shell(title: str, body: str, *, status: int = 200, brand: str = "", wide: bool = False,
          script: str = "", account: str = "", chat: str = "") -> HTMLResponse:
    """The page: top bar with the agent's (or Hubzoid's) mark and name (and a
    way back to the ``chat`` and the signed-in ``account``), the content, the
    /hubzoid footer. ``body`` is HTML the caller escaped."""
    nonce = secrets.token_urlsafe(12)
    brand_html = (f'<span class="brand">{mark(brand)}<span class="brand-name">{esc(brand)}</span></span>'
                  if brand else
                  '<span class="brand"><span class="brand-name"><span class="slash">/</span>hubzoid</span></span>')
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{esc(title)}{' · ' + esc(brand) if brand else ''}</title>"
        f'<script nonce="{nonce}">{_THEME}</script>'
        f"<style>{_css()}</style></head><body>"
        f'<header class="top">{brand_html}<span class="top-end">'
        + (f'<a class="btn btn-ghost btn-sm" href="{esc(chat)}">{icon("back", 15)}Back to chat</a>' if chat else "")
        + f'{avatar_chip(account) if account else ""}</span></header>'
        f'<main id="main"><div class="wrap{" wide" if wide else ""}" aria-live="polite">{body}</div></main>'
        f'<footer>{"<span class=slash>/</span>hubzoid" if brand else "Hubzoid"}</footer>'
        + (f'<script nonce="{nonce}">{script}</script>' if script else "")
        + "</body></html>")
    return HTMLResponse(doc, status_code=status, headers=headers(nonce))


def headers(nonce: str | None = None) -> dict:
    script = f"'nonce-{nonce}'" if nonce else "'none'"
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "same-origin",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'unsafe-inline'; script-src {script}; "
            "font-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
            "frame-ancestors 'none'"),
    }
