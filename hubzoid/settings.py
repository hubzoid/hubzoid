"""Hub-level settings derived from <hub>/.env and OS env.

Environment variables explicitly supported:
  MODEL                  Default LiteLLM model id used when an agent's
                         frontmatter does not specify one.
                         Examples:
                           openrouter/anthropic/claude-haiku-4.5
                           openai/gpt-4o-mini
                           anthropic/claude-haiku-4-5
                           azure/<deployment-name>
  OPENROUTER_API_KEY     Key for OpenRouter (used when MODEL starts with openrouter/).
  OPENAI_API_KEY         Key for OpenAI direct.
  ANTHROPIC_API_KEY      Key for Anthropic direct.
  AZURE_API_KEY          Key for Azure OpenAI (when MODEL starts with azure/).
  AZURE_API_BASE         Azure resource endpoint, e.g.
                         https://<resource>.openai.azure.com
  AZURE_API_VERSION      Azure REST API version, e.g. 2024-10-21. Optional;
                         LiteLLM falls back to a default if unset.
  BRIDGE_API_KEYS        Comma-separated list of API keys the FastAPI bridge
                         will accept. Default: "dev".
  MODEL_LABEL            Optional name shown to OpenAI-compatible clients in
                         /v1/models. If blank, derived from AGENTS.md `name`.
  WEBUI_NAME             Optional Open WebUI display name. If blank, Open
                         WebUI uses its default.
  PORT                   Open WebUI port. Default: 3080.
  BRIDGE_PORT            FastAPI bridge port. Default: 8000.
  HUBZOID_PUBLIC_URL     Public base URL the bridge is reachable at, used to
                         build download links emitted by `write_artifact`.
                         Set this when running behind a reverse proxy / on a
                         different host than the user's browser. If unset,
                         falls back to WEBUI_URL (same public host fronts both
                         behind a proxy), then to http://127.0.0.1:<BRIDGE_PORT>
                         for localhost dev.
                         Example: HUBZOID_PUBLIC_URL=https://hub.example.com
  HUBZOID_MAX_UPLOAD_BYTES  Per-file ingress cap, in bytes. Applies to both
                         `data:` URLs decoded from chat-completion message
                         content and POSTs to `/uploads/{chat_id}/{filename}`.
                         A request whose attachment exceeds this returns 413
                         instead of being silently truncated. Default:
                         25 MiB (26214400).
  HUB_LOG_LEVEL          info | debug | warning. Default: info.
  REASONING_EFFORT       low | medium | high. Optional. Maps to the backend's
                         reasoning control: OpenAI/Azure reasoning models get
                         `reasoning_effort`; Claude gets an extended-thinking
                         token budget. Unset = the model's own default (Azure
                         keeps its built-in effort; Claude does no extended
                         thinking). Invalid values are ignored.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from . import reasoning as reasoninglib


DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB


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
    max_upload_bytes: int
    reasoning_effort: str | None = None

    @property
    def first_api_key(self) -> str:
        return self.bridge_api_keys[0] if self.bridge_api_keys else "dev"


def load(hub_dir: Path) -> Settings:
    """Load .env from the hub directory (if present) and bind a Settings object.

    `.env` is the operator's authoritative config and wins over shell env.
    Deployments that want shell-env precedence (systemd EnvironmentFile, k8s)
    simply don't ship a `.env` file.
    """
    env_path = hub_dir / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=True)

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
        max_upload_bytes=_int_env("HUBZOID_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES),
        reasoning_effort=reasoninglib.normalize(os.environ.get("REASONING_EFFORT")),
    )


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        return default
    return n if n > 0 else default
