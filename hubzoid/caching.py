"""Prompt caching for LiteLLM-routed providers.

Anthropic-family models (anthropic/*, openrouter/anthropic/*, bedrock claude)
require explicit `cache_control: {"type": "ephemeral", "ttl": "1h"}` blocks
to cache the system prompt + conversation history. Cached reads cost ~10% of
full input tokens; the 1h TTL absorbs normal chat think-time between turns.

OpenAI caches the system + prefix automatically server-side when a request
is large enough (no client config needed). Gemini context caching uses a
separate `CachedContent` API not wired here.

We monkey-patch litellm.acompletion at install time so caching works
regardless of which path constructs the request (openai-agents SDK, direct
LiteLLM calls, etc.). Idempotent. Opt out with HUBZOID_DISABLE_PROMPT_CACHE=True.
Override TTL with HUBZOID_CACHE_TTL=5m if you want the cheaper short cache.
"""
from __future__ import annotations

import os
from typing import Any

_INSTALLED = False
_CACHEABLE_MARKERS = ("anthropic", "claude", "bedrock")
_CACHE_TTL = os.environ.get("HUBZOID_CACHE_TTL", "1h").strip().lower()


def _cache_control_value() -> dict:
    cc: dict[str, Any] = {"type": "ephemeral"}
    if _CACHE_TTL and _CACHE_TTL != "5m":
        cc["ttl"] = _CACHE_TTL
    return cc


def _supports_cache_control(model: str | None) -> bool:
    if not model:
        return False
    m = model.lower()
    return any(marker in m for marker in _CACHEABLE_MARKERS)


def _as_block_list(content: Any) -> list[dict] | None:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return None


def _mark_last_text_block(message: dict) -> bool:
    blocks = _as_block_list(message.get("content"))
    if not blocks:
        return False
    for block in reversed(blocks):
        if isinstance(block, dict) and block.get("type") == "text":
            block["cache_control"] = _cache_control_value()
            message["content"] = blocks
            return True
    return False


def _inject(messages: list[dict], model: str | None) -> None:
    """Add up to 2 cache_control breakpoints (Anthropic allows 4).

    1. Last text block of the system message -> caches system + tool defs.
    2. Last assistant message -> caches the conversation history up through
       the previous turn. The latest user message stays uncached so its
       tokens count as fresh.
    """
    if not _supports_cache_control(model) or not messages:
        return

    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            _mark_last_text_block(msg)
            break

    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            _mark_last_text_block(msg)
            break


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    if os.environ.get("HUBZOID_DISABLE_PROMPT_CACHE", "").lower() in ("true", "1", "yes"):
        _INSTALLED = True
        return

    import litellm

    _orig_acompletion = litellm.acompletion
    _orig_completion = litellm.completion

    async def _wrapped_acompletion(*args, **kwargs):
        model = kwargs.get("model") or (args[0] if args else None)
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            _inject(messages, model)
        return await _orig_acompletion(*args, **kwargs)

    def _wrapped_completion(*args, **kwargs):
        model = kwargs.get("model") or (args[0] if args else None)
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            _inject(messages, model)
        return _orig_completion(*args, **kwargs)

    litellm.acompletion = _wrapped_acompletion
    litellm.completion = _wrapped_completion
    _INSTALLED = True
