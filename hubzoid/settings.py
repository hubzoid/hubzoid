"""Hub-level settings derived from <hub>/.env and OS env.

Environment variables explicitly supported:
  MODEL                  Default LiteLLM model id used when an agent's
                         frontmatter does not specify one.
                         Examples:
                           openrouter/anthropic/claude-haiku-4.5
                           openai/gpt-4o-mini
                           anthropic/claude-haiku-4-5
  OPENROUTER_API_KEY     Key for OpenRouter (used when MODEL starts with openrouter/).
  OPENAI_API_KEY         Key for OpenAI direct.
  ANTHROPIC_API_KEY      Key for Anthropic direct.
  BRIDGE_API_KEYS        Comma-separated list of API keys the FastAPI bridge
                         will accept. Default: "dev".
  MODEL_LABEL            Optional name shown to OpenAI-compatible clients in
                         /v1/models. If blank, derived from AGENTS.md `name`.
  WEBUI_NAME             Optional Open WebUI display name. If blank, Open
                         WebUI uses its default.
  PORT                   Open WebUI port. Default: 3080.
  BRIDGE_PORT            FastAPI bridge port. Default: 8000.
  HUB_LOG_LEVEL          info | debug | warning. Default: info.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    hub_dir: Path
    model: str | None
    model_label: str | None
    bridge_api_keys: tuple[str, ...]
    webui_name: str | None
    ui_port: int
    bridge_port: int
    log_level: str

    @property
    def first_api_key(self) -> str:
        return self.bridge_api_keys[0] if self.bridge_api_keys else "dev"


def load(hub_dir: Path) -> Settings:
    """Load .env from the hub directory (if present) and bind a Settings object.

    Existing OS env vars take precedence over .env values (12-factor friendly).
    """
    env_path = hub_dir / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)

    keys_raw = os.environ.get("BRIDGE_API_KEYS", "dev")
    keys = tuple(k.strip() for k in keys_raw.split(",") if k.strip()) or ("dev",)

    return Settings(
        hub_dir=hub_dir.resolve(),
        model=os.environ.get("MODEL") or None,
        model_label=os.environ.get("MODEL_LABEL") or None,
        bridge_api_keys=keys,
        webui_name=os.environ.get("WEBUI_NAME") or None,
        ui_port=int(os.environ.get("PORT", "3080")),
        bridge_port=int(os.environ.get("BRIDGE_PORT", "8000")),
        log_level=os.environ.get("HUB_LOG_LEVEL", "info"),
    )
