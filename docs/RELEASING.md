# Publishing a release

Pull requests and pushes to `main` run the fast checks
(`.github/workflows/tests.yml`), and every push and pull request is scanned for
secrets (`.github/workflows/secrets.yml`). The broader validation and image
builds run before publication on a pull request labeled `release-candidate`,
or through a manual run of the release workflow. These validation runs cannot
publish packages or images. Publishing requires a GitHub release event
(`.github/workflows/ci.yml`). Its checks (every test, on two machines in
parallel, the Console journeys, packaging and both images) run side by side
on hosted runners, and publishing waits for all of them. No test is left out
for its duration. Skipped critical checks are not passes.

Before tagging, validate the exact candidate commit on a draft pull request
with the `release-candidate` label. Inspect skips, test failures, both native
image checks and clean-install results. Complete acceptance on a copy of the
intended deployment, including its runtime, sign-in, permissions and restart
persistence. Check real email delivery if that deployment promotes email.
Apply the chat-interface validation requirements below.

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

Hubzoid 2.0 has one chat interface, the Hubzoid web app (founder decisions
D030 and D031). Before release, record results for the exact candidate:

| Area | Required validation |
|---|---|
| Installation and startup | Clean install, startup, single-hub and gateway operation; validate each promoted container configuration. A retired Open WebUI setting stops the start with its message. |
| Identity and access | Sign-in, sign-out, permitted teammate access, denied access and isolation between people and agents. |
| Useful chat | A useful agent response, streaming, tools, uploads and artifact access. |
| Shared capabilities | Settings (team and personal sections), personal connectors, workflows and schedules started from chat, and access to their results. |
| Lifecycle | Upgrade (including from a 1.x Open WebUI deployment with `hubzoid migrate openwebui`) and backup/restore, plus account, conversation and schedule persistence after restart. |

Record the Hubzoid version/commit, dependency versions, runtime,
configuration, commands or manual steps, results and any provider-dependent
gaps. A skipped, timed-out or excluded critical check blocks release until it
is completed.

Hubzoid 2.0 is also gated by its Open WebUI parity gap report: each gap there
is closed, or accepted by the founder, before the 2.0 release.

### Current automation coverage

The release workflow runs the whole suite (`-m ""`, heavy tests included) with
no duration-based exclusions, plus the focused Codex and GitZoid-scanner checks
that need their own binaries. Its live smoke check starts the web app through the
public edge, verifies sign-in and refused credentials, serves JavaScript,
Settings and the old `/portal/` address, streams a scripted reply and verifies
the saved chat after restart. The browser job runs the chat journeys and stream
contracts alongside the Settings journeys.

Two Python lanes run the suite in parallel (`pytest-xdist`): the Codex and
scanner checks in lane 1, the live interface check in lane 2, and the shards
skip only those checks already run explicitly. Locally on 4 workers the whole
suite took 9.5 minutes for 2.0 (30 minutes serially); unchanged limits alone do
not prove unchanged elapsed time.

The clean-wheel and container jobs validate the default install; the two
image jobs are CPU architectures. The live smoke uses a scripted model and
HTTP clients. Complete deployment-specific and real-provider acceptance where
required by the table above. Inspect skips and exclusions; these automated
checks do not establish full feature parity by themselves.
