# Bounded Gmail history for ready hubs

Implementation authorized by the user's 20 October 2026 request to build the
reviewed three-hub proposals and raise PRs. This dependency is the narrow SDK
gap named in Order Desk's approved inbox-first proposal. User authorization
supersedes the usual merged-proposal gate for this work. A person merges.

The existing Gmail channel starts from now. Reuse its OAuth transport,
operator CLI, connection state/lease and ordinary email admission to import
selected history. Add a local scope preview with exact digest confirmation,
inclusive/exclusive UTC dates, a declared message limit and one-page resumable
applies. Preserve sender admission, quarantine, source evidence, workflow
authority and new-mail cursor. Expired provider pages require explicit
same-scope restart and never trigger wider recovery.

Files: `channels/email/history.py`, email CLI/status, operator documentation,
and focused fake-provider regression tests. No new required hub/config fields,
model calls, runtime-specific code, alternate intake service, secret handling,
mail writes, or automatic approvals. Strict delegated setup credentials are
deferred platform hardening. The local operator owns the host and credentials.

Tests cover pure preview, changed scope/configuration, pagination, interruption,
overlapping new mail, idempotency, lease exclusion, deleted/out-of-period mail,
expired pages and honest limits. A broader email-channel regression runs once
after focused checks. Real Gmail/hosted qualification remains separate and is
not inferred from a fake-provider pass.
