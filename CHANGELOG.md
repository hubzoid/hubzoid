# Changelog

All notable changes to Hubzoid. Versions follow the package version in
`pyproject.toml`; each release tag `vX.Y.Z` must have a section here.

## [Unreleased]

### Security and stability (R0)
- OpenAI Agents SDK trace export is off unless `HUBZOID_OPENAI_TRACING=true`.
- Artifact download links are signed with a per-hub secret; links issued by
  earlier versions stop working. The public default bridge key `dev` is refused
  on `/artifacts`. Optional expiry: `HUBZOID_ARTIFACT_LINK_TTL`.
- Open WebUI branding is kept unless the hub has files in `branding/`. Admins
  can no longer open or export other users' chats (`ENABLE_ADMIN_CHAT_ACCESS`,
  `ENABLE_ADMIN_EXPORT` to allow).
- Workflow agent calls are not retried unless `agent_max_attempts` is set; a
  failed agent run fails the workflow run.
- DBOS 3.1. Runs are tied to the workflow code version; runs from other code
  are cancelled at start instead of blocking the queue.
- Dependencies bounded and locked (`requirements.lock`); Docker image built from
  source with CPU-only PyTorch, publishing only port 3080.
