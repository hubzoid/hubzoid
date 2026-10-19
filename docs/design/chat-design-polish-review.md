# Native chat design review — 19 October 2026

Reviewed and implemented on `hz2/chat-design-polish`, based on `hz2/chat-design`.
The review used an isolated deployment with five hubs: Operations, Engineering,
Customer Desk, Finance and People Operations. No new dependencies or backend
changes were needed. The standalone Console source and styling were left alone.

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
