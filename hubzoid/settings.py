"""Hub-level settings derived from <hub>/.env and OS env.

Configuration layers, lowest precedence first. A later layer overrides an
earlier one for the processes it reaches (see docs/DEPLOYING.md and
hubzoid.config_secrets).

  1  built-in          the defaults below                        all processes
  2a deployment compat Open WebUI and sign-in keys from hub .env  gateway and Open WebUI
                       files, only when 2b and 2c lack them
                       (0.9.x compatibility)
  2b deployment        the gateway's (or a standalone hub's)      see below
                       process environment. Standalone: also
                       the hub .env
  2c deployment secret JSON secret named by AWS_SECRET_NAME       see below
  3a hub               <hub>/.env                                that hub's bridge, inbound,
                                                                 Slack and agent runtime
  3b hub secret        JSON secret named by                      same as 3a
                       HUBZOID_HUB_SECRET_NAME in <hub>/.env
  4a restricted        <hub>/restricted/.env                     that hub's restricted tools,
                                                                 in the bridge process only
  4b restricted secret JSON secret named by                      same as 4a
                       HUBZOID_RESTRICTED_SECRET_NAME in
                       restricted/.env

Within a layer the AWS secret wins over the file. A standalone hub applies its
deployment secret after <hub>/.env, so the secret wins there too. A hub in a
gateway takes only BRIDGE_DEPLOYMENT_KEYS from the deployment secret, before
its own .env. Open WebUI gets the deployment layer without HUBZOID_* keys and
never a hub or restricted layer. Agent child processes (the claude CLI and its
stdio MCP servers) get service secrets, AWS credentials and restricted-layer
keys blanked (config_secrets.child_env_overrides). With no secret named, the
files load exactly as before: <hub>/.env then restricted/.env, both overriding
the process environment.

Environment variables explicitly supported:
  MODEL                  Default LiteLLM model id used when an agent's
                         frontmatter does not specify one.
                         Examples:
                           openrouter/anthropic/claude-haiku-4.5
                           openai/gpt-4o-mini
                           anthropic/claude-haiku-4-5
                           azure/<deployment-name>
  OPENROUTER_API_KEY     Key for OpenRouter (used when MODEL starts with openrouter/).
  JEV_OPENROUTER_API_KEY Dedicated OpenRouter key for Jev decisions only
                         (hub.call_jev in workflows, the jev-gated call_jev chat
                         tool). The chat model never uses it, and Jev never
                         falls back to OPENROUTER_API_KEY.
  OPENAI_API_KEY         Key for OpenAI direct.
  ANTHROPIC_API_KEY      Key for Anthropic direct.
  AZURE_API_KEY          Key for Azure OpenAI (when MODEL starts with azure/).
  AZURE_API_BASE         Azure resource endpoint, e.g.
                         https://<resource>.openai.azure.com
  AZURE_API_VERSION      Azure REST API version, e.g. 2024-10-21. Optional;
                         LiteLLM falls back to a default if unset.
  BRIDGE_API_KEYS        Comma-separated list of API keys the FastAPI bridge
                         will accept. Default: "dev", which is public: set a
                         random key for any shared or deployed install. The
                         default key is not accepted on the public /artifacts
                         route.
  HUBZOID_ARTIFACT_SECRET  Secret that signs artifact download links. Default:
                         generated per hub in .hubzoid/artifact_secret (0600).
                         Set it only to share one secret across hosts serving
                         the same hub. Changing or deleting it revokes every
                         issued link.
  HUBZOID_ARTIFACT_LINK_TTL  Seconds a newly issued download link stays valid.
                         Default: 0 (links never expire).
  HUBZOID_WORKFLOW_USER  Account email scheduled workflows and schedule/*.md
                         tasks run as when their declaration has no run_as.
                         Hub value wins over the deployment's. Unset: the
                         owner recorded at setup (locally admin@localhost).
                         See docs/workflow-identity.md.
  HUBZOID_ARTIFACT_MAX_BYTES  Largest file hub.publish_artifact accepts.
                         Default: 52428800 (50 MiB).
  HUBZOID_ARTIFACT_LINK_DAYS  Default lifetime of a report's public link, in
                         days (1-90). Default: 7.
  HUBZOID_ARTIFACT_ALLOW_ORIGINS  Comma-separated https origins published HTML
                         reports may load scripts, styles, fonts and images
                         from (e.g. a chart CDN). Default: none (reports are
                         self-contained and make no network requests).
  HUBZOID_SMTP_HOST / _PORT / _USERNAME / _PASSWORD / _FROM  The SMTP sender
                         for hub.send_email (owner-only email). Port default
                         587. Credentials are only sent over TLS.
  HUBZOID_SMTP_STARTTLS  Upgrade with STARTTLS. Default: true.
  HUBZOID_SMTP_SSL       Implicit TLS (usually port 465). Default: false.
  HUBZOID_SMTP_TIMEOUT   Seconds per SMTP operation. Default: 30.
  HUBZOID_EMAIL_DELIVERY smtp | preview. preview writes each email to
                         .hubzoid/outbox/ and reports that nothing was sent.
                         Default: smtp. See docs/reports-and-email.md.
  MODEL_LABEL            Optional name shown to OpenAI-compatible clients in
                         /v1/models. If blank, derived from AGENTS.md `name`.
  ENABLE_SIGNUP          Open WebUI public email registration. Default: false;
                         administrators add accounts through the chat admin panel.
  ENABLE_OAUTH_SIGNUP    Automatic account creation through SSO. Default: false;
                         explicitly opt in for an SSO onboarding deployment.
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
                           full -> the legacy inline `> ↳ tool` blockquote on
                             every surface (verbose; useful for debugging).
                           off -> emit nothing.
                         Aliases: true->compact, false/hide->off, inline->full.
  MCP_SERVER             true | false (default). Serve this hub as a hosted
                         MCP server at /mcp on the bridge (exposed publicly by
                         the edge). External MCP clients (Claude Code, Cursor)
                         authenticate with the caller's own Open WebUI API key
                         and get the hub's tools + knowledge under the same
                         per-group access rules as chat. See docs/mcp-server.md.
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
  HUBZOID_OTEL_ENDPOINT  OTLP/HTTP base URL to push traces to (e.g. a Langfuse
                         `.../api/public/otel`). Unset = observability off. See
                         docs/OBSERVABILITY.md.
  HUBZOID_OTEL_NORMALIZE true | false (default). claude-local only. When true,
                         the bridge intercepts the `claude` subprocess's OTLP
                         in-process, renames Claude Code's non-standard token
                         attrs to the `gen_ai.usage.*` names Langfuse maps, and
                         promotes the OWUI user to span `user.id` — so Langfuse
                         shows cost + the real user WITHOUT running a separate
                         collector. No-op on the OpenAI/LiteLLM path (already
                         standard). See docs/OBSERVABILITY.md.
  HUBZOID_OPENAI_TRACING true | false (default). OpenAI Agents backend only.
                         When true, the OpenAI Agents SDK's own tracing exports
                         runs (including prompts and tool data) to OpenAI's
                         trace dashboard. Off by default so an OPENAI_API_KEY
                         never sends run content anywhere undeclared.
                         HUBZOID_OTEL_ENDPOINT is separate and unaffected.
  HUBZOID_BROWSER        true | false (default). Give every agent in this hub a
                         shared, resource-limited web browser as the full
                         Playwright MCP toolset (browser_navigate, browser_click,
                         browser_snapshot, ...). One browser is shared across all
                         agents, so N agents no longer mean N browsers. See
                         hubzoid.browser and docs/BROWSER.md.
  HUBZOID_BROWSER_PORT   Port the playwright-mcp sidecar listens on. Default 8931.
  HUBZOID_BROWSER_MCP_URL
                         If set, HubZoid does NOT spawn a sidecar — it connects
                         agents to this already-running playwright-mcp endpoint
                         (docker-compose / production manages the sidecars). If
                         unset, HubZoid spawns playwright-mcp itself (dev).
  HUBZOID_BROWSER_CDP_URL
                         A CDP browser-pool endpoint (browserless) the spawned
                         playwright-mcp attaches to, giving hard concurrency +
                         memory limits (one action at a time, the rest queue).
                         MUST be the ws form so the token survives:
                         ws://host:3000?token=... . If unset, playwright-mcp
                         launches its own shared browser (shared, but no queue).
  HUBZOID_BROWSER_CHANNEL
                         Browser build for the sidecar. Default `chromium`
                         (the Playwright-bundled build — required in containers,
                         which have no system Chrome). Do not use `chrome`
                         unless a real Chrome is installed on the sidecar host.
  HUBZOID_BROWSER_CONCURRENT | _QUEUED | _TIMEOUT | _MEMORY
                         Pool limits enforced by the browserless container:
                         parallel browser slots (default 2), waiters before
                         rejection (5), stuck-session timeout in seconds (60),
                         and the hard container memory ceiling (2g). Read by the
                         shipped docker/browser-compose.yml via ${...}.
  HUBZOID_OPERATIONAL_DB  Shared access, identity and audit database URL. A
                         registered gateway manifest is authoritative; conflicting
                         overrides fail startup rather than splitting access data.
  HUBZOID_DBOS_DB        Workflow system database URL. SQLite is per hub; a
                         gateway may share PostgreSQL. Must agree with manifest.
  HUBZOID_DEPLOYMENT     Explicit gateway manifest path; normally discovered via
                         <hub>/.hubzoid/deployment.json. Do not copy hub pointers
                         into an unrelated deployment.
  OWUI_INTERNAL_URL     Server-to-server Open WebUI URL; manifest-authoritative.
                         WEBUI_URL is a standalone fallback (not a gateway override).
  HUBZOID_SCHEDULES      Enable workflow dispatch for standalone runs (1/true).
                         Gateways enable it automatically. Missed slots are reported,
                         not replayed. Markdown schedules retain their own gate.
  HUBZOID_PORTAL_DEV_USER
                         Subject trusted only when HUBZOID_PORTAL_DEV=1.
  HUBZOID_PORTAL_DEV     Development-only portal authentication; never enable on
                         a shared or public deployment. See ADMINISTRATION.md.
  HUBZOID_BROWSER_MAX_RSS_MB
                         Direct-mode safety net only (no browserless): restart
                         the spawned browser if its process-tree RSS exceeds this
                         many MB. 0 (default) = watchdog off.
  AWS_SECRET_NAME        Deployment layer. Name or ARN of an AWS Secrets Manager
                         secret holding a flat JSON object of settings, read at
                         start with boto3's default credential chain (instance
                         or task role, AWS_PROFILE, or AWS_ACCESS_KEY_ID and
                         AWS_SECRET_ACCESS_KEY plus AWS_SESSION_TOKEN). Set it in
                         the gateway's environment, or for a standalone hub in
                         its .env or environment. The gateway records it in
                         deployment.json so external bridges find it. A hub
                         .env naming it inside a gateway is ignored. Unset = no
                         boto3 import and no network call.
  AWS_REGION             Region for every secret fetch (AWS_DEFAULT_REGION is the
                         fallback, and a secret ARN carries its own region).
  HUBZOID_HUB_SECRET_NAME
                         Hub layer. A secret whose keys override <hub>/.env for
                         this hub only. Read from <hub>/.env and nowhere else.
  HUBZOID_RESTRICTED_SECRET_NAME
                         Restricted layer. A secret whose keys override
                         restricted/.env for this hub's restricted tools. Read
                         from restricted/.env and nowhere else.
                         A secret may not set AWS_* keys, secret names or
                         process-control keys (PATH, LD_*, PYTHONPATH). Values
                         are strings, numbers or booleans. A secret that cannot
                         be read stops the process at start with the secret
                         name, the layer and the AWS error class, never a value.
                         Values are read once. Restart after a rotation: the
                         gateway and every bridge for the deployment secret,
                         the hub's bridge, inbound and Slack processes for a hub
                         secret, the hub's bridge for a restricted secret.
  HUBZOID_DEPLOYMENT_SECRET_INHERITED
                         Internal. Set by `hubzoid gateway` on the bridges it
                         launches, which then use the deployment values the
                         gateway passed instead of fetching the secret again.
  HUBZOID_MANAGEMENT_TOOLS
                         Hub. true | false (default). Registers the agent tools
                         that propose access changes and new accounts
                         (my_management_scope, propose_access_change,
                         propose_new_account). Effective only on managed hubs. A
                         proposal applies only after the same manager confirms
                         it in the Console.
  HUBZOID_CHANGE_REQUEST_TTL
                         Seconds a proposed access change waits for
                         confirmation in the Console. Default 900.
  HUBZOID_HIDE_OWUI_USERS
                         Deployment and edge. true | false. Sends the Open
                         WebUI user list to Console People, lands its Users
                         section on Groups and blocks browser writes to Open
                         WebUI's account endpoints. Unset: on for a gateway set
                         up fresh with Console accounts (recorded in
                         deployment.json), off for existing deployments.
  HUBZOID_CONNECT_JOURNEY
                         Hub. true | false (default). Enables the
                         connect_account tool and its bound link journey for
                         connecting a personal app account from chat.
  HUBZOID_CONNECT_TTL    Seconds a connection link stays valid. Default 600.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import config_secrets
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
    otel_normalize: bool = False
    openai_tracing: bool = False
    composio_api_key: str | None = None
    connections: tuple[str, ...] = ()
    # Native image vision: pass uploaded images to the model as content blocks.
    # On by default (HUBZOID_VISION=false to disable for a text-only/cost-
    # sensitive hub). max_edge caps the long edge before send; max_images caps
    # how many referenced images expand to blocks per turn (older -> text note).
    vision_enabled: bool = True
    vision_max_edge: int = 1568
    vision_max_images: int = 4
    # Shared browser (Playwright). Off by default. When on, the hub gets one
    # shared, resource-limited browser exposed to every agent as the full
    # Playwright MCP toolset — see hubzoid.browser and docs/BROWSER.md.
    browser_enabled: bool = False
    browser_port: int = 8931
    # If set, HubZoid does NOT spawn a sidecar; it just connects agents to this
    # already-running playwright-mcp endpoint (compose / production manages it).
    browser_mcp_url: str | None = None
    # browserless (or any CDP browser pool) endpoint. When set, the spawned
    # playwright-mcp attaches to it (pooled + hard concurrency/memory limits).
    # When unset, playwright-mcp launches its own shared browser (share-only).
    # Must be the ws form so the token survives: ws://host:3000?token=...
    browser_cdp_url: str | None = None
    browser_channel: str = "chromium"
    # Pinned playwright-mcp version for the spawned sidecar — reproducible builds
    # (a floating @latest can change the required browser under you). Override
    # with HUBZOID_BROWSER_MCP_VERSION; use "latest" to float deliberately.
    browser_mcp_version: str = "0.0.81"
    # Pool limits — consumed by the browserless container (see the shipped
    # docker/browser-compose.yml, which reads these via ${...}).
    browser_concurrent: int = 2
    browser_queued: int = 5
    browser_timeout: int = 60          # seconds; reaps stuck sessions
    browser_memory: str = "2g"         # hard container memory ceiling
    # Direct-mode safety net only (no browserless): restart the spawned browser
    # if its process-tree RSS exceeds this. 0 = watchdog off.
    browser_max_rss_mb: int = 0

    @property
    def first_api_key(self) -> str:
        return self.bridge_api_keys[0] if self.bridge_api_keys else "dev"


def load(hub_dir: Path, *, secrets: bool = True) -> Settings:
    """Apply the hub's configuration layers to the process env and bind a
    Settings object.

    `.env` is the operator's authoritative config and wins over shell env.
    Deployments that want shell-env precedence (systemd EnvironmentFile, k8s)
    simply don't ship a `.env` file.

    Secrets for access-controlled tools live in <hub>/restricted/.env, the
    restricted/ folder the file-reading tools refuse, so the model cannot read
    them. It loads after the main .env, into the process env, where only the
    restricted tools' own code reads them. See hubzoid.access.

    AWS secrets named by AWS_SECRET_NAME, HUBZOID_HUB_SECRET_NAME and
    HUBZOID_RESTRICTED_SECRET_NAME are fetched when `secrets` is true (see the
    precedence table above). Raises config_secrets.SecretFetchError when one
    cannot be read. `secrets=False` loads the files only, as the gateway does
    while planning, so it never holds a hub's secrets.
    """
    hub_dir = Path(hub_dir)
    config_secrets.apply_layers(hub_dir, secrets=secrets)

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
        otel_normalize=truthy(os.environ.get("HUBZOID_OTEL_NORMALIZE")),
        openai_tracing=truthy(os.environ.get("HUBZOID_OPENAI_TRACING")),
        composio_api_key=(os.environ.get("COMPOSIO_API_KEY") or "").strip() or None,
        connections=_conn_slugs(os.environ.get("CONNECTIONS")),
        vision_enabled=truthy(os.environ.get("HUBZOID_VISION", "true")),
        vision_max_edge=_int_env("HUBZOID_VISION_MAX_EDGE", 1568),
        vision_max_images=_int_env("HUBZOID_VISION_MAX_IMAGES", 4),
        browser_enabled=truthy(os.environ.get("HUBZOID_BROWSER")),
        browser_port=_int_env("HUBZOID_BROWSER_PORT", 8931),
        browser_mcp_url=(os.environ.get("HUBZOID_BROWSER_MCP_URL") or "").strip() or None,
        browser_cdp_url=(os.environ.get("HUBZOID_BROWSER_CDP_URL") or "").strip() or None,
        browser_channel=(os.environ.get("HUBZOID_BROWSER_CHANNEL") or "chromium").strip() or "chromium",
        browser_mcp_version=(os.environ.get("HUBZOID_BROWSER_MCP_VERSION") or "0.0.81").strip() or "0.0.81",
        browser_concurrent=_int_env("HUBZOID_BROWSER_CONCURRENT", 2),
        browser_queued=_int_env("HUBZOID_BROWSER_QUEUED", 5),
        browser_timeout=_int_env("HUBZOID_BROWSER_TIMEOUT", 60),
        browser_memory=(os.environ.get("HUBZOID_BROWSER_MEMORY") or "2g").strip() or "2g",
        browser_max_rss_mb=_int_env_zero_ok("HUBZOID_BROWSER_MAX_RSS_MB", 0),
    )


def layer_report(hub_dir: Path, *, fetch_secrets: bool = True) -> list[dict]:
    """[{"key", "layer", "source", "shadows"}] for every key a file or secret
    sets for this hub. Names and sources only, never values."""
    return config_secrets.layer_report(Path(hub_dir), fetch_secrets=fetch_secrets)


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


def _int_env_zero_ok(name: str, default: int) -> int:
    """Like _int_env but 0 is a valid value (e.g. a disable sentinel)."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        return default
    return n if n >= 0 else default
