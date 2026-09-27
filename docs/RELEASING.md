# Publishing a release

Starting with 1.0.2, CI runs only when a GitHub release is published. Branch
pushes, pull requests, and tag pushes alone do not run CI. Run tests locally
before pushing changes.

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
