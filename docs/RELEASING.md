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
