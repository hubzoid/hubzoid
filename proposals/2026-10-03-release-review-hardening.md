# Release review hardening and concise operator UX

Status: authorized for implementation and commit by the owner on 3 October
2026, after reviewing the production-readiness report for candidate 6a5d165.
The owner requested focused validation, not another full suite run.

## Need and scope

The human review reproduced attachment collisions, rehearsal writes through
symlinks, audience widening of imported restricted shares, and false gateway
readiness/success after a child failed. These must be fixed before release.
The normal chat, multi-hub, workflow and backup journeys worked, with bounded
archive, share-route, startup and documentation fixes already prepared.

1. Verify attachment content before reuse. Allocate stable collision names,
   preserve files and support idempotent imports.
2. Materialize rehearsal symlinks into the scratch copy and enforce its write
   boundary. Preserve consistent SQLite copies. Reject unsafe destinations.
3. Keep migrated owner-only/restricted share links closed until the owner
   explicitly republishes. Preserve metadata and public links. No new share
   ACL system or database schema is needed.
4. Validate ports and owned process readiness in both gateway modes. Verify
   the hub identity of independently managed bridges. Fail startup or a
   supervised child exit with a nonzero status and clean up child services.
5. Render workflow results as structured data and artifact links. Reuse
   existing CLI controls through copyable commands. Hide secondary explanations
   behind accessible question-mark help and raw identifiers behind details.
   Failures and privacy/consent boundaries stay visible.
6. Move SDK object construction out of neutral loaders and eval logic into
   existing runtime factories. Preserve loader outputs and injected judges.
7. Update the canonical project rules, operating/upgrade docs and release
   record. Keep SQLite the single-host default with separate per-hub workflow
   databases, explicit recovery checks and no multi-host/HA claim.
8. Reduce ordinary framework startup chatter and register readable standalone
   Open WebUI model names without changing model ids or access grants.

## Build order and validation

Migration fixes first, then process lifecycle and runtime adapters, then UI and
docs. Extend existing regression fixtures, remove resolved strict xfails, run
the affected Python checks including real Open WebUI and PostgreSQL fixtures,
build the frontend and run its pure data checks. Recheck the local browser
journeys and installed package behavior. Commit the reviewed source and bundled
frontend together. No PR, push or production deployment is requested.

## Decisions and limits

The only current customer already uses Console-managed access. Do not retain
or restore a second access authority. Open WebUI mode and its native connector
tool names remain supported. The first customer upgrade can keep that UI,
avoiding unnecessary history/token migration. Native UI migration retains its
documented subset and requires per-person connector reconnection.

No new execution engine, workflow canvas, feature gate, auth framework, or
memory system. No customer endpoints or credentials are exercised. Real SSO,
customer integrations, a human new-password flow, and actual device acceptance
cannot be certified by model-free fixtures. Record those limits honestly.


## Implemented and rechecked

The fixes above are implemented without a new database schema or required hub
field. Structured outputs preserve the existing privacy checks. Artifact
actions use only the local canonical viewer route. Existing gateway bridges
remain independently managed with `--no-bridges`.

Focused validation on the patched candidate:

| Check | Result |
|---|---|
| Migration and share privacy, with installed Open WebUI and PostgreSQL fixtures | 37 passed; resolved strict xfails removed |
| CLI, gateway, factories, server and workflow presentation | 277 passed |
| SQLite crash recovery, backup/restore and actual occupied ports | 31 passed |
| Neutral loaders, cross-hub runs and per-person MCP parity across runtimes | 76 passed; 2 real Codex CLI checks skipped |
| Judge adapter, model completion and failure handling | 51 passed |
| Frontend | TypeScript/Vite build and pure stream/router/image/result checks passed; lint has no errors |
| Live gateway failure | Two SQLite hubs started; stopping one bridge caused exit 1 and released all three owned ports |
| Browser | Eight-hub demo; compact workflow list and result; report rendered; question-mark help and command-copy worked; archiving the active synthetic conversation reset to an empty chat |

Counts overlap where existing test modules support more than one change. The
full suite was not repeated, as requested. The browser viewport override did
not take effect (it remained 1280 pixels), so no real-phone claim is made.
Customer SSO/integrations, real Codex acceptance and a human new-password
journey remain deployment acceptance checks, not completed by these fixtures.
