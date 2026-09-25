# Administering a multi-hub deployment

Hubzoid has two audiences: builders configure agents, tools and workflows;
end users sign in and use the agents they have been granted. Open WebUI owns
accounts and authentication. Hubzoid owns agent/tool permissions and execution
inspection. The portal is at `/portal/`; administrators also see **Manage agent
access** in Open WebUI when using the Hubzoid edge.

## One deployment, several agents

```bash
export WEBUI_AUTH=true
export HUBZOID_GATEWAY_ADMIN_EMAIL=operator@example.com
export HUBZOID_GATEWAY_ADMIN_PASSWORD='your-service-account-password'
hubzoid gateway ./finance ./operations --data-dir ./gateway-data
```

The OWUI service account must be an OWUI administrator. Use the same credentials
in the gateway service environment for provisioning, account lookup and visibility
sync. No email/invitation is sent by a portal grant. OWUI signup/OIDC and account
approval continue to work as configured in OWUI.

The gateway writes `gateway-data/deployment.json` and a discovery pointer at
`<hub>/.hubzoid/deployment.json`. All registered hubs—including those with no
grants—appear in the portal. CLI commands find the same operational database
through these pointers. They do not require repeating the database URL.
The internal OWUI authentication URL is discovered from the same manifest.
Protect this file: a database URL can contain credentials (files are mode 0600).

**Hub folder names must be unique (case-insensitively) within one deployment.**
Each hub's directory name is its Casbin access domain, so two hubs that resolve
to the same name (for example `team.alpha` and `team-alpha`, or `Finance` and
`finance`) would share one access domain and could leak grants across agents. The
gateway refuses to start—and writes no manifest—when it detects a collision, and
`deployment.json` validation rejects duplicate, empty or reserved keys. Rename one
of the hub folders before starting the gateway.

For external bridges (`--no-bridges`), mount the same hub/config paths and shared
databases into the bridge processes. Set `HUBZOID_GATEWAY=1` to enable workflows.
Do not configure a different operational database in a hub's `.env`.

Default SQLite layout:

| Data | Owner | Location |
|---|---|---|
| Accounts, sessions, chats, model visibility | OWUI | `gateway-data/webui.db` |
| Grants, identities, change history, workflow state | Hubzoid | `gateway-data/hubzoid-operational.db` |
| Workflow execution history and checkpoints | DBOS | `<hub>/.hubzoid/dbos.db` per hub |
| Tool decisions | Hubzoid | `<hub>/logs/access-YYYY-MM.jsonl` |

This is one **deployment configuration**, not one physical SQLite file. Separate
DBOS files avoid sharing an unsupported multi-application SQLite execution store.
For PostgreSQL, set `DATABASE_URL` before starting the gateway; shared server/DB
storage is supported by the libraries. `HUBZOID_OPERATIONAL_DB` and
`HUBZOID_DBOS_DB` are explicit gateway-level overrides. Multiple hubs must not
share an explicitly configured SQLite DBOS file. A conflicting operational URL
in a registered bridge/CLI fails rather than silently writing another store.
Registered DBOS and internal OWUI URLs follow the same fail-on-conflict rule.
A copied hub pointer is rejected if that hub is not registered in its manifest.

Use one bridge process per hub with embedded SQLite. The two-process test proves
duplicate scheduled-slot suppression against an initialized database, not safe
concurrent first-time SQLite schema creation or production high availability.
Use PostgreSQL for multiple production workers and rehearse deployment upgrades.

## Fresh installation

First start the gateway to register its hubs. Then initialize a Hubzoid admin:

```bash
hubzoid access bootstrap --admin operator@example.com ./finance
# Fresh hubs only: make each hub authoritative explicitly.
hubzoid access bootstrap --authoritative ./finance
hubzoid access bootstrap --authoritative ./operations
```

Use migration below for existing hubs. An OWUI admin is not automatically a
Hubzoid admin. Sign in to OWUI using the bootstrapped email, then open `/portal/`.
The portal reuses and verifies that session; it does not accept a browser-supplied
identity header. Ordinary users cannot open its APIs.

## Grant access and verify the end-user experience

1. Open the agent and go to its **Access** tab. Use **Add person** and enter the
   person's signup email.
2. Tick the capabilities they need (a tool capability includes agent entry
   automatically), then **Review changes** and **Save**.
3. The permission check changes immediately. The chat app's model picker reflects
   the new access within about 30 seconds; the portal retries that projection on
   its own and only surfaces it if it keeps failing.
4. Ask the person to sign in with that email and open the agent. They should see
   only their permitted tools. Refresh the model picker if it was already open.
5. Review **Activity → Access changes** for who changed what and **Tool
   decisions** for actual allow/deny decisions. Both are scoped to the administrator.
   Filter by time range, agent, person, who-changed-it/action (access changes), or
   tool/outcome/channel (tool decisions); filters and paging are kept in the URL, so a
   filtered view is shareable and survives refresh and Back. **Details** on any row opens
   the full record — precise time (in your timezone), actor, affected identity, agent,
   capability/tool, channel and reason, with copyable identifiers. **People** has the
   same filtering by account status, role and agent.

The portal's **People** screen distinguishes accounts awaiting signup, pending
approval, active accounts, services, and blocked people. **Refresh accounts**
re-reads the account directory; it does not create accounts. Organization admins
add or remove other organization admins from a person's **Details**. Agent admins
cannot change admin rights or use an entry revocation to remove another admin's
rights.

Revoking agent entry also removes that person's direct tool grants in that agent.
Public access and organization-level admin rights are displayed as inherited
access. Removing a direct grant does not remove those inherited rights.

**Block access** removes a person's direct grants and blocks agent access,
including public access. It leaves the OWUI account and chats intact. Reactivation
does not restore removed grants. For full account offboarding, also suspend/delete
the account and revoke its sessions/API keys in OWUI. The final organization
administrator cannot be removed or blocked.

Workflow subjects are `workflow:<function_name>` and are granted in a named hub.
Permission definitions come from `restricted/*.py`; optional display metadata
lives in `identity/permissions.yaml`:

```yaml
ledger:
  label: Read ledger
  description: Read accounting entries for this agent.
  sensitive: true
```

Sensitivity is explicit metadata, not inferred from words such as `prod` in a name.
Permission keys `use_hub` and `manage_access` are reserved by Hubzoid.

## Migrate existing customers

Migration keeps the existing preview and atomic cutover. Start with a clone.
Keep the current gateway version/database backups until end-user checks pass.

1. Back up the deployment manifest, OWUI database, operational database and hub
   directories. Stop writes for a consistent SQLite file backup (or use SQLite's
   online backup API); use a database snapshot for PostgreSQL.
2. On the clone, export any function-backed access roster to an explicit CSV;
   remove the live access function before migrating. Computed permissions cannot
   be safely enumerated automatically.
3. Preview each hub with its specific OWUI model ID:

```bash
hubzoid access migrate ./finance \
  --from-owui sqlite:////absolute/gateway-data/webui.db --model-id finance
```

The model ID is the gateway's configured model label, not necessarily the folder
name. The OWUI 0.11 adapter reads `group_member` and `access_grant`; supported older
JSON schemas are recognized explicitly. Unknown schemas are refused. Import combines
CSV and OWUI tool groups, intersected with existing model visibility. It checks a
before/after permission matrix containing both permitted and denied users before
activation. Preview prints the number of legacy decisions checked and any
differences. A CSV-only preview warns that model visibility has not been verified, and
`--apply` refuses an unverified plan. For a legacy standalone hub where signed-in
entry was public, pass `--standalone-public` explicitly. It checks tool permissions
against the existing CSV resolver and automatically includes the local OWUI group
source when present; `--from-owui` can specify a different source copy. Registered
gateways require model ACL evidence and reject standalone bypass. Disabled models are refused rather than implicitly enabled.
It does not translate legacy groups into administrator privileges.

4. Freeze access edits, rerun the preview against the live source, then cut over:

```bash
hubzoid access migrate ./finance \
  --from-owui sqlite:////absolute/gateway-data/webui.db --model-id finance --apply
hubzoid access sync ./finance
hubzoid access diff ./finance \
  --from-owui sqlite:////absolute/gateway-data/webui.db --model-id finance
```

`--apply` automatically writes a mode-0600 pre-cutover access snapshot under
`<hub>/.hubzoid/backups/`. Grants and activation change in one transaction.
Test representative end users' agent entry and restricted tools immediately.
The static diff compares the imported plan with stored grants; it supplements,
not replaces, the effective access checks and real user checks.

5. If needed, restore the printed snapshot:

```bash
hubzoid access rollback /absolute/finance/.hubzoid/backups/access-....json ./finance
```

Rollback restores that hub's grants, attributes and previous authority state.
For an OWUI-based migration, the snapshot also retains the original model ACL.
Rollback to legacy restores it through OWUI's API, preserving current model
settings. Pause the visibility sync worker and access edits during rollback so an
in-flight projection cannot overwrite the restored ACL. If OWUI is unavailable,
the command reports partial restoration and exits unsuccessfully: keep maintenance
open, fix connectivity/credentials, then rerun the same rollback. Older or CSV-only
snapshots lack that ACL; the command explicitly requires restoring it from your
pre-cutover deployment backup. For a previously managed hub, run `access sync`.
Rollback does not undo account changes or replace a full deployment backup.

Migrate one hub at a time. Shared OWUI groups remain editable for unmigrated hubs
and non-agent resources. Migrated models' ACL writes are blocked at the edge and
redirect operators to the portal. Chat permissions always come from the authority
for the selected hub; OWUI visibility is only a mirror. OWUI's own administrators
may retain its privileged model visibility, but Hubzoid still checks agent entry.

## Planned maintenance upgrade (the whole deployment)

The upgrade is a short, explicit maintenance window run by an operator — there is no
automatic migration on startup. It reuses the commands above (`access migrate`,
`access sync`, `access diff`, `access rollback`) in this sequence. Do it once, per hub,
with all access writers stopped.

Prerequisites you must have to hand:
- The OWUI source database for each hub (SQLite file path, or a PostgreSQL URL if OWUI
  runs on Postgres) and each hub's **OWUI model ID** (the gateway's configured model
  label, not necessarily the folder name).
- The gateway admin email/password (`HUBZOID_GATEWAY_ADMIN_EMAIL` / `_PASSWORD`) so the
  visibility sync can sign in to OWUI.
- The email(s) of the existing OWUI administrator(s) who will own the dashboard.

Procedure:

1. **Stop the writers, but keep Open WebUI reachable.** Stop all bridges and the
   visibility projector (nothing should edit access or project during the window). The
   gateway normally supervises the OWUI subprocess, so stopping the gateway also stops
   OWUI — but `access sync` and rollback's visibility restore call OWUI's API. So run
   **OWUI privately** for the window: start it on its own, bound to loopback
   (`127.0.0.1`), with `OWUI_INTERNAL_URL`/`WEBUI_URL` and `HUBZOID_GATEWAY_ADMIN_EMAIL`
   / `_PASSWORD` set so the CLI can sign in — but with the bridges and projector down so
   no chat traffic or projection races the migration.
2. **Back up.** Copy the deployment manifest, the operational database, each OWUI
   database, and the hub directories to a protected location. For SQLite, copy the files
   while stopped (or use the SQLite online-backup API); for PostgreSQL, take a snapshot.
   (`access migrate --apply` also writes its own 0600 pre-cutover snapshot under
   `<hub>/.hubzoid/backups/`, but keep your full backup too — it is the verification and
   rollback baseline, and it captures the ORIGINAL OWUI model visibility before any sync.)
3. **Establish dashboard admins**, once, explicitly (audited):
   ```bash
   hubzoid access bootstrap --admin admin@example.org ./finance
   ```
4. **Preview, migrate, and verify BEFORE projecting**, per hub. Preview is a dry-run; it
   checks a before/after matrix of permitted AND denied users and refuses an unverified or
   empty plan. Run the confirming `diff` **immediately after `--apply`, before `sync`** —
   `diff` rebuilds its baseline by reading the OWUI source, and `sync` will rewrite that
   source's model visibility (replacing "public" with explicit per-user grants), so a
   `--from-owui` diff run *after* sync compares against a changed baseline and can report
   a false mismatch even though Hubzoid's permissions are unchanged.
   ```bash
   hubzoid access migrate ./finance \
     --from-owui sqlite:////absolute/gateway-data/webui.db --model-id finance           # preview (dry-run)
   hubzoid access migrate ./finance \
     --from-owui sqlite:////absolute/gateway-data/webui.db --model-id finance --apply    # cut over (+ 0600 backup)
   hubzoid access diff ./finance \
     --from-owui sqlite:////absolute/gateway-data/webui.db --model-id finance            # verify: expect 0 missing, 0 extra
   ```
   Re-running `--apply` on an already-migrated hub is refused (it would overwrite edits
   made since migration); `--remigrate` overrides that only if you intend to discard them.
5. **Project visibility, then verify the projection separately** (not with another
   `--from-owui` diff — see above). `sync` is idempotent, so a clean second run is the
   check that projection converged:
   ```bash
   hubzoid access sync ./finance   # projects Casbin → OWUI model visibility
   hubzoid access sync ./finance   # run again: it should report state "ok" and change nothing
   ```
6. **Restart** the gateway and bridges (which resumes the normal visibility loop).
7. **Smoke-test:** an admin opens the dashboard (the "Manage agent access" link in Open
   WebUI) and sees the migrated agent as editable; a permitted user can enter the agent
   and use its tools in chat; a denied user cannot; an ordinary user still uses chat
   normally; visibility sync shows `ok`.

**Rollback (if a hub fails verification):** with the bridges/projector still stopped and
OWUI still running privately (step 1), restore that hub's pre-cutover snapshot — this
restores grants, authority, AND the original OWUI model visibility saved in the backup.
Restart leaves the rollback intact (the hub is legacy again). If OWUI is unreachable the
command reports partial restoration and exits non-zero — keep the window open, fix
connectivity, rerun the same command.
```bash
hubzoid access rollback /absolute/finance/.hubzoid/backups/access-....json ./finance
```

**After upgrade — where things live:** migrated agents' permissions are managed in the
dashboard (the portal refuses and hides permission edits for a still-legacy agent, so
nothing there is misleading or silently overwritten); accounts, sign-in and roles stay
in Open WebUI. Migration flattens OWUI groups into direct grants, so if onboarding adds
users to an OWUI group to grant agent access, after migration those new members need an
explicit dashboard grant instead — confirm whether the deployment relies on group-based
onboarding before scheduling the window.

## Workflow execution and inspection

```bash
hubzoid new workflow daily-report ./finance
hubzoid doctor ./finance
hubzoid schedule run ./finance daily-report --dry-run
hubzoid schedule run ./finance daily-report
hubzoid schedule list ./finance
hubzoid schedule status ./finance
```

Definitions live in `workflows/<name>/*.py`, with literal schedule/timezone values
on `@workflow`. `doctor` checks syntax, duplicates and schedules without executing
workflow modules for its workflow inspection. Manual dry-run **does not import
or execute workflow code or launch DBOS**. Workflow runs reject markdown-only
`--timeout`, `--max-rounds` and `--model` overrides; configure execution in code.

```python
from hubzoid import workflow, step, hub

@step(max_attempts=3)
def collect():
    return "summary inputs"

@workflow("daily 06:00", timezone="Asia/Kolkata")
def daily_report():
    return hub.call_agent("Summarize: " + collect())
```

Agent calls use this hub's configured backend and its workflow service identity.
Grant that identity the tool permissions it needs. `hub.state` is persistent
per-hub/per-workflow state. Keep secrets inside steps, and do not return secrets
in step outputs: authorized administrators can inspect execution results.

An agent's **Runs & schedules** tab lists its workflows; **View runs** opens a
workflow's run list, and a run shows status, start/completion, duration, result,
error and its step timeline. Hub names distinguish identically named workflows.
Use the CLI for manual runs; portal workflow inspection is read-only. DBOS is the
source of execution history; Hubzoid does not maintain a second run database.

Gateway deployments enable workflows. Standalone `hubzoid run` requires
`HUBZOID_SCHEDULES=1`. The existing markdown scheduler retains its own enablement
and history. Both definitions appear in schedule list/status; the portal workflow
view is specifically for code-defined DBOS workflows.

The dispatcher wakes at minute boundaries; execution may start later because of
load or the per-hub concurrency-one queue. Scheduled invocations use deterministic
hub/workflow/slot IDs so duplicate dispatchers sharing the same DBOS database do
not execute a slot twice. Delayed ticks skip older slots and record the skipped
count. Downtime is not backfilled. Steps are at-least-once; external side effects
still need idempotency. Durability does not make external writes exactly-once.

**Failures and retries.** `hub.call_agent` and `hub.call_llm` run the hub's full
agent, tools included, so a failed call is **not retried**: a retry could repeat a
message or write the first attempt already made. The step raises, the run is
marked failed, and `on_failure` fires. A hub whose agent calls are safe to repeat
opts in with `agent_max_attempts: 3` in `workflows/settings.yaml`. Your own
`@step(max_attempts=N)` retries apply only to that step, so use them for steps
you have made idempotent.

**Restarts and code changes.** A run interrupted by a stop or crash resumes on the
next start: completed steps are not repeated, and the interrupted step runs again.
Runs are tied to the hub's workflow code (a hash of the Hubzoid version and
`workflows/**/*.py`; code a workflow imports from outside `workflows/` is not
covered). After you edit a workflow or upgrade Hubzoid, older interrupted or
queued runs are **not** resumed on the new code: they are cancelled at the next
start (status `CANCELLED` in the run list), so they can't block the hub's queue.
To change workflow code or upgrade Hubzoid safely:

1. Drain: wait until `hubzoid schedule status` lists no run as `PENDING` or
   `ENQUEUED`, ideally between scheduled slots.
2. Deploy the change and restart.
3. Check the run list. A run cancelled because of the change will not continue;
   start a fresh run with `hubzoid schedule run` if it is still needed.

## Troubleshooting

| Symptom | Check |
|---|---|
| Grant has no effect | Agent still marked legacy? Correct deployment manifest? Matching signup email? |
| Agent not visible | People screen sync status; service-account credentials; `access sync` |
| Portal denies entry | OWUI sign-in session; Hubzoid `manage_access`; discovered internal OWUI URL |
| Workflow absent/error | `doctor`, Workflow state/error, literal valid schedule/timezone, bridge logs |
| No execution history | Correct hub's DBOS database, workflow enabled, run actually submitted |
| User still has access after revocation | Public/inherited access; use Block access for offboarding |

For local UI development only, `HUBZOID_PORTAL_DEV=1` plus
`HUBZOID_PORTAL_DEV_USER=<bootstrapped-admin>` bypasses OWUI session validation.
Never enable this bypass on an exposed deployment.


## Account deletion, replacement and approval

OWUI remains the account owner. A successful directory refresh marks missing or
pending accounts unavailable; signup grants stay pending until the actual account
exists. The verified OWUI account ID is bound on migration, login and API-key use.
If a different account reuses an existing email, direct grants are removed and
agent access is blocked, with an audit event. An organization administrator must
review the account, reactivate it and grant access again. Public entry applies
after reactivation. This also protects chat and MCP entry before the next sync.
If the replaced account was the only administrator, use the documented local
`access bootstrap --admin <new-verified-email>` break-glass path, then verify the
new account. Do not reactivate an account solely because its email matches.

Gateway planning isolates each hub's dotenv settings; hub-specific secrets are
not inherited by other hubs, OWUI or the edge. Put deployment-wide OWUI service
credentials in the gateway process environment and the operator CLI environment.
Account refresh failures are visible and do not treat a partial/failed directory
response as a mass account deletion.
