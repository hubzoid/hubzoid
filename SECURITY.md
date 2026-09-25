# Security policy

## Reporting a vulnerability

Please report security problems privately, through GitHub's private
vulnerability reporting on this repository (the **Security** tab, then
**Report a vulnerability**). Do not open a public issue or discussion for them.

Include what you found, how to reproduce it, the Hubzoid version
(`hubzoid version`) and, if you can, the output of `hubzoid doctor <hub> --json`
with secrets removed. We will acknowledge the report, keep you informed while
we fix it, and credit you in the release notes if you wish.

## Supported versions

Fixes go into the latest release. Upgrade to it to receive them
([docs/UPGRADING.md](docs/UPGRADING.md)).

## How a deployment is protected

Knowing the boundaries helps you report the right thing:

- **The public port** is the edge. It serves the chat app and forwards only
  downloads, the Console (`/portal`), MCP (`/mcp`) and webhooks to a hub.
  It refuses `.` and `..` path segments and drops client-sent identity headers.
- **Bridges** listen on 127.0.0.1 and require a bridge key (`BRIDGE_API_KEYS`).
  Identity headers are trusted only on requests that carry that key.
- **Restricted tools** are checked in code at call time for the verified
  person or service identity, and every decision is logged. A call that cannot
  be logged does not run.
- **Download links** are signed per hub.
- **Public file reads**, in chat and MCP, exclude dotenv files (including
  nested files and variants), databases/sidecars, `restricted/`, and private
  runtime/credential directories such as `.hubzoid/`, `.openwebui-data/` and
  `.git/`. Search checks every candidate before reading, with or without
  ripgrep. Resolved symlink targets receive the same checks; SQLite headers
  also block renamed databases. Name-based knowledge/skill loading applies
  this boundary too.
- **Chat files**: the current chat's uploads and artifacts remain readable
  through their scoped tools; dotenv and database checks still apply there.
  Generic readers do not expose other chats' private runtime directories.
- **Templates** render in Jinja's sandbox without filesystem access.

These guards apply to the built-in tools. Hub-local Python, workflow code and
external integrations are trusted operator-installed code, not an OS sandbox;
their authors must enforce their own data access. Do not copy credentials into
public knowledge or raw-data text files. Restart running bridges/MCP servers
after upgrading so their tool registries load the fix.

`hubzoid doctor` flags the common misconfigurations: a missing or public bridge
key, chat sign-in off on an exposed port, and missing model credentials. See
[docs/access-management.md](docs/access-management.md) and
[docs/DEPLOYING.md](docs/DEPLOYING.md).
