"""Codex app-server adapter. Only Hubzoid executes tools; no native environment.

The experimental protocol is deliberately version-pinned: an unreviewed CLI must
not silently re-enable native tools. Every request uses an ephemeral thread and
an isolated configuration directory, with only the operator's login copied in.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from contextvars import ContextVar
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile

from . import __version__, _request_ctx, tool_events

log = logging.getLogger(__name__)
SUPPORTED_CODEX_VERSION = "0.147.0"
_usage_rollup: ContextVar[list | None] = ContextVar("codex_usage_rollup", default=None)
# In addition to environments=[], turn off every optional native tool source.
_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apply_patch_freeform", "view_image", "apps",
    "connectors", "plugins", "plugin_hooks", "hooks", "codex_hooks", "browser_use",
    "browser_use_external", "computer_use", "in_app_browser", "image_generation",
    "imagegenext", "js_repl", "code_mode", "code_mode_only",
    "multi_agent", "multi_agent_v2", "collab", "memories", "memory_tool",
    "chronicle", "goals", "token_budget", "current_time_reminder", "deferred_executor",
    "tool_search", "tool_suggest", "recommended_plugins", "skill_search",
    "skill_mcp_dependency_install", "workspace_dependencies", "remote_control",
    "external_migration", "external_agent_memory_import", "request_permissions_tool",
    "default_mode_request_user_input", "realtime_conversation",
)


def codex_binary() -> str:
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Codex CLI is not installed. Install @openai/codex, then run codex login.")
    result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
    if result.returncode or result.stdout.strip() != f"codex-cli {SUPPORTED_CODEX_VERSION}":
        raise RuntimeError(f"codex-local requires Codex CLI {SUPPORTED_CODEX_VERSION}; install @openai/codex@{SUPPORTED_CODEX_VERSION}. Other versions need a tool-isolation review.")
    return binary


def _auth_file() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"


def codex_available() -> bool:
    """Read-only detection: never starts a login flow or makes a model call."""
    try:
        binary = codex_binary()
        if not _auth_file().is_file():
            return False
        return subprocess.run([binary, "login", "status"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return False


class CodexRuntime:
    def __init__(self, *, name, instructions, registry, model_setting="codex-local",
                 hub_dir=None, max_turns=20, tool_mode="compact", mcp_servers=None,
                 vision=(True, 1568, 4), effort=None, personal_mcp=False):
        self.name = name
        self.instructions = instructions
        self.registry = registry
        self.model = model_setting.partition("/")[2] or None
        self.hub_dir = hub_dir
        self.max_turns = max_turns or 20
        self.tool_mode = tool_mode
        self.vision = vision
        self.effort = effort
        # Only the hub's main agent carries the caller's personal MCP servers,
        # as on the Claude and OpenAI backends (delegates never do).
        self.personal_mcp = personal_mcp
        self._servers = mcp_servers or []
        self._stack = None
        self._processes = set()
        # A bridge runtime serves concurrent users; error state must be task-local.
        self._error = ContextVar(f"codex_error_{id(self)}", default=None)

    @property
    def last_error(self):
        return self._error.get()

    async def aopen(self):
        if self._stack is not None:
            return
        from agents import Agent, RunContextWrapper
        from agents.mcp.util import MCPUtil
        self._stack = AsyncExitStack()
        try:
            for server in self._servers:
                await self._stack.enter_async_context(server)
                tools = await MCPUtil.get_function_tools(
                    server, False, RunContextWrapper(context=None), Agent(name=self.name))
                for tool in tools:
                    if tool.name in self.registry:
                        raise RuntimeError(f"Duplicate MCP tool name: {tool.name}")
                    self.registry[tool.name] = tool
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self):
        for proc in tuple(self._processes):
            await _stop(proc)
        if self._stack:
            await self._stack.aclose()
            self._stack = None

    async def run(self, prompt):
        return "".join([part async for part in self.stream(prompt)])

    async def stream(self, prompt):
        """Run one turn. When the caller connected personal MCP servers in Open
        WebUI (see `owui_mcp`), their tools join a per-turn copy of the
        registry for this turn only. The shared registry never changes."""
        personal = self._personal_servers()
        if not personal:
            async for part in self._stream(prompt, self.registry):
                yield part
            return
        from .runtime import relay_in_task

        self._error.set(None)
        outcome = {}

        async def turn():
            async for part in self._stream_personal(prompt, personal):
                yield part
            outcome["error"] = self._error.get()

        async for part in relay_in_task(turn):
            yield part
        # The turn ran in its own task; carry its error state back to ours.
        self._error.set(outcome.get("error"))

    def _personal_servers(self):
        if not (self.personal_mcp and self.hub_dir):
            return []
        from . import owui_mcp
        from .access.identity import current_identity
        try:
            return owui_mcp.per_user_servers(self.hub_dir, current_identity())
        except Exception:  # noqa: BLE001 — a DB/token hiccup must never break chat
            log.warning("owui-mcp per-user injection skipped", exc_info=True)
            return []

    async def personal_registry(self, stack, personal):
        """A copy of the shared registry plus the caller's personal MCP tools,
        connected on `stack`. A server whose tool name is already taken is
        skipped for this turn (see `runtime.open_personal_mcp`)."""
        from .runtime import open_personal_mcp

        registry = dict(self.registry)
        for _server, tools in await open_personal_mcp(stack, personal, set(registry)):
            for tool in tools:
                registry[tool.name] = tool
        return registry

    async def _stream_personal(self, prompt, personal):
        async with AsyncExitStack() as stack:
            registry = await self.personal_registry(stack, personal)
            async for part in self._stream(prompt, registry):
                yield part

    async def _stream(self, prompt, registry):
        self._error.set(None)
        # Only a per-turn registry (personal MCP tools) is passed down; the
        # shared path calls the exchange exactly as before.
        turn_tools = {} if registry is self.registry else {"registry": registry}
        proc = None
        shown = []
        usage = {}
        parent_usage = _usage_rollup.get()
        records = parent_usage if parent_usage is not None else []
        rollup_token = _usage_rollup.set(records)
        try:
            binary = await asyncio.to_thread(codex_binary)
            auth = _auth_file()
            if not auth.is_file():
                raise RuntimeError("Codex file-backed login is required. Run codex -c 'cli_auth_credentials_store=\"file\"' login as the Hubzoid service user.")
            with tempfile.TemporaryDirectory(prefix="hubzoid-codex-") as tmp:
                root = Path(tmp)
                config_home = root / "config"
                config_home.mkdir(mode=0o700)
                initial_auth = auth.read_bytes()
                (config_home / "auth.json").write_bytes(initial_auth)
                (config_home / "auth.json").chmod(0o600)
                # Do not inherit provider keys, personal config, MCPs, hooks or skills.
                env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LANG", "TMPDIR", "SYSTEMROOT") if k in os.environ}
                env["CODEX_HOME"] = str(config_home)
                config = {
                    # Some models require code mode. Its V8 host has no imports,
                    # filesystem, network or shell; nested calls still use our registry.
                    "features": {**{name: False for name in _DISABLED_FEATURES}, "code_mode_host": True},
                    "web_search": "disabled", "project_doc_max_bytes": 0,
                    "orchestrator": {"skills": {"enabled": False}, "mcp": {"enabled": False}},
                    "skills": {"bundled": {"enabled": False}, "include_instructions": False},
                    "tools": {"update_plan": {"enabled": False}, "experimental_request_user_input": {"enabled": False}},
                    "analytics": {"enabled": False}, "check_for_update_on_startup": False,
                    "cli_auth_credentials_store": "file",
                }
                # Pass only our isolated configuration (nested values use dotted keys).
                args = [binary, "app-server", "--listen", "stdio://"]
                for key, value in _flatten(config):
                    args += ["-c", f"{key}={json.dumps(value)}"]
                proc = await asyncio.create_subprocess_exec(
                    *args, cwd=tmp, env=env, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True, limit=8 * 1024 * 1024)
                self._processes.add(proc)
                try:
                    async with asyncio.timeout(300):
                        async for part in self._exchange(proc, prompt, tmp, usage, **turn_tools):
                            shown.append(part)
                            yield part
                finally:
                    await _stop(proc)
                    self._processes.discard(proc)
                    _persist_refreshed_auth(auth, config_home / "auth.json", initial_auth)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error.set(exc)
            log.warning("Codex request failed (%s)", type(exc).__name__)
            yield f"\n\n[Codex could not complete this request: {exc}]"
        finally:
            if usage:
                records.append(usage)
            _usage_rollup.reset(rollup_token)
            if parent_usage is None and records:
                _request_ctx.record_usage(_combine_usage(records))
        if parent_usage is not None:
            return  # The outer request owns artifact delivery and usage accounting.
        footer = tool_events.format_artifact_footer(_request_ctx.drain_artifacts(), "".join(shown))
        if footer:
            yield footer

    async def _exchange(self, proc, prompt, cwd, usage, registry=None):
        registry = self.registry if registry is None else registry

        async def send(message):
            proc.stdin.write((json.dumps(message) + "\n").encode())
            await proc.stdin.drain()

        async def read():
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("Codex stopped unexpectedly. Check codex login status and available usage.")
            return json.loads(line)

        async def request(id_, method, params):
            await send({"id": id_, "method": method, "params": params})
            while True:
                msg = await read()
                if msg.get("id") == id_ and "method" not in msg:
                    if "error" in msg:
                        # Raw protocol errors can echo config or prompt contents.
                        raise RuntimeError(f"Codex rejected {method}. Check CLI version, login and model availability.")
                    return msg["result"]
                if "id" in msg and "method" in msg:
                    await send({"id": msg["id"], "error": {"code": -32601, "message": "Unsupported by Hubzoid"}})

        await request(1, "initialize", {"clientInfo": {"name": "hubzoid", "version": __version__}, "capabilities": {"experimentalApi": True}})
        await send({"method": "initialized", "params": {}})
        params = {"cwd": cwd, "ephemeral": True, "environments": [],
                  "selectedCapabilityRoots": [], "runtimeWorkspaceRoots": [],
                  "approvalPolicy": "never", "sandbox": "read-only",
                  "baseInstructions": self.instructions, "developerInstructions": "",
                  "dynamicTools": [{"type": "function", "name": t.name,
                    "description": t.description, "inputSchema": t.params_json_schema}
                    for t in registry.values()], "allowProviderModelFallback": False}
        if self.model:
            params["model"] = self.model
        started = await request(2, "thread/start", params)
        thread = started["thread"]["id"]
        usage["model"] = started.get("model")
        from .access.identity import current_identity
        ident = current_identity()
        identity = f"[Session context] Caller: {ident.user or 'automated run'}; surface: {ident.surface}. The Codex login is infrastructure, not the end user.\n\n"
        inputs = [{"type": "text", "text": identity + prompt, "text_elements": []}]
        if self.hub_dir:
            from .vision_inject import openai_input
            data = openai_input(prompt, self.hub_dir, _request_ctx.get_chat_id(), enabled=self.vision[0], max_edge=self.vision[1], max_images=self.vision[2])
            if isinstance(data, list):
                inputs.extend({"type": "image", "url": b["image_url"]} for b in data[0]["content"] if b["type"] == "input_image")
        turn_params = {"threadId": thread, "input": inputs}
        if self.effort:
            turn_params["effort"] = self.effort
        # Events can precede the turn/start response, so consume both in one loop.
        await send({"id": 3, "method": "turn/start", "params": turn_params})
        calls = 0
        saw_delta = set()
        while True:
            msg = await read()
            method = msg.get("method")
            p = msg.get("params", {})
            if "error" in msg and msg.get("id") == 3:
                raise RuntimeError("Codex could not start the turn. Check login, model availability and usage limits.")
            if p.get("threadId", thread) != thread:
                raise RuntimeError("Codex returned an unexpected thread identity.")
            if method == "item/tool/call":
                calls += 1
                if calls > self.max_turns:
                    raise RuntimeError("Codex reached the configured tool-call limit.")
                name, arguments = p.get("tool"), p.get("arguments", {})
                tool = registry.get(name) if not p.get("namespace") else None
                if tool is None:
                    result, success = "Tool is not available in this hub.", False
                else:
                    from agents import RunConfig
                    from agents.tool_context import ToolContext
                    args_json = json.dumps(arguments)
                    ctx = ToolContext(context=None, tool_name=name, tool_call_id=p["callId"], tool_arguments=args_json, run_config=RunConfig())
                    _request_ctx.record_tool_call(name, arguments)
                    display = tool_events.format_call(name, arguments, mode=self.tool_mode)
                    if display:
                        yield display
                    try:
                        result = await tool.on_invoke_tool(ctx, args_json)
                        success = True
                    except Exception:
                        result, success = "Tool failed. Check the hub server logs.", False
                        log.exception("Codex tool %s failed", name)
                if not isinstance(result, str):
                    result = json.dumps(result, default=str)
                await send({"id": msg["id"], "result": {"contentItems": [{"type": "inputText", "text": result}], "success": success}})
            elif "id" in msg and method:
                # No native approvals, elicitation or unconfigured callbacks.
                await send({"id": msg["id"], "error": {"code": -32601, "message": "Unsupported by Hubzoid"}})
            elif method == "item/agentMessage/delta":
                saw_delta.add(p.get("itemId"))
                yield p.get("delta", "")
            elif method == "item/completed":
                item = p.get("item", {})
                if item.get("type") == "agentMessage" and item.get("id") not in saw_delta:
                    yield item.get("text", "")
            elif method == "thread/tokenUsage/updated":
                total = p.get("tokenUsage", {}).get("total", {})
                usage.update(input_tokens=total.get("inputTokens", 0), output_tokens=total.get("outputTokens", 0))
            elif method == "turn/completed":
                if p.get("turn", {}).get("status") != "completed":
                    raise RuntimeError("Codex turn failed or was interrupted. Check login, model availability and usage limits.")
                break


def _combine_usage(records):
    from .usage import estimate_cost
    models = {row.get("model") for row in records}
    costs = [estimate_cost(row.get("model"), row.get("input_tokens"), row.get("output_tokens")) for row in records]
    return {
        "model": next(iter(models)) if len(models) == 1 else None,
        "input_tokens": sum(row.get("input_tokens", 0) for row in records),
        "output_tokens": sum(row.get("output_tokens", 0) for row in records),
        "cost_usd": sum(costs) if all(cost is not None for cost in costs) else None,
    }


def _persist_refreshed_auth(original: Path, temporary: Path, initial: bytes) -> None:
    """Retain a CLI token refresh without overwriting a concurrent operator login.

    This is the operator's chosen Codex credential store, never a hub credential.
    File contents are never logged or sent to the model.
    """
    import fcntl
    try:
        updated = temporary.read_bytes()
        if updated == initial:
            return
        with (original.parent / ".hubzoid-auth-refresh.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if original.read_bytes() != initial:
                return
            fd, name = tempfile.mkstemp(prefix=".hubzoid-auth-", dir=original.parent)
            try:
                with os.fdopen(fd, "wb") as dest:
                    dest.write(updated)
                os.replace(name, original)
            finally:
                Path(name).unlink(missing_ok=True)
    except OSError:
        log.warning("Codex refreshed its login but could not save it; run codex login again before credentials expire.")


def _flatten(values, prefix=""):
    for key, value in values.items():
        key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from _flatten(value, key)
        else:
            yield key, value


async def _stop(proc):
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), timeout=5)
    except ProcessLookupError:
        pass
    except asyncio.TimeoutError:
        os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()


def build_codex_runtime(hub_dir, *, extra_tools=None, max_turns=None, model_override=None):
    from . import access, connections, memory, settings
    from .factory import HubContext, _compose_instructions, _load_skills_and_delegates, _with_core_skills, _add_curator_tool
    from .loaders import agents, knowledge, tools_local, mcp
    from .tools import make_all
    hub_dir = Path(hub_dir).resolve()
    config = settings.load(hub_dir)
    main = agents.load_main(hub_dir)
    model = model_override or config.model or main.spec.model or "codex-local"
    skills, delegates = _load_skills_and_delegates(hub_dir, model)
    session = memory.make_session_id()
    ctx = HubContext(hub_dir=hub_dir, output_dir=memory.session_output_dir(hub_dir, session), session_id=session,
                     settings=config, skills=_with_core_skills(skills), knowledge=knowledge.load_all(hub_dir), delegates=delegates)
    connections.attach(ctx)
    registry = access.apply(hub_dir, {**make_all(ctx), **tools_local.load_all(hub_dir), **(extra_tools or {})})
    _add_curator_tool(ctx, registry, access)
    from . import handover
    base_registry = dict(registry)
    for delegate in delegates:
        scoped = {n: base_registry[n] for n in handover.scoped_tool_names(delegate.spec.tools, list(base_registry))}
        from agents import FunctionTool
        async def invoke(_ctx, args, loaded=delegate, tools=scoped):
            child = CodexRuntime(name=loaded.spec.name, instructions=loaded.instructions,
                                 registry=tools, model_setting=loaded.spec.model,
                                 hub_dir=hub_dir, max_turns=max_turns, tool_mode="off")
            result = await child.run(json.loads(args)["task"])
            if child.last_error:
                raise child.last_error
            return result
        registry[handover.tool_name(delegate.spec.name)] = FunctionTool(
            name=handover.tool_name(delegate.spec.name), description=delegate.spec.description,
            params_json_schema={"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"], "additionalProperties": False}, on_invoke_tool=invoke)
    return CodexRuntime(name=main.spec.name, instructions=_compose_instructions(main.instructions, ctx, backend="codex-local"),
                        registry=registry, model_setting=model, hub_dir=hub_dir,
                        max_turns=max_turns, tool_mode=config.show_tools, mcp_servers=mcp.load_all(hub_dir),
                        vision=(config.vision_enabled, config.vision_max_edge, config.vision_max_images), effort=config.reasoning_effort,
                        personal_mcp=True)
