---
title: Open-source analyzers
description: Optional tools GitZoid runs on each pull request's changed files.
---

Any command that prints SARIF can be added under `analyzers:` in
`workflows/settings.yaml`. Results on added lines are shown to the reviewer as
evidence; they do not become findings by themselves. An analyzer that is not
installed is skipped with a note in the summary's run details.

| Tool | Finds | Install |
|---|---|---|
| Ruff | Python bugs and security patterns | `pip install ruff` |
| Gitleaks | Secrets | `brew install gitleaks` |
| OSV-Scanner | Vulnerable dependencies | `brew install osv-scanner` (`--format sarif`) |
| Opengrep | Semgrep-style rules for many languages | see opengrep.dev |
| zizmor | GitHub Actions workflow problems | `pip install zizmor` |
