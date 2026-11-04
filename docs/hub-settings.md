# Hub settings and checks

A hub can declare the settings it needs and its own setup checks. Both are
optional: a hub without these files works as before.

## Declare settings: `settings.yaml`

```yaml
version: 1
settings:
  - name: FACTORY_REPO
    label: Repository      # optional; Settings shows it instead of the name
    description: Repository the factory works on
    required: true
    regex: '^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$'   # anchored; linear-time engine
    example: acme/widgets
  - name: FACTORY_LABEL
    default: factory
  - name: MAX_RETRIES
    type: integer          # string (default), integer, number, boolean, string_list
    minimum: 0
    maximum: 5
    env: FACTORY_MAX_RETRIES   # this environment variable wins when set
  - name: GITHUB_TOKEN
    secret: true
    required: true
```

Names are `UPPER_CASE`. A secret setting has no default and its example is a
reference, never a value. Settings shows `label` where one is given. Without
it, the name reads as words, without the first word most of the agent's
settings share: `FACTORY_BRANCH_PREFIX` shows as "Branch prefix". A `label`
needs Hubzoid 2.0.7 or later; older versions refuse the file.

## Set values: `hub.settings.yaml`

```yaml
version: 1
values:
  FACTORY_REPO: acme/widgets
  MAX_RETRIES: 2
secrets:
  GITHUB_TOKEN_ref: secret:hub/factory/github-token   # or env:GITHUB_TOKEN
```

A value comes from, in order:

1. the environment variable named by `env`;
2. `hub.settings.yaml`;
3. the default.

A secret setting holds a reference, never a value. Without one it uses
`env:NAME`, so the key in `.env`, or one stored with `hubzoid secrets set
--name NAME`, satisfies it. Secret settings also appear in the
[Secrets](secrets.md) list.

## Use settings in instructions

AGENTS.md (and sub-agents) and skills may write `{{settings.KEY}}`:

```markdown
Work only on {{settings.FACTORY_REPO}}. Retry at most {{settings.MAX_RETRIES}} times.
```

Each token is replaced once when the hub loads. Text stays as it is, and other
values are written as JSON. Nothing is evaluated, and a replaced value is never
scanned again.

An unknown, secret or unset key stops the hub from loading, with the file and
line, so every runtime gets the same instructions or none starts.

## Use settings in workflows

```python
repo = hub.setting("FACTORY_REPO")    # workflows/settings.yaml first, then the hub's settings
token = hub.secret("GITHUB_TOKEN")    # follows the declared reference
```

## Configure from the terminal

```bash
hubzoid hub settings .
hubzoid hub configure . --check
hubzoid hub configure . --apply --expected-revision absent
```

- `hubzoid hub settings .` lists values, references and sources, plus every
  problem.
- `hubzoid hub configure . --check` validates `hub.settings.yaml` and lists
  every problem at once.
- `--apply` writes the file, or a `--file` you give, and records a revision.
  `--expected-revision` takes the number `hub settings` shows, or `absent`.
- Exit codes: 0 done, 1 a check failed, 2 invalid, 4 the revision changed.

A change applies after the hub restarts. Editing the file by hand also moves
the revision on, so a stale editor is refused.

## Configure in Settings

Agents, then the agent, then **Configuration**, **Setup**. Saving the form there is Beta and off by default (see [DEPLOYING](DEPLOYING.md#in-app-editing)):

- the settings form, each setting under its label (hover a label for its
  name), with a note when the server's environment sets the value;
- the secret settings and whether each is available;
- a **Run checks** button.

The hub's administrators can change values. Changing which secret a setting
references needs an organization administrator.

The API is:

- `GET /api/hubs/{hub_id}/settings`
- `GET .../settings/schema`
- `POST .../settings/validate`
- `PUT .../settings`, with `expected_revision`
- `POST .../doctor`
- `GET .../get-started`, the [Get started cards](#get-started-cards-get-startedyaml)

## The hub's own checks: `hub.manifest.yaml`

```yaml
version: 1
doctor:
  - id: repository
    entrypoint: doctor/repository.py:check
    timeout_seconds: 10        # at most 30
    requires: [FACTORY_REPO]   # not run until these are set
    required: true             # a failure fails doctor (otherwise it warns)
```

```python
# doctor/repository.py
def check(context):
    repo = context["settings"]["FACTORY_REPO"]
    if not context["available"]["GITHUB_TOKEN"]:
        return {"status": "warn", "summary": "No GitHub token yet", "recovery": "hubzoid secrets set . --name GITHUB_TOKEN"}
    return {"status": "ok", "summary": f"{repo} is configured",
            "card": {"line": repo, "detail": "GitHub connected"}}  # optional, for Get started
```

How checks run:

- Each check runs in its own process with its deadline, at most four at a time.
  A crash or a hang becomes one failed row and never stops the others.
- `context` holds `hub_dir`, the non-secret `settings`, and for secret settings
  only whether each is `available`.
- Checks are trusted hub code, like tools. They run only when doctor runs, or
  when a manager opens the agent's Get started cards (only the checks the cards
  name).
- `card` is optional: a `line` (at most 60 characters) and a `detail` (at most
  80) for the check's Get started card. Doctor's output does not show it.

`hubzoid doctor` includes `settings.files`, `settings.values` and one
`probe.<id>` row per check. `hubzoid hub doctor .` runs just these.

## Get started cards: `get-started.yaml`

An optional file at the hub root. A manager sees these cards on the chat's
empty screen, with the status of each check they name. Older Hubzoid versions
ignore the file.

```yaml
version: 1
title: Get started          # optional, at most 40 characters
cards:                      # 1 to 6, ids unique
  - id: model
    title: Model            # at most 24 characters
    check: model            # built in: the agent's model sign-in
  - id: code
    title: Code
    check: repository       # a check in hub.manifest.yaml
  - id: results
    title: Results
    line: Reviewed branches # a static card: a line (at most 60) and an optional detail (at most 80)
    detail: After you approve the plan
```

- A card has a `check` or a `line`, not both. Unknown keys are refused.
- `check: model` shows the login the agent uses (for example "Claude Code, Team
  plan"), or the model's name for an API model, with the same status and fix as
  the `model.signin` diagnostic.
- A checked card shows the check's status, its `card` line and detail (else the
  card's own `detail`, and the summary for the line), and its recovery as the fix.
- `hubzoid hub doctor` adds a failed `get_started` row when the file is invalid.

`GET /api/hubs/{hub_id}/get-started` answers the hub's administrators from
the site's own pages. Anyone else gets 404, like the other routes here, and so
does an agent without the file. It runs only the checks the cards name and
keeps the answer for 30 seconds. `?refresh=1` checks again, and the logins too,
unless the last check ended under 10 seconds ago. A request while the cards
are being checked waits for that run, and the cards and `POST .../doctor`
never run checks at once (doctor answers 429 meanwhile):

```json
{"title": "Get started", "ready": false,
 "cards": [{"id": "code", "title": "Code", "status": "ok", "line": "acme/widgets",
            "detail": "GitHub connected", "fix": null}],
 "checked_at": "2026-11-04T12:00:00Z"}
```

`status` is `ok`, `info`, `warn`, `fail`, or `null` for a static card. `ready`
is true when no card is `warn` or `fail`. An invalid file answers
`{"error": "...", "cards": []}`.
