# CI for a hub

A hub is a folder in Git, so every change can be checked before it reaches the
team. Two kinds of check run in CI:

| Check | What it tests | Needs a model |
|---|---|---|
| `pytest` | The hub's own code: local tools, connectors, scripts in `tests/` | No |
| `hubzoid eval run --required` | The agent's behaviour: the evals `quality/manifest.yaml` requires ([evals](evals.md#required-evals-in-doctor)) | Yes |

## The workflow file

Copy [`examples/ci/hubzoid-checks.yml`](../examples/ci/hubzoid-checks.yml) to
`.github/workflows/hubzoid-checks.yml` in the hub's repository and set
`HUB_DIR` to the folder holding `AGENTS.md`. On every pull request and every
push to `main` it:

1. installs the hub's `requirements.txt` (or Hubzoid alone);
2. runs `pytest` when the repository has tests;
3. runs `hubzoid eval run "$HUB_DIR" --required --junit eval-results.xml`;
4. keeps the JUnit file and the results JSON as a build artifact.

Then make the `checks` job a required status check in the repository's branch
protection, so a change that breaks a required eval cannot merge. That step is
the team's, in GitHub's settings.

## The release gate

`--required` runs exactly the cases the manifest lists, chosen as
`hubzoid doctor` chooses them:

| Exit | Meaning |
|---|---|
| 0 | Every required case passed. `hubzoid doctor` now shows them `ok` |
| 1 | A required case failed. `hubzoid eval explain` shows why |
| 2 | The gate cannot be trusted, so nothing ran: no manifest, a manifest that requires nothing, a name that matches no case, a case file that cannot be read, a required case with `enabled: false` or with no checks at all, or `--tag`, `--case` or `--no-judge` given with `--required` |

Each results file records the Hubzoid version, the hub's Git commit and whether
the hub folder had uncommitted changes, so a pass is tied to what was tested.
JUnit carries the same values as properties.

## The model credential

Evals run the real agent, so CI needs the hub's model credential: an
`ANTHROPIC_API_KEY` or a Claude subscription token (`CLAUDE_CODE_OAUTH_TOKEN`)
for a Claude hub, or the provider key for another model. Store it as a GitHub
repository secret, never in the hub's files. Pull requests from forks get no
secrets, so the example skips the eval step there and runs `pytest` only.

Each run costs about one agent turn per case, plus one judge call per case with
`## Criteria` ([what a run costs](evals.md#what-a-run-costs)).
