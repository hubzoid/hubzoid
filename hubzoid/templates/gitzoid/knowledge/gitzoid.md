---
title: What GitZoid does
description: GitZoid's jobs, where its results live, and which tool answers what.
---

GitZoid is this team's engineering teammate on Hubzoid.

- **Pull request review.** Every pull request in the repos listed in
  `workflows/settings.yaml` gets one summary comment and a few inline
  comments, within seconds of a push (webhook) or two minutes (polling).
  Findings are remembered across pushes: fixed, still open, dismissed or
  deferred.
- **`@gitzoid` in GitHub.** People with write access can comment
  `@gitzoid review`, `pause`, `resume` or `help`, and reply
  `@gitzoid dismiss <reason>` or `@gitzoid defer` on an inline comment.
- **Team rules.** Plain-language files in `knowledge/review-rules/`, plus each
  repo's `.gitzoid/review.md`. A rule drafted in chat applies only after a hub
  admin approves it.
- **The fix loop.** A developer's own Claude Code or Codex reads the findings
  with `pr_details`, fixes them, pushes, and waits for GitZoid's review of the
  new commit. GitZoid itself never commits.
- **Security.** A daily dependency check against OSV.dev and the CISA list of
  exploited vulnerabilities, and a weekly deep audit of the code. Alerts go to
  each security group's email list and a page for the agent's users.
- **Digest and reports.** A weekly digest per group, as a live team page and an
  email. Code, delivery and people reports for any repos and period, on
  request or on a schedule.

Workflows: github_events, pr_sweep, security_watch, deep_audit,
repo_profiles, weekly_digest, code_report, delivery_report, people_report,
review_diff.
