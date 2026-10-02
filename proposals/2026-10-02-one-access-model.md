# One access model, per-agent connectors and eval scores (1.1)

## Problem
Hubzoid 1.1 still carries a second access model. A hub with no recorded access
mode uses "legacy" Open WebUI group access. In the web app that means anyone
signed in can use the hub, whatever their grants. `hubzoid init` avoids it with
a `.hubzoid/fresh-install` marker, but `.hubzoid/` is git-ignored, so a hub
deployed from git falls back to the open legacy mode. An upgraded hub in local
mode also shows "No agents yet" until its owner opens the Console once, because
the owner is provisioned only there.

The Console has sections the product does not need: Groups, and a separate
Connectors section for personal connections that belong to agents. Open WebUI is
called "legacy mode" although it is a supported chat UI. Evals show results but
no score at a glance. Webhook deliveries are visible only from the command
line. Pull request CI still runs every slow test.

## Why now
The founder confirmed on 2 October 2026 that every deployment has moved to
Console-managed access and that every new hub starts there. Nothing needs the
legacy model any more. 1.1 is unreleased, so removing it now costs no migration
for released users. Groups were added in 1.1 and were never released.

## What should happen
1. **One access model.** Every hub is Console-managed: access is decided by
   grants, always. The authority marker, `hubzoid access migrate`, `rollback`
   and `diff`, the init marker and the legacy group gates go. The checks around
   them stay: a store error denies (503), a suspended person is refused, the
   identity must be verified, and restricted surfaces keep their limits. Open
   WebUI session checks and trusted bridge headers are Open WebUI mode, not
   legacy, and stay.
   The owner is provisioned where it is today, at a verified sign-in of the
   configured owner as an administrator (web app, Open WebUI session, MCP
   sign-in), and additionally for the implicit local owner when sign-in is off.
   An API key never provisions anyone. The Open WebUI visibility mirror, the
   agent picker and the edge's model-access lock cover every hub.
   `hubzoid doctor` reports a hub that nobody may use and a hub only its owner
   may use.
2. **No Groups.** A migration turns each group grant into the same grant for
   each member, and each artifact shared with a group into a share with each
   member, then drops the group tables. Then the Console Groups screen, group
   pickers and group resolution go. `hubzoid migrate openwebui` turns Open
   WebUI group access into per-person grants.
3. **Connectors inside each agent (web app).** The Console's Connectors section
   becomes an agent tab. A connector is still registered once (one OAuth client,
   one connection per person). A new relation records which agents offer it.
   The migration offers every existing connector in every agent, so nothing
   changes on upgrade. In an agent, an organisation administrator adds a
   connector (registers it and offers it here), offers an existing one, stops
   offering it here (only this agent's offer and grants go), edits it, or
   deletes it everywhere. A person's connection is used in a turn only when the
   agent offers the connector and the person holds `connector_<id>` on that
   agent; the current-agent check stays. The connections page lists what the
   person may use in at least one agent that offers it.
4. **Open WebUI mode keeps its connectors in 1.1.** Isha's ERP and Nurturing
   hubs register their Odoo servers in Open WebUI (`OWUI_NATIVE_MCP=true`) and
   their skills call `mcp__owui_odoo_<erp>__...`. Replacing that path needs an
   Open WebUI identity adapter, connection routes under `/portal`, a one-time
   import of the registered servers, stable tool names and a reconnect for
   everyone. That is its own change, after 1.1. In 1.1 the agent's Connectors
   tab lists the servers registered in Open WebUI with their permission,
   read-only, and says where to change them.
5. **Wording.** "Open WebUI mode" everywhere. "Legacy" stays only for old data
   formats and the legacy service identity of pre-account workflows.
6. **Eval scores.** The Evals tab opens with the latest pass rate (passed of
   total), the trend over the last 10 runs, cases failing now, cases never run,
   and the last run's time and trigger. Each case shows its last 10 results as
   dots. Each agent card in the Console shows "Evals 8/10" or "Evals not run",
   linking to the tab. All from the existing eval index. Case names and
   pass/fail results are operational data, as the Evals tab already shows them
   for every case. Prompts, checks and answers of an account's private cases
   stay private.
7. **Webhooks in Runs & schedules.** A webhook workflow shows its URL,
   verification type, 24-hour counts (accepted, running including retrying,
   succeeded, failed) and its latest failed events with their ids and the
   redrive command. The endpoint checks agent access first and reads named
   columns only, never payloads, digests or headers. Code workflows declare
   `on_webhook` in their decorator, which the static reader learns.
8. **Small fixes.** The Console agent card uses the agent's display name.
9. **CI.** Pull requests run the fast default (`pytest`). Pushes to `main` run
   everything except e2e, slow tests included, as a separate step. Releases
   keep `pytest -m ""`.
10. **A latest link for published boards.** `hub.publish_artifact(..., key=...)`
    tags a publish. Every publish is still a new, permanent page.
    `/portal/latest/<hub>/<key>` sends a signed-in viewer to the newest page with
    that key, if they may see it. Otherwise it answers the usual "not available",
    never an older copy. Signed-out viewers go to sign-in first. A live board
    republished every two hours then has one bookmark.
11. **An agent picker that scales.** The new-chat screen shows two large cards
    per row, which does not work for 10 to 20 agents. Replace it with a compact
    list with search, the person's recent agents first.

## Order
CI, latest link (Isha's AdBrain is waiting for it), one access model, Groups,
connectors, eval scores, webhooks, the small UI items, docs. Focused tests per
step, then the full suite and both journeys once.

## Scope and non-goals
Not included: workflow diagrams, numeric eval scores, eval datasets or
experiments, a Console eval run button, approvals, event retention, moving Open
WebUI mode to Hubzoid connectors (item 4), and deleting older pages of a latest
key (keep the last N), which would remove permanent pages.

## Review
Codex reviewed the first draft on 2 October 2026. Accepted: keep the
fail-closed checks and Open WebUI session handling (item 1), provisioning only
at verified sign-ins (item 1), expand group grants and shares instead of
deleting them (item 2), the connector-to-agent relation and the detach versus
delete split (item 3), the eval privacy statement (item 6), the webhook
projection and code-workflow declaration (item 7), and two CI steps (item 9).
Its Open WebUI findings (no identity binding, routes outside `/portal`, pinned
Open WebUI is 0.11.4, flags are only defaults) plus Isha's use of native
connectors moved item 4 out of 1.1. Not accepted: a stranded owner marker after
a rollback, because rollback is removed and doctor reports a hub nobody may use.

## Definition of done
- A hub deployed without `.hubzoid/` denies a signed-in person without a grant,
  in the web app and in Open WebUI mode. The owner reaches it on first sign-in,
  and in local mode on first use.
- No Groups or Connectors item in the Console menu. An agent's Connectors tab
  offers a connector that a granted person then uses in that agent only.
- Group grants made before the upgrade still give each member the same access.
- The Evals tab and the agent cards show pass rates from real runs.
- A webhook workflow's Runs & schedules view shows its deliveries and failures.
- The full suite passes once (`pytest -m ""`), and the Console and chat
  journeys pass. Docs, CHANGELOG and release notes describe the result.
