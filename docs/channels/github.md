# GitHub

Start work from a GitHub issue or pull request and get the answer in the same
place: the issue timeline, the pull request timeline or the inline review
thread. Agents and workflows also get `github__<operation>` tools to read and
write the connected repositories. See [Channels](../channels.md) for what
every provider shares.

There are two ways to connect:

- **Your own GitHub App**, for a hosted hub. The App belongs to your account
  or organization. Hubzoid creates it with GitHub's manifest flow, so GitHub
  hands the private key to the hub's secret store and you never copy it. Each
  call uses a token narrowed to one repository and the permissions that call
  needs.
- **A personal token**, for a hub on your laptop. Replies use your own GitHub
  account. A hosted hub refuses it.

## Example

`channels/github.yaml`:

```yaml
version: 1
id: github
provider: github
enabled: false
workflow: factory          # or agent: main, to answer as a chat
run_as: workflow
provider_config:
  mode: app
  owner: {type: organization, login: example-company}
  # app_id, installation_id and repositories are filled in by the setup
  private_key_ref: secret:channel/company-hub/github/private-key
  webhook_secret_ref: secret:channel/company-hub/github/webhook-secret
  rules:
    label: factory
```

## Settings

Under `provider_config`:

| Setting | Default | What it does |
|---|---|---|
| `mode` | `app` | `app` (your own GitHub App) or `pat` (a personal token, local hubs only). |
| `owner` | required | `{type: organization or user, login}`: who owns the App. |
| `app_id`, `installation_id` | set by setup | The App and its installation. Needed before `enabled: true`. App mode only. |
| `repositories` | set by `configure` | Up to 100 repositories, saved by id. At least one before `enabled: true`. |
| `private_key_ref`, `webhook_secret_ref` | required in app mode | References to the App's private key and webhook secret. |
| `token_ref` | required in pat mode | Reference to the personal token. |
| `bot_name` | the App's slug (the token owner's login in pat mode) | The login people mention to ask. |
| `hooks` | `[]` | Up to 20 extra events for other workflows: `{id, event, workflow}`. |
| `body_limit_bytes` | 10485760 | Largest delivery accepted, 262144 to 26214400. |
| `excluded_diff_files` | `[]` | Up to 100 path patterns left out of diffs, added to lock files and other generated files. |

Under `provider_config.rules`:

| Rule | Default | What it does |
|---|---|---|
| `mentions` | `true` | A mention of the App in a new comment starts work. |
| `label` | `factory` | This label, added by someone with triage access or more, starts work. |
| `pr_opened` | `true` | Each new pull request gets an orientation summary. |
| `ci_failure` | `true` | Failed checks on a pull request go to the workflow. |
| `status_failures` | `[]` | Up to 10 `{context, poster}` entries. A failed commit status from that login counts as a failed check. Needs `ci_failure: true`. |
| `routes` | `{}` | The work-item route each trigger asks for. |

`allowed_groups` is no longer read. A person with an account link needs the
**Start work from &lt;id&gt;** grant in Settings. In a workflow connection, labels, new pull requests,
failed checks and mentions by repository members without an account link run
under the workflow's own identity, with the GitHub user recorded as who asked.

## Connect your GitHub App

With Claude Code or Codex:

> Connect this hub to my GitHub organization example-company and repository
> example-company/widget. Create a GitHub App owned by that organization
> through GitHub's manifest flow. Use the factory workflow. Show the
> permissions and the files you will save. Never put credentials in a file
> or in this conversation.

Or by hand. `HUBZOID_PUBLIC_URL` must be the hub's HTTPS address.

```sh
hubzoid channels apply ./company-hub --file channels/github.yaml --expected-revision absent
hubzoid channels github connect ./company-hub github --owner example-company --owner-type organization --name company-factory --open
```

The page that opens says who will own the App, what it may do and where it
sends events. The App asks for metadata read, contents write, issues write,
pull requests write, checks read, actions read and commit statuses read.
GitHub creates the App and then asks where to install it. An organization
owner may need to approve. Then:

```sh
hubzoid channels github setup status ./company-hub github
hubzoid channels github configure ./company-hub github --repository example-company/widget --expected-revision 2
hubzoid channels doctor ./company-hub github
hubzoid channels github configure ./company-hub github --set enabled=true --expected-revision 3
```

`doctor` checks the App's owner, the installation, the granted permissions and
that each repository is installed. It says `webhook_delivery: unverified`
until GitHub has delivered something.

To use an App you already have, store its private key and webhook secret with
`hubzoid secrets set`, then run `hubzoid channels github import-app HUB ID
--app-id N --installation-id N --owner-id N`.

## Connect with a personal token

```sh
hubzoid channels github connect ./company-hub github-local --mode pat --repository example-company/widget
hubzoid channels github intake ./company-hub github-local --url https://github.com/example-company/widget/issues/12 --task "Explain the failure and share a plan"
```

The token is read hidden (or with `--token-stdin`), stored in the secret
store and never printed. A token connection receives no deliveries unless
you add a repository webhook yourself and set `webhook_secret_ref`. `intake`
fetches the issue and starts work as the token's owner.
`intake --route NAME` asks for another route the workflow declares. Without
it, intake asks for the `plan` route.

`connect` keeps the agent's own `channels/<id>.yaml` (its workflow, rules and
routes), says what will run and asks once whether to turn intake on. `--yes`
turns it on without asking. With `--token-stdin`, or with no terminal, nothing
asks: the connection starts paused and `connect` prints the command that
turns it on, such as
`hubzoid channels resume ./company-hub github-local --expected-revision 1`.

The token is not narrowed to one repository. Use a fine-grained token limited
to the repositories the hub needs.

## What starts work

| Trigger | When it fires |
|---|---|
| A mention | `@your-app` in a new comment, outside code, quotes and HTML comments, by an OWNER, MEMBER or COLLABORATOR. Edited comments never start anything. |
| The label | The configured label is the one just added, by a person whose current permission is admin, maintain, write or triage. The permission is checked live, and a failed check waits and retries, it never admits. |
| A new pull request | Opened by a person, not a bot. The workflow may write only on that pull request. |
| Failed checks | A failed check suite on a pull request. Which branches the hub owns, and how many repair attempts it makes, is the hub's own rule. |
| A failed status | See [Failed statuses from a reviewer](#failed-statuses-from-a-reviewer). Off unless `rules.status_failures` lists it. |

Outsiders, bots and the App's own comments are recorded and ignored, with no
reply. A repository not in the file is ignored.

### Routes

Each trigger asks for a work-item route, used when the workflow declares it
(otherwise its first route applies). A route is a ceiling the run can only
narrow. The defaults are `mention: answer`, `review_mention: revise`,
`label: triage`, `pull_request: summarize` and `ci_failure: revise`. For
example, a factory whose label goes straight to implementation:

```yaml
  rules:
    label: factory
    routes: {mention: request, review_mention: request, label: implement}
```

### Failed statuses from a reviewer

A review bot that reports through a commit status can hand its failures to
the hub like a failed check:

```yaml
  rules:
    status_failures:
      - {context: review-bot/review, poster: review-bot}   # the login that posts the status
```

A status starts work only when all of these hold: its state is `failure`, its
context is listed, it was posted by that rule's `poster`, and exactly one open
pull request has that commit as its head. Pending, success and error
statuses, other contexts and other posters start nothing. The work gets it as
a failed check with `failure_kind: status`.

`getCiFailureContext` then returns the latest failing status of each listed
context as a job with `kind: status`. When the status's `target_url` links to
a comment on the same pull request by the same login, that comment is
included. Statuses and comments are untrusted data.

GitHub sends status events only when the App has **Commit statuses: read**
and the **Status** event. New Apps ask for both. For an older App, add them in the App's settings on GitHub and accept the update on the
installation. With a token, add **Statuses** to the repository webhook.
`doctor` warns when a status rule is set but the App lacks them. Everything
else works without this permission.

## Tools for agents and workflows

A hub with an enabled GitHub connection gets `github__<operation>` tools. An
agent lists the ones it uses in its frontmatter.

| Kind | Operations |
|---|---|
| 19 reads | repository, branches, labels, tree, file content, code search, issue search, list commits, one commit, compare, issues, pull requests, issue context, comments, pull request context with bounded diffs, pull request files, reviews, check runs, CI failure context with bounded logs |
| 12 writes | create, update and close issues, comment on an issue, comment on a pull request, add and remove labels, add and remove assignees, request reviewers, open a pull request, update a pull request |

Rules:

- Every call names `repository_id`. Work started by the channel may use only
  its own repository.
- Writes run only inside a work item, as recorded writes that are never
  repeated on their own. The work may have started anywhere: GitHub, chat,
  the API, Slack or Linear.
- Unattended work (a webhook or a service, with no person asking) writes only
  on the issue or pull request it came from and never opens an issue. One
  exception: work started by the label may open one draft pull request, for a
  branch it pushed itself with `push_branch`.
- A new pull request is always a draft. With `head_sha`, it opens only if the
  head branch is still at that commit. An interrupted creation is settled by
  finding the pull request for that head branch, never by opening a second.
- Commit SHAs, branch names and file paths are checked before they go into a
  URL, so no argument can reach another repository.
- The repository is looked up by its id before each call. A renamed
  repository keeps working. One moved to another owner is refused until the
  file is updated.

### Update a pull request

Updating a pull request (title, body, base, closing it, or marking it ready
with `draft: false`) is the action `github.pr_update`. It is off until the
hub's `work-items.yaml` sets it to `approve`:

```yaml
# work-items.yaml
version: 1
write_modes:
  github.pr_update: approve
```

Then the work waits until the person who asked approves that exact change, in
the agent's **Work** tab in Settings or with `hubzoid operations approve`. The
approval covers the pull request at its current head commit, so a push after
it needs a fresh approval. This includes `updateIssue` and `closeIssue` on a
number that is a pull request. Unattended work and agents inside a station
turn never update a pull request.

## Push a branch from workflow code

Agents never hold a GitHub token. An isolated agent commits in its sandbox,
Hubzoid exports those commits as a patch, and workflow code pushes them:

```python
from hubzoid.channels import config
from hubzoid.channels.github import operations

conn = config.active(hub.hub_dir, "github")
pushed = operations.push_branch(
    conn, repository_id=2001, branch="factory/12-fix-total",
    patch=patch_bytes, patch_sha256=checked_digest, base_sha=base_commit,
    expected_head=None)            # or the head this work pushed last time
```

- It runs only in a work item whose route allows the action `github.push`,
  and never from inside an agent's turn.
- The host clones the repository at the start commit, applies the patch with
  hooks off, and pushes with a token for that one repository with
  `contents: write`. The token stays in that process. In personal token mode
  it pushes with your whole token.
- It refuses the default branch, protected branches, an existing branch when
  `expected_head` is empty (`branch_exists`) and a branch that moved
  (`stale_head`). The push carries a lease on the exact head it expects, so
  nothing is overwritten.
- `patch_sha256` must match the bytes, so what is pushed is what the hub
  checked.
- The commit id is deterministic (the committer date is the author date), so
  an interrupted push is recognised by reading the branch, never pushed twice.
  `key=` tells later pushes to the same branch apart.

## Commands

| Command | What it does |
|---|---|
| `hubzoid channels github connect HUB ID --owner LOGIN [--owner-type user] [--name N] [--open]` | Start the App setup and print the page that creates the App. |
| `hubzoid channels github connect HUB ID --mode pat --repository OWNER/NAME [--token-stdin] [--yes]` | Connect a local hub with a personal token. |
| `hubzoid channels github setup status / verify / cancel HUB ID` | Follow, finish (`--installation-id N`) or cancel the App setup. |
| `hubzoid channels github import-app HUB ID --app-id N --installation-id N --owner-id N` | Use an App you already have. |
| `hubzoid channels github repositories HUB ID` | The repositories the installation or token can reach. |
| `hubzoid channels github configure HUB ID --expected-revision N [--repository OWNER/NAME] [--set KEY=JSON]` | Choose repositories (saved by id) and change settings in one revision. |
| `hubzoid channels github intake HUB ID --url URL --task TEXT [--route NAME]` | Personal token mode: start work on an issue. |
| `hubzoid channels github call HUB ID --operation OP --arguments-file F` | Run one operation as an agent would. Writes run only inside channel work. |

Each takes `--json`.

## Troubleshooting

| Code | What to do |
|---|---|
| `public_url_required` | Set `HUBZOID_PUBLIC_URL` to the hub's HTTPS address. |
| `setup_expired`, `setup_state_invalid` | Start a new setup. |
| `setup_conversion_unknown` | The App may exist already. Check your GitHub Apps: import it, or delete it, then start again. |
| `app_owner_mismatch`, `installation_mismatch` | The App or installation belongs to someone else. Nothing was activated. |
| `installation_pending`, `repository_not_installed` | Approve the installation on GitHub, or add the repository to it. |
| `permission_missing` | Accept the App's updated permissions on GitHub. For `status_failures`, add Commit statuses (read) and the Status event. |
| `credential_rejected` | GitHub refused the token or the App's private key. Store it again, then run `doctor`. |
| `pat_remote_forbidden` | Use App mode for a hosted hub. |
| `pat_only` | `intake` is for personal token mode. An App receives webhooks. |
| `signature_invalid` | The webhook secret does not match. |
| `pr_update_requires_approval` | Set `github.pr_update: approve` in `work-items.yaml`, or comment instead. |
| `branch_exists`, `stale_head` | The branch is not where this work left it. Nothing was overwritten. Check who pushed. |
| `branch_protected`, `default_branch_refused` | Push to a branch of the hub's own and open a draft pull request. |
| `patch_changed` | The patch changed after it was checked. Export and check it again. |
