# GitHub

Work starts from a GitHub issue or pull request and the answer comes back in
the same place: the issue timeline, the pull request timeline, or the inline
review thread. See [Channels](../channels.md) for what every provider shares.

## Two ways to connect

- **Your own GitHub App** (for a hosted hub). The App belongs to your account
  or organization. Hubzoid creates it with GitHub's manifest flow, so you
  never copy a private key: GitHub hands it to the hub's secret store
  directly. Each call uses a token narrowed to one repository and only the
  permissions that call needs.
- **A personal token** (for a hub on your laptop). Replies use your own
  GitHub account. Refused for a hosted hub.

## The file

```yaml
version: 1
id: github
provider: github
enabled: false
workflow: factory          # or agent: main, to answer as a chat
run_as: workflow
allowed_groups: [engineering]
provider_config:
  mode: app
  owner: {type: organization, login: example-company}
  # app_id, installation_id and repositories are filled in by the setup
  private_key_ref: secret:channel/company-hub/github/private-key
  webhook_secret_ref: secret:channel/company-hub/github/webhook-secret
  rules:
    mentions: true         # @your-app in a comment by an owner, member or collaborator
    label: factory         # this label added by someone with triage access or more
    pr_opened: true        # an orientation summary for each new pull request
    ci_failure: true       # failed checks on a pull request go to the workflow
  hooks: []                # extra events for other workflows: {id, event, workflow}
  body_limit_bytes: 10485760
  excluded_diff_files: []
```

## Setting it up with your assistant

> Connect this hub to my GitHub organization example-company and repository
> example-company/widget. Create a GitHub App owned by that organization
> through GitHub's manifest flow. Use the factory workflow. Show the
> permissions and the files you will save. Never put credentials in a file
> or in this conversation.

```sh
hubzoid channels apply ./company-hub --file channels/github.yaml --expected-revision absent
hubzoid channels github connect ./company-hub github --owner example-company --owner-type organization --name company-factory --open
```

The page that opens says who will own the App, what it may do (metadata
read, contents write, issues write, pull requests write, checks read, actions
read) and where it sends events. GitHub creates the App and then asks where
to install it. An organization owner may need to approve. Then:

```sh
hubzoid channels github setup status ./company-hub github
hubzoid channels github configure ./company-hub github --repository example-company/widget --expected-revision 2
hubzoid channels doctor ./company-hub github
hubzoid channels github configure ./company-hub github --set enabled=true --expected-revision 3
```

`doctor` checks the App's owner, the installation, the granted permissions and
that each repository is installed. It says `webhook_delivery: unverified`
until GitHub has delivered something.

An App you already have: store its private key and webhook secret with
`hubzoid secrets set`, then `hubzoid channels github import-app ... --app-id N
--installation-id N --owner-id N`.

### On your laptop, with a token

```sh
hubzoid channels github connect ./company-hub github-local --mode pat --repository example-company/widget
hubzoid channels github intake ./company-hub github-local --url https://github.com/example-company/widget/issues/12 --task "Explain the failure and share a plan"
```

The token is read hidden (or `--token-stdin`), stored in the secret store, and
never printed. Intake fetches the issue and acts as the token's owner.

## What triggers work

| Trigger | Rule |
|---|---|
| A mention | `@your-app` in a new comment, outside code, quotes and HTML comments, by an OWNER, MEMBER or COLLABORATOR. Edited comments never start anything. |
| The label | The configured label is the one just added, by a person whose current permission is admin, maintain, write or triage (checked live; a failed check waits, it never admits). |
| A new pull request | Opened by a person, not a bot. The workflow may write only on that pull request. |
| Failed checks | A failed check suite on a pull request. Which branches the hub owns, and how many repair attempts it makes, is the hub's own rule. |

Outsiders, bots and the App's own comments are recorded and ignored, with no
reply. A repository not in the file is ignored.

## Tools for agents and workflows

A hub with an enabled GitHub connection gets `github__<operation>` tools: 19
reads (repository, branches, labels, tree, file content, code and issue
search, commits, compare, issues, pull requests, issue context, comments,
pull request context with bounded diffs, files, reviews, check runs, CI
failure context with bounded logs) and 12 writes (create, update and close
issues, comment, labels, assignees, reviewers, open a pull request, update a
pull request). An agent lists the ones it uses in its frontmatter. Rules:

- Every call names `repository_id`, and work started by the channel may use
  only its own repository.
- Writes run only inside channel work, as recorded writes that are never
  repeated on their own. Unattended work writes only on the issue or pull
  request it came from, and never opens a new issue or pull request.
- A new pull request is always a draft.
- Updating a pull request waits for a person's approval of the exact change,
  which arrives with the shared approvals service. Until then it is refused.
  This includes `updateIssue` and `closeIssue` on a number that is a pull
  request.
- Commit SHAs, branch names and file paths are checked before they go into a
  URL, so no argument can reach another repository.
- The repository is looked up by its id before each call. A renamed
  repository keeps working; one moved to another owner is refused until the
  file is updated.

## Troubleshooting

| Code | What to do |
|---|---|
| `public_url_required` | Set `HUBZOID_PUBLIC_URL` to the hub's HTTPS address. |
| `setup_expired`, `setup_state_invalid` | Start a new setup. |
| `setup_conversion_unknown` | The App may exist already. Check your GitHub Apps: import it, or delete it, then start again. |
| `app_owner_mismatch`, `installation_mismatch` | The App or installation belongs to someone else. Nothing was activated. |
| `installation_pending`, `repository_not_installed` | Approve the installation on GitHub, or add the repository to it. |
| `permission_missing` | Accept the App's updated permissions on GitHub. |
| `pat_remote_forbidden` | Use App mode for a hosted hub. |
| `signature_invalid` | The webhook secret does not match. |
