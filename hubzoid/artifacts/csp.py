# Hubzoid artifacts. Apache-2.0 licensed like the rest of the repository.
"""The one Content Security Policy for agent-made HTML shown by Hubzoid.

Used by the web app's Documents pane (``/api/chat/artifact-frame`` and the
``blob:`` page inside it) and by the published viewer
(``/portal/artifacts/<id>/content``), so a document renders the same in both.

The page runs in an opaque origin (the sandbox, set by each view). This policy
adds: scripts and styles inline, or from a short list of public script CDNs
(the chart libraries agents load); images, fonts and media only from
``data:``/``blob:``; and no ``fetch``, XHR, WebSocket, beacon or form request
anywhere (``connect-src 'none'``, ``form-action 'none'``).

``HUBZOID_ARTIFACT_ALLOW_ORIGINS`` replaces the CDN list: comma-separated
``https://`` origins, which may then serve scripts, styles, fonts and images;
``none`` allows no outside origin at all (air-gapped deployments).
"""
from __future__ import annotations

import os
import re

#: Where agent-made pages load chart libraries from by default.
DEFAULT_ORIGINS = ("https://cdn.jsdelivr.net", "https://cdnjs.cloudflare.com", "https://unpkg.com")

_ORIGIN = re.compile(r"^https://[A-Za-z0-9.-]+(:\d{1,5})?$")


def configured() -> tuple[str, ...] | None:
    """The operator's origins (``HUBZOID_ARTIFACT_ALLOW_ORIGINS``), ``()`` for
    ``none``, or None when the variable is unset."""
    raw = os.environ.get("HUBZOID_ARTIFACT_ALLOW_ORIGINS")
    if raw is None or not raw.strip():
        return None
    if raw.strip().lower() == "none":
        return ()
    return tuple(o.strip() for o in raw.split(",") if _ORIGIN.match(o.strip()))


def policy(*, frames: str = "'none'") -> str:
    """The resource policy, without a sandbox or ``frame-ancestors``."""
    custom = configured()
    if custom is None:
        code = " " + " ".join(DEFAULT_ORIGINS)
        media = ""
    else:
        code = media = "".join(" " + o for o in custom)
    return ("default-src 'none'; "
            f"script-src 'unsafe-inline'{code}; style-src 'unsafe-inline'{code}; "
            f"img-src data: blob:{media}; font-src data:{media}; media-src data: blob:; "
            f"connect-src 'none'; frame-src {frames}; form-action 'none'; base-uri 'none'")
