# Optional local Codex backend

Status: implementation authorized by the owner on 2026-09-25.
Includes first-run selection, dark-theme refinement and Apache-2.0 migration.
Use the official app-server protocol for guarded dynamic tools, rather than
parsing CLI display output. Keep per-request threads ephemeral and disable native
environment access; fail closed on unsupported protocol versions.

## Implemented design

`MODEL=codex-local` is implemented through app-server 0.147.0.
The non-interactive command is `codex exec`; `-p` chooses a configuration profile.
Connecting Codex to a Hubzoid MCP server already works at the protocol level and
does not require this new backend or a custom plugin.

During first-time interactive setup, detect available CLIs and authentication,
show the usable options, and save the chosen backend in the hub configuration.
Do not replace an existing MODEL or silently switch an established deployment.
If several backends are available, ask once. A CLI binary alone is insufficient:
authentication, usage availability and the intended operator account matter.
Non-interactive/container startup should require explicit configuration rather
than selecting a personal CLI account from whichever binary is first on PATH.

## Validation contract

- Same hub-defined tool names, schemas, outputs, skills and knowledge as both
  existing backends; maintain permission checks for each caller.
- Disable or isolate native shell/file tools so they cannot bypass Hubzoid's
  .env/database restrictions or access another user's credentials.
- Correct streaming, cancellation, context/session isolation, tool errors,
  structured workflow calls and subprocess cleanup.
- Authentication failures and exhausted usage produce actionable errors without
  switching accounts or providers silently.
- Keep the adapter in the existing runtime construction boundary. Prefer the
  supported Codex SDK/CLI integration over a bespoke process protocol.

Hubzoid currently integrates local Claude through the Claude Agent SDK, not a
simple shell wrapper. A Codex backend therefore needs equivalent runtime and
security behavior; merely invoking an installed CLI does not meet the contract.

References: [MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
[non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode).

## Verification

The model-free adapter tests cover protocol errors, identity and permission
checks, usage, structured workflow output, auth refresh and cancellation cleanup.
A real pinned CLI with a synthetic Responses endpoint verifies direct and
code-mode tool dispatch, no host module imports or native I/O globals, and only
registered Hubzoid tools. An authenticated smoke run reads synthetic knowledge
and verifies `.env` remains inaccessible. The protocol pin is intentional;
upgrading Codex requires rerunning these checks and reviewing tool exposure.
