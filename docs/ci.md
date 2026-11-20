# CI for a hub

A hub is a folder in Git, so every change can be checked before it reaches the
team. Use CI to run the hub's own tests and the evals it must pass, and to stop
a change that breaks them from merging.

```bash
hubzoid eval run ./my-hub --required --junit eval-results.xml
```

## What runs

| Check | What it tests | Needs a model |
|---|---|---|
| `pytest` | The hub's own code: local tools, connectors, scripts in `tests/` | No |
| `hubzoid eval run --required` | The agent's behaviour: the evals `quality/manifest.yaml` requires ([required evals](evals.md#required-evals-in-doctor)) | Yes |

## Exit codes

`--required` runs exactly the cases the manifest requires, chosen as
`hubzoid doctor` chooses them, on the hub's own model.

| Exit | Meaning |
|---|---|
| 0 | Every required case passed. `hubzoid doctor` now shows them `ok` |
| 1 | A required case failed. `hubzoid eval explain` shows why |
| 2 | The gate cannot be trusted, so nothing ran. See the list below |

Exit 2 when any of these is true:

- There is no `quality/manifest.yaml`, or it cannot be read, does not start
  with `version: 1`, has an unknown key, or `required` or `required_tags` is
  not a list of names.
- The manifest requires nothing.
- A name, glob or tag in the manifest matches no case.
- A case file under `evals/` cannot be read.
- A required case has `enabled: false`.
- A required case checks nothing: no `contains`, `not_contains`,
  `expect_tools`, `forbid_tools`, `expect_tool_args`, `fake_tools` or
  `## Criteria`.
- The hub has more than 100,000 files, so doctor can never confirm its results
  are current.
- `--tag`, `--case`, `--no-judge` or `--model` is given with `--required`.
  `--judge-model` and `--run-as` are allowed.
- `--run-as` is not a valid account.

A case passes when every check in it passes. With `## Criteria` that includes
a judge score at or above the case's `threshold` (default 7 out of 10,
[evals](evals.md#adding-checks)). There is no overall pass rate. Every
required case must pass. For a first set of required cases, see the
[starter safety cases](guardrails.md#starter-safety-cases).

Each results file records the Hubzoid version, the hub's Git commit and whether
the hub folder had uncommitted changes, so a pass is tied to what was tested.
The JUnit file carries the same values as properties (`hubzoid_version`,
`git_commit`, `git_dirty`).

## Gate a release in CI

1. Copy [`examples/ci/hubzoid-checks.yml`](../examples/ci/hubzoid-checks.yml)
   to `.github/workflows/hubzoid-checks.yml` in the hub's repository.
2. Set `HUB_DIR` in it to the folder holding `AGENTS.md`.
3. Add the model credential as a repository secret (below).
4. In the repository's branch protection, make the `checks` job a required
   status check, so a change that breaks a required eval cannot merge.

On every pull request and every push to `main` the workflow:

1. installs the hub's `requirements.txt`, or Hubzoid alone when there is none
2. installs `pytest` and runs it when the repository has `tests/**/*.py`
3. runs `hubzoid eval run "$HUB_DIR" --required --junit eval-results.xml`
4. keeps the JUnit file and the results JSON as a build artifact, even when a
   step failed

The workflow sets `CI=true`, so results record the trigger as `ci`.

## Add the model credential

Evals run the real agent, so CI needs the hub's model credential: an
`ANTHROPIC_API_KEY` or a Claude subscription token (`CLAUDE_CODE_OAUTH_TOKEN`)
for a Claude hub, or the provider key for another model. Store it as a GitHub
repository secret, never in the hub's files. For another provider, add its key
to the workflow's `env:` the same way.

Each run costs about one agent turn per case, plus one judge call per case with
`## Criteria` ([what a run costs](evals.md#what-a-run-costs)).

## Troubleshooting

**The check fails on a pull request from a fork.** Forks get no secrets, so the
gate cannot run there. The example fails the check rather than passing it
unchecked. A maintainer pushes the branch to the repository to run it.

**Exit 2.** The message names the problem. Fix the manifest or the case file,
or remove the flag that `--required` does not accept.

**A required check never reports.** The example has no `paths:` filter on
purpose. A filter would leave a required check waiting on changes it skips.
