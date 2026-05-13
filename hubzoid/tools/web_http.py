"""HTTP / web tools.

http_get: GET a URL (no JS, no auth). Domain allowlist optional via HTTP_ALLOWLIST env.
web_search: thin wrapper over DuckDuckGo HTML results (no API key needed).
"""
from __future__ import annotations

import os
import re
from urllib.parse import quote_plus, urlparse

import httpx
from agents import function_tool

_DEFAULT_TIMEOUT = 15.0


def _allowed(url: str) -> bool:
    allow = os.environ.get("HTTP_ALLOWLIST", "").strip()
    if not allow:
        return True
    host = urlparse(url).hostname or ""
    return any(host == d.strip() or host.endswith("." + d.strip()) for d in allow.split(",") if d.strip())


def make(ctx) -> list:  # noqa: ARG001
    @function_tool
    def http_get(url: str) -> str:
        """GET a URL and return the response body as text.

        Honors HTTP_ALLOWLIST env (comma-separated hostnames) if set.

        Args:
            url: Full URL including scheme.

        Returns:
            Response body (truncated to 50_000 chars) or an `[error: ...]` message.
        """
        if not url.lower().startswith(("http://", "https://")):
            return "[http_get: only http/https URLs are allowed]"
        if not _allowed(url):
            return f"[http_get refused: {urlparse(url).hostname} is not in HTTP_ALLOWLIST]"
        try:
            with httpx.Client(timeout=_DEFAULT_TIMEOUT, follow_redirects=True) as client:
                r = client.get(url, headers={"User-Agent": "hubzoid/0.1"})
            body = r.text
            if len(body) > 50_000:
                body = body[:50_000] + "\n[truncated]"
            return f"HTTP {r.status_code}\n\n{body}"
        except httpx.HTTPError as exc:
            return f"[http_get error: {exc}]"

    @function_tool
    def web_search(query: str, limit: int = 5) -> str:
        """Search the web via DuckDuckGo and return top results.

        Args:
            query: Search query.
            limit: Max results (default 5).

        Returns:
            Markdown bullet list of `title — url\\n  snippet`.
        """
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        try:
            with httpx.Client(timeout=_DEFAULT_TIMEOUT, follow_redirects=True) as client:
                r = client.get(url, headers={"User-Agent": "hubzoid/0.1"})
            html = r.text
        except httpx.HTTPError as exc:
            return f"[web_search error: {exc}]"

        # Light HTML parsing without bs4 — enough for v1.
        results = []
        for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
            r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            html, re.DOTALL,
        ):
            href, title, snippet = m.group(1), _strip_tags(m.group(2)), _strip_tags(m.group(3))
            results.append((title.strip(), href.strip(), snippet.strip()))
            if len(results) >= max(1, limit):
                break
        if not results:
            return "(no results)"
        return "\n".join(f"- **{t}** — {u}\n  {s}" for t, u, s in results)

    return [http_get, web_search]


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)
