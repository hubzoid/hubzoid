"""render_jinja — render a Jinja2 template string with a context dict.

Useful for templated outputs (emails, reports, generated config files). The
template is rendered in a sandbox with no I/O — pure string in, string out.
"""
from __future__ import annotations

import json

from agents import function_tool
from jinja2 import StrictUndefined, Template
from jinja2.exceptions import TemplateError


def make(ctx) -> list:  # noqa: ARG001
    @function_tool
    def render_jinja(template: str, context_json: str = "{}") -> str:
        """Render a Jinja2 template with a JSON-encoded context.

        Args:
            template: Jinja2 template source.
            context_json: JSON object string. Empty string or "{}" means no variables.

        Returns:
            The rendered string, or an `[error: ...]` message.
        """
        try:
            ctx_obj = json.loads(context_json or "{}")
            if not isinstance(ctx_obj, dict):
                return "[render_jinja: context must be a JSON object]"
        except json.JSONDecodeError as exc:
            return f"[render_jinja: invalid JSON context — {exc}]"
        try:
            return Template(template, undefined=StrictUndefined).render(**ctx_obj)
        except TemplateError as exc:
            return f"[render_jinja: {exc}]"

    return [render_jinja]
