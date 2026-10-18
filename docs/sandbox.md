# Sandboxes

A sandbox is an isolated workspace where an agent runs commands, Python, tests,
search and git, and reads and edits files. It runs on Docker on your laptop or
server. Each sandbox belongs to one agent call, has CPU, memory and process
limits, and is removed when the call is done. Your hub's files, outputs and
history are never touched by sandbox cleanup.

Nothing secret goes into a sandbox: no API key, token or Git credential. A
repository is cloned on the host with the host's own Git access and copied in
without it.

## How it runs

1. **Setup.** Hubzoid creates the container and copies in any repository
   checkout. If the profile has a `setup` command, Hubzoid runs it once. That
   command comes from the reviewed profile file, never from a model. It can
   reach only the package registries the profile allows, through a proxy
   created for this sandbox alone. The sandbox has no other route and no DNS.
2. **Offline.** Hubzoid then stops the container, so every setup process ends,
   removes all its network, starts it again and checks that it is offline.
3. **Agent work.** Only now does the agent get its tools. Its commands run as
   an unprivileged user, offline.
4. **Done.** When the call completes, any new commits in a checkout are
   exported as a patch into the hub's `output/`, and the sandbox is deleted.

If Hubzoid restarts, the same sandbox is found again with its files. A sandbox
that disappeared (deleted by hand, or the Docker host was reset) is reported as
**lost** and never recreated empty. The call stops with `sandbox_lost`; an
explicit retry starts a fresh sandbox from the same checkout.

## Turn it on

Install Docker and the extra:

```bash
pip install 'hubzoid[sandbox]'
```

Add `sandbox.yaml` to the hub folder (the deployment folder in a gateway):

```yaml
version: 1
sandbox:
  enabled: true
  default_provider: local-docker
  providers:
    local-docker:
      kind: docker            # endpoint: unix:///path or ssh://user@host (optional)
  limits:
    max_active_per_hub: 16
    max_probe_seconds: 120
  lifecycle_cleanup:
    terminal_seconds: 0        # keep a finished sandbox this long before removal
  reaper:
    interval_seconds: 300
    orphan_grace_seconds: 600
```

Add a profile, `sandboxes/analysis.yaml`:

```yaml
version: 1
description: Python and Node checkout for analysis
image:
  base: hubzoid-default        # or ref: name@sha256:... (pinned by digest)
resources:
  cpus: 2                      # 0.5 to 16
  memory_mib: 4096             # no swap
  pids: 1024
lifecycle:
  idle_stop_seconds: 900
  max_lifetime_seconds: 86400
  max_job_seconds: 3600        # longest a single command may run
  command_timeout_seconds: 120 # then it continues in the background
network:
  setup: {mode: allowlist, presets: [registries-v1], allow_hosts: []}
  agent: {mode: deny_all}
setup:
  command: "uv sync --frozen && npm ci"
  cwd: repo
  timeout_seconds: 900
env:
  PYTHONDONTWRITEBYTECODE: "1"
```

`registries-v1` is npm, Yarn, PyPI, Go, crates.io, RubyGems and Maven Central.
Agent commands are always offline in this version: `network.agent` accepts
only `deny_all`. `env` refuses anything that looks like a credential or a
proxy setting (`secret_in_profile`).

Give an isolated agent the profile and the tools it needs:

```markdown
---
name: coder
isolated: true
engine: claude-local
sandbox: analysis
tools: [exec_command, write_stdin, read_file, write_file, edit_file, glob, grep]
response_schema: schemas/result.json
---
The repository is at /workspace/repo. Make the change, run the tests, commit.
```

And call it from a work-item workflow, with an optional checkout:

```python
hub.call_agent("Fix the failing test.", agent="coder", stage="code",
               checkout={"url": "https://github.com/acme/app.git", "ref": "main"})
```

## The tools

The same names, inputs and outputs on Claude and Codex.

| Tool | What it does |
|---|---|
| `exec_command` | Runs a shell command in the sandbox. A command still running after `command_timeout_seconds` keeps going and returns a session id. |
| `write_stdin` | Sends input to, or reads more output from, such a session. |
| `read_file` | A text file with line numbers. Needed before changing a file. |
| `write_file` | Creates a file, or replaces one the agent has read and that has not changed since. |
| `edit_file` | Replaces an exact string, once unless `replace_all`. |
| `glob`, `grep` | Find files and search their contents (ripgrep). |

`exec_command` and `write_stdin` are the OpenAI Agents SDK's own sandbox shell
tools. File paths are inside `/workspace`, checked against their real path
inside the sandbox. Output is scrubbed of credentials before the agent sees it.
An agent with a sandbox that also needs the hub's files lists `read_hub_file`
and `list_hub_files`.

## Check, try and clean up

```bash
hubzoid sandbox doctor ./hub
hubzoid sandbox profiles validate ./hub
hubzoid sandbox profiles prepare ./hub analysis
hubzoid sandbox try ./hub --profile analysis -- python3 -V
hubzoid sandbox try ./hub --profile analysis -- curl -sS https://example.com   # fails: offline
hubzoid sandbox list ./hub
hubzoid sandbox show ./hub SANDBOX
hubzoid sandbox stop ./hub SANDBOX --reason "…"
hubzoid sandbox delete ./hub SANDBOX --reason "…"
hubzoid sandbox reap ./hub            # preview; add --execute to do it
```

The Console's **Sandboxes** page shows the same: health, each sandbox's owner,
profile, state and network policy, pause and remove with a reason, and cleanup
preview. It never shows a sandbox's files or command output. The API is under
`/portal/api/hubs/{hub}/sandboxes`; reading needs `workflows_view`, changes and
probes need `workflows_manage`.

| Code | What to do |
|---|---|
| `docker_unavailable` | Start Docker, then run `hubzoid sandbox doctor`. |
| `profile_invalid`, `secret_in_profile`, `network_mode_unsupported` | Fix the file at the line shown. |
| `image_unavailable` | Run `hubzoid sandbox profiles prepare`. |
| `setup_failed` | Read the output tail, fix the setup command or lockfile. |
| `capacity_exceeded` | Wait, stop idle sandboxes or raise `max_active_per_hub`. |
| `sandbox_lost` | The sandbox disappeared; retry the call to start a fresh one. |

## Limits

- Docker only. Hosted sandbox providers come with hosted deployments.
- Disk size is not limited on Docker Desktop's default storage.
  `require_disk_limit: true` fails rather than run without it.
- Agent commands cannot reach package registries; install dependencies in the
  profile's `setup` command.
- A sandbox belongs to an isolated agent's call. The main chat agent does not
  get one yet.
