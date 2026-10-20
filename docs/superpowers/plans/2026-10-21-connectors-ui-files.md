# Connector UI with YAML persistence

> Implement inline with the executing-plans skill; the user explicitly asked
> for implementation of UI and file setup.

**Goal:** Restore connector management in Settings with YAML as its only definition.

**Architecture:** Reuse the previous React connector editor and existing authorized
routes. Put validated, locked file writes in a connector-specific module. Registry
reads and writes use one parser; secrets entered in the UI are env references.

**Tech Stack:** Existing Python/FastAPI/PyYAML/dotenv and React/Ant Design.

**Spec:** `proposals/2026-10-21-connectors-ui-files.md`

## Constraints and review focus

Preserve runtime neutrality and grants. No new dependencies. Do not modify real
connector configuration. Verify duplicate IDs, stale edits, malformed files,
symlinks, secret redaction/rotation and changed-server token cleanup.

## Tasks

- [x] Add API regression tests for create/offer persistence, reload after manual
  edits, secrets, invalid/duplicate requests, access and stale revisions. Run red.
- [x] Implement file writes and registry integration, revision metadata and
  atomic create-with-hub. Run connector and file-setting regressions.
- [x] Restore the existing connector UI, add file persistence copy and revision
  checks. Build, lint, and verify the browser flow with synthetic connector data.
- [x] Update upgrade/MCP docs, review the full diff, run broader tests and rebuild
  the packaged UI. Verify the local server serves the tested UI bundle without creating real connections.

## Verification, 21 October 2026

- Final connector, OAuth, token, per-runtime, network and upgrade regression
  selection: 184 passed (8 existing MCP deprecation warnings).
- Restored Settings browser journey: 22 checks passed, including stale edits,
  permission boundaries, shared keys and mobile width.
- Build and lint succeeded. Repository lint reports existing warnings; focused
  lint of the changed TypeScript and browser test files reports none.
- Fresh review found dotenv interpolation could alter a UI-entered secret after
  restart, and a numeric name could raise a 500. Both were reproduced with failing
  tests and fixed. Generated env references use their literal file value; normal
  hand-authored references retain operator environment precedence.
- Added stale-delete and changed-sign-in cleanup coverage. File-write tests: 23 passed.
- Optional full repository pytest run was interrupted before completion, around
  one fifth through; it is not counted as a pass. Interrupting also produced a
  pytest fixture-teardown KeyError. Full-release qualification remains unclaimed.
- Graceful restart returned ready for customer-desk, order-desk, software-factory
  and edge. HTTP on port 3600 returned the exact tested AgentDetail bundle with
  Add connector and YAML-save guidance. No real connector was created.
- Initial implementation was verified in the ready-hubs-review checkout. The
  connector PR is prepared separately from current main, excluding the unrelated
  UI commits in that local deployment. Production deployment and merge are outside
  this change.
