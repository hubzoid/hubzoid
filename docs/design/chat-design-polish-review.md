# Native chat design review — 19 October 2026

Reviewed and implemented on `hz2/chat-design-polish`, based on `hz2/chat-design`.
The review used an isolated deployment with five hubs: Operations, Engineering,
Customer Desk, Finance and People Operations. No new dependencies or backend
changes were needed. The standalone Console's default presentation is preserved.

## Recommendations and resulting changes

| Finding | Recommendation and implementation |
| --- | --- |
| Personal settings occupied too many sidebar entries. | One **Preferences** entry, with Profile & security, Appearance, Notifications and Data & history tabs. **My connections** remains separate. Existing section URLs still work. |
| Profile inputs and orange actions dominated the page. | Limit form width, tighten the label/control grid, use quiet secondary actions, and stack fields on phones. |
| Chat defaults and Brand felt like unrelated admin pages. | Group both under **Workspace settings**. Flatten embedded cards into sections and align Chat defaults labels with their controls. Preserve review/apply/discard and technical metadata. |
| Activity & usage suggested a duplicate usage report. | Rename it **Activity**. Keep the cross-agent audit view: five-hub administrators need one view across the deployment. Agent Activity stays scoped to that agent; People manages accounts while agent Access manages permissions. No duplicate usage dashboard was added. |
| Embedded management text was unexpectedly tiny. | Stop applying Ant's compact typography algorithm on top of the native 13px scale. Keep compact control spacing and respect the person's text-size preference. Align wrapping search/filter toolbars. |
| Search and the composer gained square focus borders after visiting Settings. | Fix the cascade between lazy-loaded Console CSS and the native app. Use one rounded, visible focus indicator on each compound input. |
| Sign-in looked like a generic boxed form. | Use a restrained 360px form, clearer heading hierarchy, consistent provider/password actions and a neutral primary button. Retain the existing authentication behavior. |
| Phone controls and empty states were inconsistent. | Expand affected controls to 44px touch targets, contain wide tables, and give the Workspace file-selection empty state proper space. |

The agent picker remains useful context in a multi-hub deployment. Removing it
to avoid a repeated name in a single-hub screenshot would obscure which agent
will answer in the five-hub case.

## Verification

| Check | Result |
| --- | --- |
| TypeScript and production build (`npm run build`) | Passed. Vite reports its existing large-chunk warning. |
| Lint (`npm run lint`) | Passed with warnings; no errors. |
| Stream, router, image and runtime unit scripts | Passed. |
| New `npm run test:app:settings` | Passed: grouped navigation, search, profile save, preserved deep links, lazy Console focus interaction, phone layout, fixture OAuth cancel/success. |
| Five-hub layout matrix | 369 page states across owner, Ana and Ben; dark, light and black; 1470×860, 1024×768 and 390×844. No document-level horizontal overflow or uncaught page errors. These are layout checks, not exhaustive feature assertions in every combination. |
| Sign-in layouts | 27 combinations: password-only, Google-only and mixed, three themes and three sizes. No horizontal overflow. Phone dark-mode Axe checks found no serious/critical violations in these three modes. |
| Real local provider | Completed a tool call and answer. Quoted a reply. Queued a follow-up while running, interrupted the current reply, observed the interrupt instruction complete, then observed the earlier queued message run. |
| Chat chrome | Five-agent picker, add menu, bell, account menu and hidden-sidebar rail exercised. Model selection exercised using a synthetic model catalog. |
| Citations | Available-source presentation checked with synthetic passage content; unavailable-source state checked against a copied conversation. Both fit desktop and phone widths. |
| Configuration safeguards | Changed a draft timezone, opened the review diff, checked Apply availability, discarded the draft, and opened Technical details. No deployment configuration applied. |
| Edit/regenerate branches | Existing real-server journey passed: five checks including branch selection and reload persistence. |
| Console compatibility samples | Existing connector suite passed 22 checks, including Open WebUI mode. Personal reports suite passed. |
| Final bundle smoke | Desktop and phone checks repeated for the affected profile, Workspace and chat surfaces after the last build. |

The layout matrix includes new chat, all personal sections, agent Access,
Connectors, Runs & schedules, Evals and Activity, People, aggregate Activity,
Schedules, Chat defaults, Brand, Health & support, Sources, Spaces and Workspace.
Touch was enabled in the matrix; separate desktop-pointer screenshots were
also reviewed.

## Remaining verification gaps

- Real Google OAuth was not configured. Google-only/mixed layouts and the local
  OAuth fixture were checked; this does **not** establish a real Gmail login.
- The copied cited conversation correctly displayed “This source is no longer
  available.” A fresh retrieval-to-citation run against a real indexed source
  remains unverified.
- `test:app` stops when the existing fixture journey waits for **Stop response**
  after sending. Its preceding four unit scripts pass.
- The existing real-server tool journey fails its tool-state observer, which
  looks for the former visible monospace tool-name element. An independent
  stop journey times out waiting for the next fixture reply. These failures
  remain unresolved; a successful live interrupt is not a substitute for them.
- The standalone Console journey stops at its `.agent-card .identity` selector.
  The independent connector and reports suites pass, but the whole Console
  suite is not green.

This is a bounded visual/navigation change, not full release certification.
No API, data-model, runtime, authorization or provider configuration was changed.

## Annotated follow-up

The second review reproduced the constrained-width problems at 1172×740.
Document overflow checks alone had missed the cramped identities inside tables.

- **Agents:** use single-column directory rows with compact per-agent usage.
  Put deployment totals and their period filter behind **Usage overview**.
  On narrower layouts, omit per-row metrics; aggregate usage remains available.
- **People:** reserve space for the avatar and identity. Show the number of
  accessible agents as a link to the person's details, instead of stacking
  every agent/capability badge. Keep account state and Details visible.
- **Schedules:** clarify that this page lists recurring requests and reports
  created from chat. Authored workflows, their triggers and runs remain in
  each agent's **Runs & schedules** tab.
- **Brand:** explain the actual precedence. An activated, immutable revision
  determines the published brand. UI edits and CLI file imports both produce
  drafts requiring review and activation. Editing files alone does not replace
  a published revision; UI drafts are not automatically written back to the Hub.
  Before any activation, the legacy branding folder supplies the fallback.
  Recommend files for authoring and version control, with the UI for preview,
  review, activation and export. Removing UI editing would be a separate decision.
- **Health:** place Health & support under Workspace settings and omit its
  duplicate usage statistics in the embedded view. Keep checks and reports.
- **Sidebar:** increase the gap between the workspace identity and New chat.
- **Sign-in:** centre the form vertically, use a lighter centred heading,
  quiet filled fields and rounded actions, with Google below email/password.
  Reference: [Open WebUI's authentication page](https://github.com/open-webui/open-webui/blob/main/src/routes/auth/%2Bpage.svelte).
- **Populated connectors:** compact the embedded table so its switches and
  actions fit. Keep technical URLs/IDs in the editor. Flatten My connections
  and fix its status badges, which inherited an unrelated orange badge style.

Shared management screens now accept an optional embedded presentation. Their
standalone defaults are retained. No authentication or publication precedence
was changed.

### Review data

The isolated deployment contains four clearly labeled sample connectors:
GitHub (OAuth configuration only), Linear (disabled), Team knowledge (a working
local MCP service without sign-in), and Finance data (a fictional shared-key
configuration pointing to that local service). Owner and Ana can see them;
Ben retains his limited access. No customer accounts or credentials are used.
The local service exposes two harmless tools over fictional data. Discovery
and the Team knowledge connection were exercised successfully.

A localhost-only preview at port 4195 adds the Google provider to the session
response without changing production authentication code. Its Google action
opens an explanatory preview page, not a real OAuth flow. The five-hub review
deployment remains on port 4194.

### Recommendations awaiting product choice

**Themes:** offer Light and Dark, plus Use system. OLED black can be an optional
dark variant rather than a third equally prominent theme. Preserve existing
preferences and administrator restrictions during any future change. This
pass does not remove an appearance mode.

**Inbound MCP:** add a guided **Use in your assistant** panel, separate from
the outgoing app connectors in My connections. Select an agent, show whether
hosted MCP is enabled, expose its verified public connection address with Copy,
explain the assistant-side sign-in step, and link to existing connection/revoke
controls. Never imply that copying a URL establishes a connection. The current
native agents response does not provide hosted-MCP readiness or its canonical
public URL; those need a small, separately scoped read-only contract before a
live panel can accurately report availability. Reuse the existing OAuth flow.

### Follow-up verification

- Production build and lint pass (existing lint/chunk warnings remain).
- The expanded Settings test passes, including collapsed usage, the Health tab,
  and a geometry assertion preventing avatar/name overlap at 1172px.
- The 22 existing connector checks and the reports suite pass with standalone
  defaults. These do not resolve the broader failures recorded above.
- Populated desktop and phone screens have no document overflow; the reviewed
  desktop pages report no uncaught JavaScript errors. Table scrolling stays
  inside the table on phones. Screenshots wait for loading indicators to finish.
- The Google/password phone preview passes Axe serious/critical checks in
  Light, Dark and OLED after theme transitions settle. The temporary preview
  label's light-theme contrast was corrected. Real Google OAuth is still untested.

## Final review on the merged design branch

The five-hub preview now runs from `hz2/chat-design`. This pass checked the
builder, administrator and teammate journeys with fictional data, without
connecting customer systems or changing access in a real deployment.

### Fixes

- Access uses readable person/capability rows with visible Edit access actions,
  instead of a wide table that hid the actions at laptop widths.
- Workflow rows keep the schedule, execution account, state and actions
  together. On phones they stack without forcing horizontal scrolling.
- Embedded agent navigation uses ordinary links with a clear current state.
  This removes nested interactive controls and the inaccessible mobile overflow
  button from the embedded tabs. Standalone Console tabs remain unchanged.
- Breadcrumb contrast and inline link distinction are corrected. Wide eval
  tables have a named, keyboard-focusable scrolling region.
- Agent instructions identifies the hub supplying its files. Following another
  agent's Edit instructions link explicitly explains that the files shown are
  from a different hub. Authoring remains limited to the backend's current hub.
- Sources uses flat sections and complete collection names on phones. The
  sample handbook note was created, synced and found through source search.
- Embedded management dialogs now give phone buttons, inputs and checkbox rows
  44px minimum touch targets, including portals rendered outside Settings.

### Verification

- Merged baseline: 324 role/theme/viewport layout smoke states across owner,
  Ana and Ben, dark/light/OLED, desktop/tablet/phone. No document overflow or
  uncaught page errors. This is layout coverage, not 324 end-to-end workflows.
- Final changed screens: Access, Workflows, Evals, Agent instructions and
  populated Sources, at 1172px and 390px in Light and Dark. All 20 settled-page
  Axe WCAG A/AA checks passed, with no document overflow. The access drawer's
  phone button height was measured separately.
- TypeScript and production build pass. Lint has warnings and no errors.
- Expanded Settings regression passes: grouping, profile save, keyboard agent
  navigation, reachable phone access/workflow actions, focus after Console,
  and fixture Google sign-in. Standalone Console: **55 journeys passed**;
  connectors: **22 checks passed**; personal reports suite passed. This supersedes
  the earlier standalone journey failure recorded above.
- Live provider: a fresh Engineering chat called `whoami`, completed its reply,
  and rendered correctly after reopening. Five-agent switching, composer menu,
  notifications, account menu, collapsed sidebar and Chat defaults review/diff/
  discard were exercised. Earlier queue, interrupt and branch checks remain
  recorded above; they were not all repeated in this pass.
- Source creation, note entry, sync and search succeeded against the real local
  backend. Access editing and workflow history were inspected on phones without
  changing grants or starting real workflows.

### Limits and test setup

Real Google sign-in and customer connectors remain unverified. The Google page
is a labelled design preview; the working sample connector is local. The earlier
native-chat fixture/streaming test failures have not been resolved by these UI
changes. This is a design review, not full release certification.

The earlier disposable preview database contained the removed, unreleased
Spaces revision `op_0032`. A SQLite backup was made before aligning its revision
marker with `op_0031`; the now-unused Spaces tables were retained. No repository
migration or customer database was changed. This local fixture adjustment is
not an upgrade procedure for deployed databases.


## 2026-10-19: quieter activity and application copy

Source changes on main after fast-forwarding to 89e5a163. No push. Production bundles are intentionally unchanged while development continues.

- Tool activity shows the current action with a neutral text shimmer, followed by one collapsed call count. Consecutive calls group together without crossing intervening text. No visible Done badges. Errors and stopped calls remain labelled. Input and result are available on expansion. Approval and hub-change cards remain visible.
- Main actions use neutral filled buttons in native chat and embedded settings. Alternatives use outlines and row actions use ghost buttons. The standalone Console theme is unchanged.
- Composer focus uses a thin neutral outline. Copy is shorter across sharing, personal preferences, sign-in/security explanations, sources, model comparison, workflows, people, branding, chat defaults and diagnostics. The add-user outcome gives the identity and access once, followed by the next action.
- Sharing explanation has hover/focus help and a persistent tap disclosure. Access and copy-retention rules remain available. No user-authored messages or model output are rewritten.
- HubzoidStudio now has a Dashboard & chat reference, which is the application design baseline. Website references remain for marketing. Its content is synthetic.

### Product decisions to review

1. **Compatibility / parity ledger:** the page reads a deployment-owned parity.yaml checklist and records candidates, reviews and freezes. This is maintainer release/migration workflow, not ordinary team administration. Recommend moving it out of everyday settings into developer/release tooling. Labels were simplified, but the workflow and API remain intact pending a decision.
2. **Advanced settings registry:** raw domain ownership, configuration file names, CLI commands and revision metadata belong in technical support or builder tools. Keep necessary diagnostics accessible, but decide which registry controls are useful to a team owner.
3. **Brand and chat configuration:** draft, review and publish are meaningful safeguards. The UI still needs a clear file-versus-active provenance policy rather than implying a direct editor save changes live settings. No precedence or activation behavior changed here.
4. **Schedules vs workflows:** scheduled chat reports and file-authored workflows are different mechanisms. The current description explains where to find each, but naming and placement deserve a product decision before combining their controls.

### Validation and limits

TypeScript checks pass. Focused browser checks cover running/completed activity, 20-call grouping, text boundaries, results, failures/cancellation, approval visibility, reduced motion, 390px touch layout, sharing help and focus after embedded Console styles load. Tests use synthetic data and a graph-response adapter because the general chat fixture predates the current engine. Stream, image and runtime unit checks pass.

The older settings browser suite stops at its removed Profile & security label. The router unit suite expects a sign-in route without the new step field. These pre-existing expectations are not passing checks. No real-provider, OAuth or complete release validation is claimed. Use APP_DEV=1 node tests/app-tool-activity.cjs to verify source without rebuilding bundles.


## 2026-10-20: approved Studio controls in the app

The native app now uses the approved Studio control hierarchy: orange principal
actions, neutral filled chat actions, outlined alternatives and ghost row
actions. Hover, pressed and disabled states specify readable foregrounds.
Underline tabs retain transparent hover backgrounds. Shared Radix selects
replace native selects, with neutral highlighting and a selection checkmark.
Embedded management controls and their portalled dialogs use the same tokens.
Control heights, corner radii and phone targets are aligned with the reference.
Custom palette overrides and existing OLED appearance remain supported. No new
dependencies, API or permission changes are included.

### Validation for this PR

- TypeScript and lint completed; lint reports existing warnings, no errors.
- Focused theme/branding Python checks: 22 passed. Built-in primary, hover and
  pressed color contrast was checked in light, dark and OLED modes.
- Stream, router, image and runtime unit checks passed. The existing Settings
  browser suite passed in source mode, including profile save, mobile layout,
  focus after embedded management loads and fixture sign-in.
- The new five-hub browser audit passed its light, dark and OLED sections:
  action hover, transparent tab hover, agent list hover, management dropdowns
  and drawers, Radix selection, keyboard focus and popup Axe checks. Its custom
  token assertion then ran before the button color transition settled. The
  assertion now waits for the enabled control to settle; this final adjustment
  has not been rerun. Later tablet/member sections of this new audit were not
  reached. This is partial coverage, not a complete pass of the new audit.
- A broader backend run was stopped at the founder's request to free RAM. Its
  interrupted result was 369 passed and one failure: SCIM conformance could not
  bind a local port in the sandbox (PermissionError). The full run is not
  counted as passing. No additional heavy tests or build were run after
  that instruction. Generated bundles are unchanged.
- Real Google sign-in, customer connectors and production bundles were not
  validated in this pass. Source preview fixtures use fictional data only.
