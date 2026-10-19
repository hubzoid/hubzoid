# A data folder for hosted hubs, and deploy init and verify

Status: proposed for 2.0. Part of HUB-37. Founder decisions of 19 Oct 2026:
SQLite by default and PostgreSQL optional; `HUBZOID_DATA_DIR=/data`;
`hubzoid deploy init railway|maritime`; a docs page per provider;
`hubzoid deploy verify <url>`; the hub's own files live in its Git repository
and are baked into the image, and state goes to `/data`.

## Problem

- **A hub writes into its own folder.** Databases, keys, chats, artifacts,
  outputs, logs and inbound state all land under `<hub>/`. A provider gives
  one durable volume and an image that is replaced on every deploy, so today
  a hosted hub either loses its state on redeploy or needs its whole folder on
  the volume, where Git can no longer update it.
- **Nothing helps a builder deploy to a provider.** There is a Docker image
  and a Compose file, but no provider files, no provider steps and no way to
  check a deployment from outside.
- **The public health check says too much.** `/healthz` returns the hub,
  runtime and model names to anyone.

## What should happen

- **`HUBZOID_DATA_DIR`.** When set, everything a hub writes goes under it,
  with the same layout as today: `.hubzoid/` (databases, keys, chats,
  artifacts, evals, outbox), `output/`, `logs/` and `.inbound/`. The hub
  folder is only read. Unset, nothing changes.
  - One hub per data folder, run with `hubzoid run`. A gateway refuses
    `HUBZOID_DATA_DIR`: its edge, backups and other bridges find each hub's
    state in that hub's folder.
  - With it set, the hub's files come from the image: Settings shows agent
    instructions read-only, with a note to change them in Git and redeploy.
  - Doctor shows where state lives, and warns when state is split between the
    hub folder and the data folder.
- **`hubzoid deploy init railway|maritime [HUB]`.** Like Agno's per-provider
  starters and Mastra's deployers, one shared image and a few provider files:
  - `Dockerfile`: `FROM` the pinned release image, `COPY` the hub, and
    `HUBZOID_DATA_DIR=/data`.
  - `.dockerignore`, and `.env.example` with the variables to set.
  - `deploy/<provider>/README.md`: that provider's steps (volume at `/data`,
    variables, public URL, first owner, backups, costs and limits, verify).
  - It never overwrites a file without `--force`, and prints what it wrote.
  - Railway: no `railway.json` (Railway stops reading it on 1 Dec 2026).
    Maritime: never `maritime init`, which writes its own `AGENTS.md`.
- **`hubzoid deploy verify <url>`.** Like eve's doctor: one line per check,
  ✓, ! or ✗, exit 1 on a failure, `--json` for scripts. It checks HTTPS, that
  the health check answers and says nothing else, that app data needs sign-in,
  and that sign-in and MCP metadata name the same public origin.
- **Health.** The public `/healthz` returns `{"status": "ok"}` only. The bridge
  keeps its identity fields for the local start-up check.
- **Docs.** `docs/deploy/` with an overview and one page per provider, in the
  order the references share: before you begin, deploy, environment variables,
  persistence, verify, troubleshooting. Each provider page says it has not yet
  been verified on that provider until a live run on Hubzoid's own account.

## Scope and non-goals

- No provider accounts, templates, listings or spending. Live qualification
  waits for the founder's approval.
- No `deploy plan|apply|status`, `deployment.yaml`, provider adapters,
  hosting tables or a Console deployment screen (trimmed).
- No new required hub files or fields.

## Definition of done

- With `HUBZOID_DATA_DIR` set, a chat turn, an artifact, an eval run and a
  backup write nothing under the hub folder.
- `deploy init` writes the files for both providers, refuses to overwrite, and
  the generated image runs in local Docker with a volume at `/data`, keeping
  state across a restart.
- `deploy verify` passes against that container behind HTTPS and fails with a
  named check for plain HTTP, a missing sign-in or a leaking health check.
