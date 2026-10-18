# Sandboxes for isolated agents

Status: implementation authorized by the owner on 18 October 2026, for 2.0.

## Problem

An isolated agent that works on code needs a place to check a repository out,
install its dependencies, run commands and tests, and edit files. Today an
agent either has no shell at all or would use the host's, which holds the
hub's credentials and network.

## What should happen

Keep it thin. The OpenAI Agents SDK already ships a sandbox layer
(`agents.sandbox`) with a Docker session, a JSON shell tool and path checks
inside the container. Hubzoid uses it as a library, for both installed
runtimes, and adds only what it lacks:

1. **Limits and labels.** Hubzoid creates each container itself: unprivileged
   user, all capabilities dropped, no new privileges, CPU, memory (no swap) and
   process limits, and labels for cleanup. No credential or host environment
   enters it.
2. **Two network phases.** A profile's reviewed setup command runs once with
   only an allowlist of package registries, through a proxy made for that
   sandbox. Then every setup process is ended, all network is removed and the
   result is checked before any agent tool runs. Agent commands are offline.
3. **Never recreated silently.** A record per sandbox lets a restart reattach
   it with its files. A sandbox that disappeared is reported lost.
4. **Files.** `sandbox.yaml` (providers and limits) and `sandboxes/<id>.yaml`
   profiles, validated with file and line. Keys that cannot be enforced yet are
   refused, not ignored.
5. **Tools.** The SDK's `exec_command` and `write_stdin`, plus small JSON file
   tools (`read_file`, `write_file`, `edit_file`, `glob`, `grep`) instead of
   the SDK's OpenAI-only patch tool, so Claude and Codex get identical tools.
6. **Repositories.** A station call may name a repository. It is cloned on the
   host with the host's Git access and copied in without it. New commits come
   back as a patch in the hub's output.
7. **Every action through files, CLI and API**, and a Console page.

## Scope and non-goals

- Docker only. Hosted providers come with hosted deployments.
- No registry access for agent commands, no main-chat sandbox, no image builds
  from a hub's own Dockerfile yet.
- Publishing code stays outside the sandbox.

## Definition of done

- No credential appears in a sandbox's environment, files or network path.
- A restart finds the same sandbox with its files. A deleted one is lost.
- Setup reaches only the allowed registries; agent commands run offline after
  every setup process has ended.
- A station with a sandbox works the same on Claude and Codex, and cleanup
  never removes hub work or outputs.
