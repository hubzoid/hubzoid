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
  SHOW_THINKING          off | indicator | full. Claude backend only. Controls
                         how Claude's thinking is surfaced in chat. Opus already
                         thinks by default but hides the text, leaving a dead
                         spinner during the reasoning gap.
                           indicator (default) -> show a "Thinking…" panel for
                             the reasoning duration, without exposing the text.
                           full -> stream the summarized reasoning text.
                           off -> surface nothing (legacy behaviour).
                         Aliases: true->full, false->off. Independent of
                         REASONING_EFFORT (which only sets how *much* it thinks).
  SHOW_TOOLS             off | compact | full. Controls how tool-call activity
                         (e.g. `read_knowledge`, `grep_data`) is surfaced.
                           compact (default) -> a collapsible dropdown per call
                             on the web UI; hidden on Slack.
                           full -> the legacy inline `> ✓ tool` blockquote on
                             every surface (verbose; useful for debugging).
                           off -> emit nothing.
                         Aliases: true->compact, false/hide->off, inline->full.
  MCP_SERVER             true | false (default). Serve this hub as a hosted
                         MCP server at /mcp on the bridge (exposed publicly by
                         the edge). External MCP clients (Claude Code, Cursor)
                         authenticate with the caller's own Open WebUI API key
                         and get the hub's tools + knowledge under the same
                         per-group access rules as chat. Enterprise feature
                         (notice-only). See docs/mcp-server.md.
  MCP_ACCESS_GROUP       Optional OWUI group name gating the WHOLE /mcp
                         surface: only members get past auth (401 otherwise).
                         Essential in gateway mode, where one shared user DB
                         backs every hub — without it, any logged-in user of
                         any team can reach this hub's unrestricted tools and
                         knowledge. Unset = every authenticated OWUI user.
  SLACK_IDENTITY_MAPPING true | false (default). When true, the Slack adapter
                         resolves each sender's verified Slack profile email
                         (needs the `users:read.email` manifest scope) and
                         forwards it to the bridge, which looks up that user's
                         Open WebUI groups. This maps a Slack user to their OWUI
                         identity so per-group permissions apply over Slack.
                         This flag only supplies the identity; restricted tools
                         still require opting the surface into
                         HUBZOID_RESTRICTED_SURFACES (below). Emails must match
                         between Slack and OWUI (matched case-insensitively); no
                         match falls back to anonymous.
  HUBZOID_RESTRICTED_SURFACES
                         Comma-separated COMPLETE list of surfaces that may
                         reach access-controlled tools. Unset = the built-in
                         default `owui,web,api,mcp`. Setting it REPLACES the
                         default (it does not extend it), so include the
                         defaults you still want, e.g.
                         `owui,web,api,mcp,slack-dm`. Slack surfaces are split:
                         `slack-dm` (1:1 DM / assistant sidebar — one human,
                         safe) and `slack-channel` (shared thread with many
                         authors, answered under the @mentioner's identity —
                         a confused-deputy risk). Add `slack-dm` if you want
                         restricted tools in Slack DMs; NEVER add
                         `slack-channel`.
  COMPOSIO_API_KEY       Key for the Composio credential broker, which stores
                         each user's per-app credentials keyed by their
                         identity. Required for CONNECTIONS to work.
  CONNECTIONS            Comma-separated allow-list of app slugs this hub's
                         users may connect, e.g. "odoo,slack". A tool calls
                         `connections.require(app)`; only listed apps are ever
                         offered, and unlisted ones are refused without touching
                         the broker. Unset = per-user connections off. See
                         hubzoid.connections.
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
    thinking_mode: str = "indicator"
    show_tools: str = "compact"
    mcp_server: bool = False
    mcp_access_group: str | None = None
    slack_identity_mapping: bool = False
    otel_endpoint: str | None = None
    composio_api_key: str | None = None
    connections: tuple[str, ...] = ()

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

    # Secrets for access-controlled tools live in <hub>/restricted/.env — the
    # restricted/ folder the file-reading tools refuse, so the model cannot read
    # them. Load it (after the main .env) into the process env, where only the
    # restricted tools' own code reads them. See hubzoid.access.
    restricted_env = hub_dir / "restricted" / ".env"
    if restricted_env.is_file():
        load_dotenv(restricted_env, override=True)

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
        thinking_mode=reasoninglib.normalize_thinking(os.environ.get("SHOW_THINKING")),
        show_tools=reasoninglib.normalize_tools(os.environ.get("SHOW_TOOLS")),
        mcp_server=truthy(os.environ.get("MCP_SERVER")),
        mcp_access_group=(os.environ.get("MCP_ACCESS_GROUP") or "").strip() or None,
        slack_identity_mapping=truthy(os.environ.get("SLACK_IDENTITY_MAPPING")),
        otel_endpoint=(os.environ.get("HUBZOID_OTEL_ENDPOINT") or "").strip() or None,
        composio_api_key=(os.environ.get("COMPOSIO_API_KEY") or "").strip() or None,
        connections=_conn_slugs(os.environ.get("CONNECTIONS")),
    )


def _conn_slugs(raw: str | None) -> tuple[str, ...]:
    """Parse CONNECTIONS: comma-separated app slugs, trimmed, lowercased."""
    if not raw:
        return ()
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def truthy(raw: str | None) -> bool:
    """The single yes/no rule for hubzoid boolean env vars."""
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        return default
    return n if n > 0 else default
