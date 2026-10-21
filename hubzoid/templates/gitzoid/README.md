# GitZoid

A quiet engineering teammate on your own Hubzoid. It reviews every pull
request against your team's rules, remembers its findings across pushes,
answers `@gitzoid` in GitHub, hands fixes to your own coding agent, watches
dependencies and code for security problems, and writes a weekly digest and
code, delivery and people reports.

## Install and try the sample

GitZoid is included in Hubzoid since **1.3.1**. Use Python 3.11 or 3.12 in a virtual
environment. This checkout targets 2.0.1. Until it is published, install this checkout with `pip install -e .`
instead of the versioned install below.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install 'hubzoid==2.0.1'
hubzoid init my-gitzoid --template gitzoid
hubzoid grant admin@localhost gitzoid my-gitzoid
hubzoid schedule run my-gitzoid setup_check
hubzoid schedule run my-gitzoid repo_profiles
hubzoid schedule run my-gitzoid pr_sweep
hubzoid run my-gitzoid
```

The explicit `grant` command enables GitZoid chat tools for your local owner;
it does not grant rule approval or people-data access. In a signed-in deployment,
grant your own email instead. Permissions can also be chosen in Console → Access.

`init` offers a signed-in Claude Code or Codex runtime, or a hosted model key.
Use `--model` to choose explicitly. The fictional GitHub and advisory data need
no GitHub token or external feeds; reviews and summaries still use your selected
model, require its authentication/network access and consume its quota. Optional
Ruff and Gitleaks presets report when unavailable. They use isolated configuration
and scan changed files as data, including dotfiles; PR config, ignore lists and
Gitleaks suppression comments cannot turn them off. Custom analyzer commands
require `allow_project_code: true` on that analyzer in Hub settings. This is an
explicit operator opt-in: custom tools can load executable project configuration
and are not sandboxed. Use only trusted commands in an appropriately isolated
runner; they are skipped by default.

You first see a setup result with access, mode, eligible PR count and next steps.
The profile describes the repo. The review run shows each PR's outcome, full
preview payload, skipped reasons and queued work. In the running Hub, open
**Console → Workflows** to inspect runs and reports, or ask in chat:
“Show GitZoid status” and “Show the findings on storefront PR 102.”
The Console and chat URLs are printed by `hubzoid run`.

The sample has five eligible PRs: an endless retry (#101), an unprotected admin
export (#102), unsafe SQL (#103), a clean change (#104), and a vulnerable
package bump (#105). Draft #106 is skipped by default. Nothing is posted to
GitHub. Security checks, an audit and reports can be run from Console or with
`hubzoid schedule run my-gitzoid <workflow>`; email lists start empty.

Personalize one rule: ask “Draft a rule: all supplier retries must stop after
three attempts.” Rule approval is a separate choice: grant yourself `gitzoid_admin` in Console → Access.
Then, as a Hub admin, ask “Show pending rules” and approve that
specific draft. Run the sweep again. Only the explicitly approved rule becomes
shared Hub knowledge; setup conversation is not automatically saved.

## Connect GitHub quickly

Create a fine-grained personal access token for selected repos. Permissions:
**Metadata, Contents, Pull requests, Issues, Commit statuses and Checks: read**.
For publishing, also allow **Pull requests and Issues: write**, and
**Commit statuses: write**. If the organization requires approval or SSO,
complete that in GitHub before the access check.

From the directory containing your Hub:

```bash
python my-gitzoid/setup.py --repos your-org/api your-org/web --save-token --publish
hubzoid schedule run my-gitzoid setup_check
hubzoid schedule run my-gitzoid repo_profiles
hubzoid schedule run my-gitzoid pr_sweep
hubzoid run my-gitzoid
```

The token prompt is hidden and saves to the Hub's private `.env`. It is never
printed. Omit `--publish` to preview against real repos first. Enabling publishing
later publishes already-reviewed current commits without paying for another
review. GitZoid comments using the PAT owner's account; their own human
`@gitzoid` commands still work. A GitHub App gives the bot its own identity.

The setup helper selects all existing eligible PRs, verification, security watch,
resumable audits, repo profiles, weekly digests and both monthly reports. It
creates one team group and keeps email optional. Existing model choices, rules
and cost budgets are retained. Original settings are backed up once as
`workflows/settings.yaml.before-setup`.

### Choose only the options you need

| Option | Selected default | Other choices |
|---|---|---|
| Existing open PRs | `--backlog all`, five per sweep | `latest` uses `first_run_limit`; `new` waits for new PRs or pushes |
| Drafts / bot PRs | Skipped until ready | `--drafts`, `--bots` include them |
| Status | `--status report` gives information | `gate` fails on blockers/incomplete diffs; `off` posts no new status |
| Publishing | Preview unless `--publish` supplied | `--no-publish` returns to preview |
| Email | Off | `--email you@company.com` or several lists in settings |
| Old PR age | No cutoff | Set `review.max_age_days`; `0` disables cutoff |
| Busy PR pushes | Keep reviewing | Set `review.auto_pause_after`; `0` disables auto-pause |
| Findings | High severity, second-pass verification | Medium/low floor, max findings, model, rules and analyzers in settings |
| Report depth | Up to 100 PR diffs, 24,000 characters each; cached code summaries | `reports.evidence_prs`, `evidence_chars_per_pr`, `synthesis_chars` |
| Model budget | 400 calls/day | `limits.model_calls_per_day`; unavailable work is surfaced |

Settings changes apply on the next run. You can always request a particular PR
with `@gitzoid review`, including one left in the backlog; repo skip rules still
apply. Closed and merged PRs are not retroactively reviewed. Reports include
merged PRs from their selected time period, even when GitZoid never reviewed
them; automated review statistics begin when GitZoid actually runs. Changing
backlog to `all` picks up previously skipped backlog PRs.

### GitHub App (optional, unattended use)

Install your App on the selected repos with the same permissions above. Instead
of `GITHUB_TOKEN`, set these secrets in the Hub `.env` or Hub secret store:

```dotenv
GITHUB_APP_ID=your-app-id-or-client-id
GITHUB_INSTALLATION_ID=your-numeric-installation-id
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
```

Use the actual PEM contents, with real newlines or escaped `\n`, and protect the
`.env` file. GitZoid signs short-lived JWTs, obtains installation tokens and
refreshes them before expiry. It identifies itself through `/app`; installation
tokens do not support `/user`. Do not paste a one-hour installation token into
`GITHUB_TOKEN` for unattended operation. App credentials take precedence when
present. Token creation follows GitHub's [installation authentication flow](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app).

## Use it with your team on Hubzoid

Local quickstart uses Hubzoid's local owner. For a signed-in deployment, choose
the ordinary automation account in `HUBZOID_WORKFLOW_USER`, ensure it has access
to this agent, and keep the Hub process running. Schedules stop when the process
stops or the host sleeps; polling resumes on restart. Use the deployment's
existing service manager and persist the Hub folder **including `.hubzoid/`**.
Use Hubzoid backup/restore before upgrades; GitZoid migrates its SQLite state in
place. A CLI subscription login on your laptop does not establish hosted access.

In **Console → this agent → Access**, grant teammates `use_hub` and `gitzoid`.
Grant `wf_code_report`, `wf_delivery_report`, `wf_repo_profiles` and
`wf_review_diff` for personal runs; `schedule_reports` permits personal schedules.
Rule approvers need `gitzoid_admin`. People reports are enabled but require
`gitzoid_manager`, `wf_people_report` and the `gitzoid_groups` attribute listing
the manager's groups. Counts describe activity; they do not score people.

For company-wide report links, configure sign-in and `HUBZOID_SHARE_DOMAINS`,
then grant the automation account `share_domain_links`. Without domain sharing,
team reports fall back to this agent's users and state that audience. Personal
reports remain private. Security reports are shared only with agent users.
Email recipients must be allowed by Hubzoid; delivery failures are shown and
alerts retry. Add recipients only when you want actual email notifications.

### Troubleshooting and cost

- **No reviews:** run `setup_check`; check Console for authentication, paused or
  skipped PRs, budget limits and workflow-account errors. Keep `hubzoid run` alive.
- **403 / repository missing:** check token/App repo selection, organization
  approval and permission scopes. Missing CI permissions are shown as unknown CI;
  they never count as passing CI.
- **Model errors:** authenticate the selected runtime or provide its provider key.
  PR reviews commonly use one review call plus verification; changed unresolved
  findings can add a resolution call. Profiles and code summaries add calls;
  cached reports reuse prior code evidence. Hosting/model charges are separate.
- **Incomplete review:** inspect coverage. Unavailable or over-budget patches
  cannot clear a merge gate. Raise the diff budget or split the PR.
- **First reports:** merged changes are not proof of deployment. Reports link
  source PRs and disclose sampled diffs, metadata-only records and missing checks.
- **Upgrade an existing Hub:** template files are copied at init, not auto-updated.
  Back up first, compare template files, preserve secrets/rules/settings/state,
  then run setup check and focused smoke tests. Avoid blindly using `init --force`.

### Instant reviews (webhook)

Polling every two minutes needs no public URL. For reviews within seconds:

- `workflows/settings.yaml`: `trigger: webhook`. The `webhooks:` entry for
  `github` is already there; it accepts nothing until the secret is set.
- The hub's `.env`: `WEBHOOK_SECRET_GITHUB=<a long random string>`.
- GitHub (repo or organisation settings, Webhooks): payload URL
  `https://<your hub>/webhooks/<hub>/github`, content type JSON, the same
  secret, events: Pull requests, Issue comments, Pull request review
  comments, Pushes.

The sweep keeps running every 30 minutes as a safety net.

## Team rules

- Team rules: one file each in `knowledge/review-rules/`. Front matter:
  `title`, `paths`, `severity`, and an optional `pattern` that code checks on
  added lines.
- Repo rules: `.gitzoid/review.md` (or `review.md`) on the default branch:
  `skip`, `severity_floor`, `ignore`, `focus`, `skip_title`, `base_branches`,
  `paths` (glob: instruction), and free text. Conventions come from
  `CLAUDE.md`, `AGENTS.md` or `CONTRIBUTING.md`, plus `.cursorrules` and
  `.github/copilot-instructions.md`.
- Who can change them: only hub admins approve drafts (`decide_rule`), which
  writes the rule file; commit approved files to keep them in git. Repo rule
  files change by merging to the default branch, so protect `.gitzoid/` with
  a CODEOWNERS entry.

## The fix loop

Your own coding agent fixes; GitZoid reviews each push and never commits.

1. Connect this agent to Claude Code or Codex over MCP (Hubzoid's MCP setup)
   and sign in to `gh`.
2. Install the skill: copy `client/skills/gitzoid-fix/` to
   `~/.claude/skills/` (Claude Code) or `~/.codex/skills/` (Codex).
3. Ask: "fix the gitzoid findings on storefront#42 until it's clean".

The agent reads the findings with `pr_details`, fixes, runs the tests,
pre-checks with `review_diff`, pushes, waits for GitZoid's review of the new
commit and repeats, at most three rounds. It never dismisses a finding.

**Merge gate.** `review.status_check: gate` makes `gitzoid/review` fail while
a blocking finding (open, high, bug or security) is open; make it a required
check in branch protection. Keep a required human approval too: GitZoid's
green status is not an approval. With the gate on, the pull request's own
author cannot dismiss or defer a blocking finding.

Adding the `gitzoid-skip` label stops reviews on a pull request but leaves
any `gitzoid/review` status it already has, so a skip label cannot lift the
merge gate. Removing the label brings the pull request back.

**In CI (optional).** `client/github-actions/gitzoid-fix.yml` runs the same
loop with your own Claude Code or Codex action and key when someone adds the
`gitzoid-fix` label. Same-repo branches only, never forks.

## Moving from WaveAssist GitZoid

Same repos, `review.md` files, groups and recipients. Copy `security_groups`,
`digest_groups` and recipients into `workflows/settings.yaml`, start with
`post_to_github: false`, compare a week of results, then post. Differences:
email keeps GitZoid's layout with key updates, next actions and a report link
(no PDF attachment; plain-text fallback included), recipients must be at your
company domain or have an account, and repo profiles refresh when the code
changes instead of every 14 days.

## Presentation in 1.3.3

Emails use GitZoid's own layout again: the `/gitzoid` chip, a title that
matches the subject, a stats bar, a summary box, then sections and one link to
the full page. Security emails put code and access issues first and group
each package's advisories into one card with one fix. Long emails drop lower
items first; the page always has everything. They need Hubzoid 1.3.3, which
sends workflow HTML email (`body_format="html"`).

Pull request comments keep the verdict and merge-risk header, then use
GitZoid's own layout again: what the change does, issues and security
findings as `_high_` `path:line` rows, and suggestions, resolved findings, the
fix-all prompt and run details collapsed. Inline comments read
`**🐛 Bug** · high severity`. A finding the second pass confirmed
can carry a one-line committable suggestion, and every advisory for one
package version is one finding, with the upgrade as the suggestion when the
line is a plain pin. A partial review says so above the fold. A review reads
up to 200,000 characters of diff (`review.max_diff_chars`), code first, and
skips large build output and minified or generated files (anything under 20 KB
is always read).

The security watch skips lockfiles in test, sample, fixture and example
folders, and the deep audit skips large minified and generated files whatever
their name. Add your own globs with `security.exclude_paths` or a repo's
`exclude_paths`. The deep audit keeps its progress when the default branch
moves, and an audit that is still in progress is not reported as a check that
could not run; a window whose content changed is read again. A finding the
model words differently next week, in the same function, is the same finding
and is not emailed again. A second pass
(`audit.verify`) tries to refute each finding before anyone is alerted.

## Presentation in 1.3.2

Reviews group issues and security findings, keep suggestions and verified fixes
collapsed, and show the reviewed commit and any coverage limits. Reports lead
with the changes and actions, then measurements and source evidence. Email uses
Hubzoid 1.3.2's safe Markdown format with existing recipient and length limits.
Existing Hubs need the updated GitZoid workflow files as well as Hubzoid 1.3.2;
upgrading the package alone does not overwrite a personalized Hub. Preserve
your settings, rules and `.hubzoid` state when copying template updates.

## Validation for 1.3.1

The fictional sample, real-model reviews, native chat/report viewing and live
GitHub reads were exercised. Publication/retry behavior, App-token renewal,
commands and audit recovery have automated contract tests. A real GitHub App
installation, live PR publication, external webhooks and SMTP delivery still
need acceptance against your deployment before enabling them. Preview mode
lets you check review quality without posting to GitHub.
