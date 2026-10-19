# Hub settings and checks

A hub can declare the settings it needs and its own setup checks. Both are
optional: a hub without these files works as before.

## Declare settings: `settings.yaml`

```yaml
version: 1
settings:
  - name: FACTORY_REPO
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
reference, never a value.

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

## Configure in the Console

Agents, then the agent, then **Setup**:

- the settings form, with each value's source;
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
    return {"status": "ok", "summary": f"{repo} is configured"}
```

How checks run:

- Each check runs in its own process with its deadline, at most four at a time.
  A crash or a hang becomes one failed row and never stops the others.
- `context` holds `hub_dir`, the non-secret `settings`, and for secret settings
  only whether each is `available`.
- Checks are trusted hub code, like tools. They run only when doctor runs.

`hubzoid doctor` includes `settings.files`, `settings.values` and one
`probe.<id>` row per check. `hubzoid hub doctor .` runs just these.
