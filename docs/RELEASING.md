# Publishing a release

Pull requests and pushes to `main` run the fast checks
(`.github/workflows/tests.yml`), and every push and pull request is scanned for
secrets (`.github/workflows/secrets.yml`). The broader validation and image
builds run before publication on a pull request labeled `release-candidate`,
or through a manual run of the release workflow. These validation runs cannot
publish packages or images. Publishing requires a GitHub release event
(`.github/workflows/ci.yml`). Its checks (the tests on two machines without the
`heavy` ones, the Console journeys, packaging and both images) run side by side
on hosted runners, and publishing waits for all of them. A focused step also
runs report recovery and PostgreSQL claim/upgrade tests regardless of their
`heavy` classification. A second step restores duration-excluded workflow,
PostgreSQL, report-sample and schedule-control checks. Excluded or skipped
critical checks are not passes.

Before tagging, validate the exact candidate commit on a draft pull request
with the `release-candidate` label. Inspect skips, test failures, both native
image checks and clean-install results. Complete acceptance on a copy of the
intended deployment, including its runtime, sign-in, permissions and restart
persistence. Check real email delivery if that deployment promotes email.
Apply the chat-interface validation requirements below to both supported modes.

1. Update `pyproject.toml` and `CHANGELOG.md` to the new version and push main.
2. In GitHub, open **Releases → Draft a new release**.
3. Choose **Choose a tag**, enter the new tag (for example `v1.0.2`), and
   choose **Create new tag on publish**. Set **Target: main**.
4. Enter the release title and notes, then click **Publish release**.
5. Under **Actions → release**, wait for validation, both image builds, and
   publishing to finish. The workflow uploads the wheel and source archive
   to the release automatically. No downloads or manual attachments are needed.

The GitHub release page becomes visible before validation finishes. PyPI and
the versioned Docker image are published only after validation and both native
image startup checks succeed. If the `pypi` environment requires approval,
approve its pending jobs in the workflow run.

For a transient failure, open the run and select **Re-run jobs → Re-run failed
jobs**. Successful dependency jobs are reused. Docker, PyPI, and GitHub
attachments have separate jobs, so a failed attachment upload does not repeat
the image build or PyPI publish. Reruns use the original tag's workflow and
source; a code or workflow fix needs a new version and release.

Do not reuse a version already published to PyPI or move an existing release
tag. Publishing creates external artifacts in sequence; a later failure does
not undo earlier successful publishing.

## Chat interface validation

Native chat is the default. Native chat and Open WebUI are permanently and
equally supported, with no removal planned. Both must meet the same release
quality bar. An optional dependency does not make its interface optional to
validate. Equal testing means checking the supported behavior in each mode,
not requiring identical UI features or identical test counts.

Before release, record results for the exact candidate in **both** modes:

| Area | Required validation in native chat and Open WebUI |
|---|---|
| Installation and startup | Clean install, selected interface startup, single-hub and gateway operation; validate each promoted container configuration. |
| Identity and access | Sign-in, sign-out, permitted teammate access, denied access and isolation between people and agents. |
| Useful chat | A useful agent response, streaming, tools, uploads and artifact access through each interface's supported flow. |
| Shared capabilities | Console access, personal connectors, workflows and schedules started from chat, and access to their results. |
| Lifecycle | Upgrade and backup/restore of each mode's data, plus account, conversation and schedule persistence after restart. |

Record the Hubzoid version/commit, interface and dependency versions, runtime,
configuration, commands or manual steps, results and any provider-dependent
gaps. Run focused regressions for fixes in either interface. A skipped,
timed-out or excluded critical check blocks release until it is completed;
installing a dependency or passing the other interface's tests is not a pass.

### Current automation coverage

The fast workflow installs the core package without Open WebUI. The release
workflow installs the Open WebUI extra and runs tests covering both modes,
with heavy-test exclusions and selected explicit regressions. Its paired live
smoke check starts each real interface through the public edge, verifies
sign-in and refused credentials, serves JavaScript and the Console, streams a
scripted reply and verifies the saved chat after restart. A missing Open WebUI
extra fails this check in CI. The browser job also runs the existing native
chat journeys and stream contracts alongside Console journeys.

The same two Python lanes remain: critical workflow checks run in lane 1,
live interface checks in lane 2, and the general shards exclude checks already
run explicitly. No distinct regression tests are removed. Job counts and
timeouts are unchanged. Compare candidate timings with the successful 1.3.1
run 38347956989 (longest validation job: 14m34s); unchanged limits alone do not
prove unchanged elapsed time.

The clean-wheel and container jobs still validate the default native install;
the two image jobs are CPU architectures, not chat interfaces. The live smoke
uses a scripted model and HTTP clients, not an Open WebUI browser journey.
Complete deployment-specific, Open WebUI browser/container and real-provider
acceptance where required by the table above. Inspect skips and exclusions;
these automated checks do not establish full feature parity by themselves.
