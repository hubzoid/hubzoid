# R2 inventory: what R1 left out or found

Status: draft inventory for R2 planning. It is not a plan: no order, dates or
sizes. Baseline is release 1.0.1 on `auth-schedules-upgrade` (`b567b56`),
compared with the R1 contract in
[r1-workflows-console-release.md](r1-workflows-console-release.md).

Each item says what is open and why, then where the evidence is. Items marked
"by decision" were deliberate R1 non-goals. They stay out unless a need is
observed. No TODO or FIXME comments were added on this branch
(`git diff 2a0a396..HEAD`).

## Dependencies
- **Open WebUI pins libraries with known advisories.** Open WebUI 0.11.4 pins
  pypdf, Pillow, aiohttp, cryptography and mcp at versions with published
  advisories. The owner decided not to override them and to take fixes when
  Open WebUI releases them, so each Open WebUI bump is the point to re-check.
  The same pins hold openai-agents below 0.18, litellm below 1.96 and composio
  below 0.16. Evidence: `requirements.lock` (open-webui 0.11.4, pypdf 6.7.5,
  pillow 12.2.0, aiohttp 3.13.5, cryptography 48.0.0, mcp 1.27.2),
  `pyproject.toml:45-62`.
- **DBOS needs SQLite 3.42 on Python 3.12.** DBOS 3 stamps rows with
  `unixepoch('subsec')`, which older SQLite returns as NULL, so the engine fails
  to start. Hubzoid now refuses with a clear message, doctor reports it and the
  image moved to Debian 13. The root cause is upstream and could be reported to
  DBOS. Evidence: `hubzoid/workflows/runtime.py:105-127` (`sqlite_problem`),
  `hubzoid/doctor.py:108-122` (`deps.sqlite`), `tests/test_doctor.py`,
  `Dockerfile:34-37`.

## Access
- **Casbin package line.** `casbin` 1.43.0 (May 2025) is the last release under
  that name. The maintained `pycasbin` 2.8.0 has the same import name, all 111
  access tests pass on it, and neither has an OSV advisory. Deferred because an
  in-place pip upgrade leaves two distributions owning the `casbin/` files. It
  needs a clean-environment upgrade path (fresh virtualenv or image rebuild) and
  an upgrade note. Evidence: `pyproject.toml:71`, `requirements.lock:120`,
  `hubzoid/access/store.py`, `tests/test_access*.py`.

## Data lifecycle
- **Usage and decision rows are never pruned.** `hz_usage` gets a row per chat
  turn and workflow model call. `hz_access_decisions` gets a row per tool
  decision. Neither has a retention setting. The Console Overview reads at most
  30 days, while `hubzoid audit` reads decisions over any time range, so the two
  tables may need different retention. Both are indexed on `ts`. Evidence: `hubzoid/migrations/operational/versions/0002_usage.py`,
  `0003_access_decisions.py`, `hubzoid/usage.py:57-123`,
  `hubzoid/access/audit.py`, R1 proposal line 115.
- **Inbound dedup markers are never pruned.** WhatsApp, Telegram and webhook
  deliveries each leave a marker file under `.inbound/dedup`. R1 webhooks add
  one per delivery id, kept indefinitely, or one per body hash and 10 minute
  bucket. Evidence: `hubzoid/inbound/dedup.py:9-10`,
  `hubzoid/inbound/webhook.py:120-143`, `docs/inbound-surfaces.md:109`.

## Workflows and schedules
- **The `HUBZOID_SCHEDULES` gate.** On a standalone `hubzoid run`, code
  workflows schedule only with `HUBZOID_SCHEDULES=1` (on by default under
  `hubzoid gateway`). Markdown tasks need no flag. The gate was laptop safety.
  Whether it should remain now that both kinds share one engine is open.
  Evidence: `hubzoid/workflows/boot.py:1-8` and `:33-38`, `docs/workflows.md:12`,
  `docs/ADMINISTRATION.md:321-323`, `tests/test_markdown_on_dbos.py:213`.
- **Webhook repeats without a delivery id.** With no delivery id header, an
  identical body counts as a repeat only within its 600 second bucket and the
  one before, so for at least 10 and under 20 minutes. A provider retry after
  that is stored twice, and a genuine identical event inside it is dropped.
  Evidence: `hubzoid/inbound/webhook.py:120-143` (`DUPLICATE_WINDOW_SECONDS`),
  `tests/test_inbound_webhook.py::test_identical_body_without_an_id_is_a_repeat_only_briefly`.
- **Version hash scope.** Code imported by a workflow from outside `workflows/`
  is not in the version hash, so an interrupted run can resume on changed code
  after such an edit. This is documented, not guarded. Evidence:
  `hubzoid/workflows/runtime.py:73-102`, `docs/workflows.md:119-121`.
- **`hub.decide` stays experimental.** It calls Jev through OpenRouter's
  decisions endpoint, which is alpha, and is pinned to one tested request shape.
  Evidence: `hubzoid/workflows/context.py:186`, R1 proposal line 113.
- **Exactly-once external effects (by decision).** Runs are at least once.
  Idempotency is documented guidance, not a helper. Evidence: R1 proposal lines
  65 and 109-110, `hubzoid/workflows/state.py:14`,
  `hubzoid/workflows/runtime.py:251`.

## Deployment
- **SQLite to PostgreSQL is manual.** No command moves an existing SQLite
  deployment's data (Hubzoid `hz_*` tables, DBOS runs, Open WebUI accounts and
  chats) to PostgreSQL. The docs say to start fresh or copy the data by hand.
  Evidence: `docs/DEPLOYING.md:55-67`.
- **`claude-local` does not work in the Docker image.** The image has no
  `claude` CLI, so chat in a container needs a provider API key and
  subscription-billed inference is host-only. Evidence: `Dockerfile:29-30`,
  `docs/DEPLOYING.md:225` and `:563-564`.

## Console and frontends
- **No run controls in the Console (by decision).** Run, pause, resume and
  cancel stay in the CLI with server authority and are audited. The Console
  shows paused work only. Evidence: R1 proposal lines 86-88 and 108,
  `docs/workflows.md:133-143`.
- **Per-message content analytics and rich replay (by decision).** `hz_usage`
  holds no message content, so either would need new data and a privacy
  decision. Evidence: R1 proposal lines 68-70 and 108, `hubzoid/usage.py`.
- **Frontends beside Open WebUI.** AGENTS.md and the product brief leave the
  frontend choice open. The R1 proposal names LibreChat and Assistant UI and
  marks a new identity or chat UI as R2. Console numbers already come only
  from Hubzoid's tables. Identity and access still read Open WebUI through
  seven `hubzoid/access/owui*.py` modules, and the Open WebUI pin notes
  coupling to its internals for MCP OAuth and branding. Evidence:
  `docs/PRODUCT_CONTEXT.md:69`, R1 proposal lines 72 and 108-109,
  `tests/test_console_home.py`, `pyproject.toml:51`.
