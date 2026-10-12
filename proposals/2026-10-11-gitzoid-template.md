# GitZoid template

## Problem
GitZoid (AI pull request review, security watch, weekly digest) runs today as
a WaveAssist DAG product. Teams that want it on their own infrastructure, with
their own model key and their own rules in plain files, cannot have it. It is
also Hubzoid's planned sample application: a builder should be able to start
from it with `hubzoid init --template gitzoid` and get a useful result on day
one.

## Why now
The 2026-10-07 product direction names GitZoid as the sample application. The
Hubzoid workflow changes planned on 2026-10-11 (workflows with inputs, Run for
me tools, personal schedules, report series and company-domain links, email
lists, GitHub webhook signatures, the private data folder) give a template
everything it needs without template-side workarounds.

## What should happen
A template at `hubzoid/templates/gitzoid/` that keeps every current GitZoid
behaviour and adds what the 11 October plan lists:

- Pull request review with a gate, a verify pass and a finding lifecycle
  across pushes; one summary and a few inline comments; a `gitzoid/review`
  status that can gate merges; fix prompts for the developer's own coding
  agent and a fix-loop skill. GitZoid never commits.
- `@gitzoid` commands kept to the sure ones: review, pause, resume, help,
  dismiss and defer.
- Team rules as files (`knowledge/review-rules/`, repo `.gitzoid/review.md`);
  drafts approved only by hub admins.
- Daily dependency security watch (OSV, CISA KEV, reachability, ledger,
  coverage alerts), weekly deep audit, repo profiles refreshed on change.
- Weekly digest per group and code, delivery and people reports as workflows
  with inputs: from chat, MCP, the Console or the CLI, on a person's schedule
  or monthly for the team.
- Chat and MCP tools as restricted capabilities (`gitzoid`,
  `gitzoid_manager`, `gitzoid_admin`).
- A bundled fictional sample that runs offline with no token.

The full design, UX and decisions: HubzoidAgent/docs/gitzoid-final-plan-2026-10-11.html
(and the 10 October plan for research and parity).

## Scope and non-goals
- Template only. Every Hubzoid change is its own proposal in the Hubzoid
  workflows-from-chat plan (proposals a, b, c); the template assumes they ship
  as agreed with that plan.
- No autofix commits, no Slack surface, no PDF attachments, no outside-domain
  email, no several schedules per workflow.

## Open questions
- Decisions listed in the plan's section 17 (merge gate default, commands
  default, people data scope, GitHub App versus token, default scanners,
  default trigger).
- Reader-only access for security list members without the agent (in the
  Hubzoid plan's proposal b).

## Definition of done
- `pytest tests/test_gitzoid_*.py` passes with no network and no model;
  `tests/e2e/test_gitzoid_e2e.py` finds the sample's planted bugs with a real
  model and stays quiet on the clean control.
- On Hubzoid 1.3.0: `hubzoid init --template gitzoid` then
  `hubzoid schedule run <hub> pr_sweep` reviews the sample; the release checks
  in the plan's section 14 pass, including a migration rehearsal on a real
  WaveAssist project.


## Review follow-up · 2026-10-12

The user has approved implementing the review of commit 436c76b on this branch.
This follow-up repairs the existing template and its onboarding, without changing
Hub contracts, runtime adapters, or DBOS.

Implementation sequence:
1. Review state and GitHub: separate publishing state, permission diagnostics,
   PAT commands, explicit resolution evidence, complete coverage and flexible backlog.
2. Security and context: normalize stored profiles, invalidate dependency assessments,
   resume audits across bounded batches without claiming partial scans are complete.
3. Reports: human-review metrics, immutable review events, source-linked code evidence,
   visible sampling/coverage, and mobile layouts.
4. Setup: runnable release-bound instructions, preflight, safe selected defaults,
   optional backlog/publishing/merge-gate choices, and a first useful result.
5. Validation: regressions for review findings; sample install, profile, review,
   commands, push, reports, security and resume flows; HTTP contract tests; broader
   Hubzoid regression and real-provider validation where test credentials permit.

Existing Hub installations retain their settings. New options are optional.
Publishing and email use explicit configuration; no production repository or
recipient is used as an implicit test target.

Actual CLI validation exposed a platform integration gap: before first web login,
the local workflow owner may publish but could not set a team artifact's audience
because no explicit membership row exists yet. The follow-up includes a narrow
artifact-owner fix for authentication-off, undeployed local Hubs, with tests that
revoke that exception when native sign-in is enabled or the owner is blocked.

Live report polling also exposed an invalid JSON body on HTTP 304 in the shared
artifact viewer. Return an empty response for unchanged reports, retaining the
ETag and access checks; extend the existing report-series regression.

### Validation completed · 2026-10-12

- Broader regression: **444 passed, 1 skipped**, using `-m ''` so slow/heavy
  workflow tests ran. Scope: init, packaging, `test_workflow*.py`, artifacts,
  artifact links/surfacing, chat artifacts, schedule grammar and GitZoid.
  The skip was `test_workflow_durable_execution`: the legacy external
  `HubzoidTestHub` fixture was absent at its hardcoded worktree-relative path.
  This is not counted as a pass. DBOS process/restart/recovery, identity,
  concurrency, webhook delivery and MCP workflow tests in that run passed.
- Subsequent focused checks after final changes: GitZoid workflows/regressions
  and init **86 passed**; GitZoid regressions/library **68 passed**; final setup
  regressions **17 passed**; artifact/report-series checks **50 passed**.
  These scopes overlap and should not be added into a unique test count.
- Real-model sample E2E: **1 passed** using Claude Sonnet. It detected the
  planted sample problems and stayed quiet on the clean control.
- Fresh isolated Hub created through the actual CLI: setup check, profiles,
  PR sweep, weekly digest, code report, delivery report and security watch
  succeeded through Hubzoid's workflow engine with the real model. This exposed
  and verified the local-owner artifact fix. Email remained disabled.
- Native Hubzoid chat retrieved saved PR findings, reviewed commit, preview
  status, coverage and fix prompts after an explicit GitZoid tool grant. The
  native report viewer opened the generated report. Browser observation exposed
  the unchanged-report polling bug; its fix passes the report-series regression.
- Live GitHub **read-only** acceptance against `encode/httpx`: setup check,
  repo profile, one current PR preview and a report succeeded. Remaining open
  PRs were visibly queued by the selected one-PR test budget. A historical
  report used actual merged-PR diffs and real-model code evidence. No GitHub
  write was permitted by the test transport.
- Report rendering checked at desktop and 390px mobile width: no document
  overflow; wide tables scroll within their own accessible panels.
- `git diff --check` passed. Temporary native test servers were stopped.

Validation boundaries: renewable GitHub App authentication, publication failure/
retry/idempotence, commands, finding resolution and audit resume have deterministic
regression/HTTP-contract coverage. A real GitHub App installation, production PR
publication, external GitHub webhook delivery, SMTP delivery, customer-hosted
provider deployment and migration from the older WaveAssist application were
not exercised. The entire repository test suite was not run; the broader scope
above was selected for this change. These results are not a claim of full
production or hosting certification.

Access remains explicit: the documented local quickstart grants only `gitzoid`;
rule approval, people-data access and personal scheduling use Hubzoid's existing
permission controls. Ordinary setup selects the workflow features without
silently granting those account capabilities.

### Pre-merge validation · 2026-10-12

The user requested a commit and merge. An expanded model-free run included all
slow/heavy tests (`-m 'not (e2e or e2e_llm or e2e_ui or e2e_browser)'`):
**3,859 passed, 33 skipped, 37 deselected, 2 environment failures** in 709 seconds.
Both failures were Watchtower subprocess imports: the isolated environment had
the dependencies but lacked an editable Hubzoid installation. After installing
this checkout with `uv pip install --no-deps --no-build-isolation -e .`, the
complete affected module passed (**3 passed**), with no product-code changes.
There are no outstanding failures from that run. No timeout was recorded as a
skip. The 33 skips require the external legacy TestHub, the audited Codex CLI,
or the optional Open WebUI extra; they are not claimed as passes. The 37 excluded
cases are the end-to-end marker groups above, with the separate GitZoid real-model
validation already recorded.

Wheel and source archive builds succeeded. Their contents include setup.py,
setup_check, GitHub code and sample fixtures, and exclude Python bytecode. A
wheel-installed copy initialized a fresh GitZoid Hub and successfully ran
setup_check through the workflow engine. The template secret scan reported no
leaks; `git diff --check` passed. The setup guide now states the checkout's actual
1.3.0 package version, distinguishing the development branch from the published
release. No release version was changed or published.
