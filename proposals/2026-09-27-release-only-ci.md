# Release-only CI and 1.0.2

Status: requested and authorized for implementation and direct push to main by
the maintainer on 2026-09-27 in the release-recovery conversation.

## Problem and desired behavior

The 1.0.1 pipeline took 33 minutes and failed after publishing to GHCR and PyPI
because the GitHub release already existed. Pushing its fix started another
checks workflow. The maintainer wants CI only when publishing a release, no
manual artifact downloads, and a new 1.0.2 release created through GitHub's UI.

## Implementation

- Remove the push/PR checks workflow. Trigger ci.yml only on release published.
- Keep the full validation gate and both supported image architectures.
- Build and smoke-test amd64 and arm64 concurrently on native Ubuntu 24.04
  runners. Keep separate architecture caches for the publishing job, and
  export inline cache with the published image for future release builds.
- Split image publishing, PyPI publishing, and GitHub attachments into jobs,
  preserving that order. A failed job can be retried without rerunning its
  successful dependencies. Keep ci.yml and the pypi environment for existing
  PyPI trusted publishing configuration.
- Prepare package metadata and changelog for 1.0.2; push main without creating
  a tag or release. The maintainer will publish the release in GitHub.

## Validation and limits

Test workflow triggers, dependency gates, native platform mapping, cache
isolation, and retry behavior; lint the workflow and run available tests.
The full release run verifies native Docker builds and registry publishing.
Do not promise a duration before that run measures it. No application or hub
format changes, dependency removals, or weakening of release validation.

## Definition of done

A push to main starts no CI. Publishing v1.0.2 starts one release workflow;
packages are published only after validation and both image checks succeed,
and the existing GitHub release receives its package attachments.
