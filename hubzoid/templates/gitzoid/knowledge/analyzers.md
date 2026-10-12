---
title: Open-source analyzers
description: Optional tools GitZoid runs on each pull request's changed files.
---

The supplied Ruff and Gitleaks presets scan changed files as data, including
dotfiles. They ignore PR-controlled configuration and suppression markers. Keep
their `run` lists unchanged to retain this isolated preset behavior.

Other SARIF commands, including modified presets, need `allow_project_code: true`
on that analyzer under `gitzoid.analyzers` in `workflows/settings.yaml`. These
commands are **not sandboxed** and may execute PR-controlled configuration. An
operator must decide whether the repositories and execution environment are
appropriate before enabling them. The setting does not create a sandbox.

Results on added lines are evidence for the reviewer; they do not become findings
by themselves. Missing tools and custom commands without opt-in are skipped with
a note in the summary's run details. The other tools below are custom-command
options, not supplied isolated presets.

| Tool | Finds | Install |
|---|---|---|
| Ruff | Python bugs and security patterns | `pip install ruff` |
| Gitleaks | Secrets | `brew install gitleaks` |
| OSV-Scanner | Vulnerable dependencies | `brew install osv-scanner` (`--format sarif`) |
| Opengrep | Semgrep-style rules for many languages | see opengrep.dev |
| zizmor | GitHub Actions workflow problems | `pip install zizmor` |
