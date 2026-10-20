# A hub's own settings and checks

Status: proposed for 2.0. Builds on the secret store and its references
(`proposals/2026-10-19-secrets-and-hub-routes.md`).

## Problem

A hub that needs a repository name, a label, a support address or an API key
has no way to say so:

- Builders copy a `.env.example` and hope people fill it in.
- A missing or mistyped value shows up only when a tool or workflow fails
  halfway through a task.
- Nothing checks a value's type or shape.
- Instructions cannot use a configured value, so authors hard-code names such
  as the repository into AGENTS.md.
- A hub cannot add its own setup checks to `hubzoid doctor`, for example "can
  I reach the repository?".

## Why now

Ready-made hubs need a setup step that an assistant can run through files and
commands, with every problem reported at once and a clear next step. Hosted
deployments need the same through the Console, with no shell.

## What should happen

- **`settings.yaml` declares settings.**
  - Each has a name, a description and a type: string, integer, number,
    boolean or string_list.
  - Optional: required, a default, an example, enum, minimum and maximum, a
    regex (linear-time), and `env` to let an environment variable supply it.
  - `secret: true` marks a secret.
- **`hub.settings.yaml` holds the values.**
  - `values.KEY` holds a value.
  - `secrets.KEY_ref` holds a reference (`secret:...` or `env:...`), never a
    value. Without one, a secret setting uses `env:KEY`, so a key in `.env` or
    one stored with `hubzoid secrets set` satisfies it.
  - Both files are optional, so existing hubs are unchanged.
- **Validation reports every problem at once**, with the key and a safe
  example: missing, wrong type, out of range, not matching, unknown, or a
  value where a reference belongs.
- **Placeholders in instructions.**
  - AGENTS.md and SKILL.md may use `{{settings.KEY}}`. Each token is replaced
    once, with no evaluation and no second pass.
  - An unknown, secret or unset key stops the hub from loading, naming the
    file and line, so every runtime gets the same instructions or none starts.
- **Workflows** read settings with `hub.setting(KEY)` and secrets with
  `hub.secret(KEY)`, which follows the declared reference.
- **The hub's own checks.**
  - `hub.manifest.yaml` lists checks (`doctor: [{id, entrypoint, timeout_seconds, requires, required}]`).
  - Each runs in its own process with a deadline (default 10 s, at most 30),
    four at a time, so a crash or a hang becomes one failed row.
  - A check whose required settings are missing is reported without running.
- **Commands.** `hubzoid hub settings`, `hubzoid hub configure --check|--apply
  [--expected-revision]` and `hubzoid hub doctor`. `hubzoid doctor` includes
  the same rows.
- **API.** `GET`/`PUT /api/hubs/{hub_id}/settings`, `.../settings/schema`,
  `.../settings/validate` and `POST .../doctor`.
- **Revisions.** Writes are guarded by a revision that also moves on when
  someone edits the file by hand. Changes apply after the hub restarts.
- **Who.**
  - The hub's administrators read, change values and run checks.
  - Changing which secret a setting references needs an organization
    administrator.
- **Console.** A **Setup** tab for each agent: the settings form, secret
  settings with their status, and a Run checks button.

## Scope and non-goals

- No package metadata, registry, install or upgrade. These come with the
  ready-made hubs.
- No settings snapshot history. The file is the authority, and git keeps its
  history.
- Checks are trusted hub code, like tools. They run only when doctor runs,
  never when a hub loads.

## Definition of done

- A hub with `settings.yaml` reports all problems together in the CLI, the
  API, the Console and doctor, and never shows a secret value.
- `{{settings.KEY}}` reaches the model's instructions on a real run, and a bad
  placeholder stops the hub with the file and line.
- A crashing, hanging and passing check in one manifest give three separate
  rows.
- Old hubs load and pass doctor exactly as before.
