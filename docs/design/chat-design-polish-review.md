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
