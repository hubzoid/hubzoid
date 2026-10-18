"""``hubzoid chat conversations|operations|branches|view|runs|queue|deletion``.

Local mode: the same operation service as ``/api/conversations/...``, run
against the hub's operational store as one account (``--user EMAIL``, default
the local owner). A command that starts a reply runs it in this process with
the hub's runtime and waits for it to finish (Ctrl-C stops it, keeping the
partial answer). Exit codes follow the conventions: 0 done, 2 invalid, 3
denied, 4 conflict, 6 accepted and still running.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import typer

from ..control import cli_common as cc
from . import graph as g

HUB = typer.Argument(Path("."), help="Hub folder. Default: current folder.")
JSON = typer.Option(False, "--json", help="Print the API's JSON object.")
USER = typer.Option(None, "--user", help="Act as this account (email). Default: the local owner.")
KEY = typer.Option(None, "--idempotency-key", help="Reuse to retry the same request safely.")

conversations_app = typer.Typer(help="Conversations: create, read, keep, delete.", no_args_is_help=True)
operations_app = typer.Typer(help="Submit chat operations and read their receipts.", no_args_is_help=True)
branches_app = typer.Typer(help="Branches of a conversation.", no_args_is_help=True)
view_app = typer.Typer(help="Your selected branch and head.", no_args_is_help=True)
runs_app = typer.Typer(help="Replies being written.", no_args_is_help=True)
queue_app = typer.Typer(help="Queued follow-ups of a branch.", no_args_is_help=True)
deletion_app = typer.Typer(help="Preview what a deletion removes.", no_args_is_help=True)


def register(chat_app: typer.Typer) -> None:
    for name, sub in (("conversations", conversations_app), ("operations", operations_app),
                      ("branches", branches_app), ("view", view_app), ("runs", runs_app),
                      ("queue", queue_app), ("deletion", deletion_app)):
        chat_app.add_typer(sub, name=name)


class _Local:
    """An in-process chat context for one hub."""

    def __init__(self, hub: Path, as_json: bool):
        from .. import settings as settingslib
        from . import store as store_mod
        from .common import ChatContext
        from .engine import ChatEngine

        hub = Path(hub).resolve()
        if not (hub / "AGENTS.md").is_file():
            cc.fail(400, "invalid_request", f"{hub} is not a hub folder.", as_json)
        self.hub = hub
        self.as_json = as_json
        self.settings = settingslib.load(hub)
        self.ctx = ChatContext(hub_dir=hub, runtime=None, inflight=None, settings=self.settings,
                               model_label=self.settings.model_label if hasattr(self.settings, "model_label")
                               else hub.name, store=store_mod.for_hub(hub))
        from .routes import _message_parts
        from .store import chat_key

        def validate(conv, content):
            from fastapi import HTTPException

            try:
                return _message_parts(self.ctx, chat_key(conv), {"content": content})
            except HTTPException as exc:
                raise g.GraphError(exc.status_code, exc.detail["code"], exc.detail["message"])

        self.engine = ChatEngine(hub, self.ctx.store, self.ctx.hub, part_validator=validate)
        self.ctx.engine = self.engine

    def actor(self, email: str | None):
        from .. import auth
        from ..auth import users
        from .engine import Actor

        if email:
            u = users.find_by_email(self.hub, email)
            if u is None:
                cc.fail(404, "not_found", f"No account {email!r} in this deployment.", self.as_json)
            return Actor(id=u["id"], email=u["email"].lower(), name=u.get("name") or "")
        owner = auth.local_owner(self.hub)
        return Actor(id=owner.id, email=owner.email.lower(), name=owner.name or "")

    def agent(self) -> str:
        from .. import runtime as runtime_lib

        rt = runtime_lib.build(self.hub)
        self.ctx.runtime = rt
        from ..server import _slugify

        self.ctx.model_label = self.settings.model_label or _slugify(rt.name)
        return self.ctx.model_label

    def run_plan(self, plan) -> dict | None:
        """Launch an admitted reply here and wait for it (and anything the
        queue sends next on that branch)."""
        if plan is None:
            return None
        from .engine_routes import EventBus, Orchestrator
        from .runs import RunManager

        if self.ctx.runtime is None:
            self.agent()
        self.ctx.runs = RunManager(self.ctx)

        async def main():
            orch = Orchestrator(self.ctx, self.engine, EventBus())
            self.ctx.orchestrator = orch
            run = await orch.launch(plan)
            while True:
                active = [r for r in self.ctx.runs.active_runs()]
                if not active:
                    break
                await asyncio.sleep(0.2)
            await asyncio.sleep(0.3)  # let the end of the run be recorded
            return run

        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
        return self.engine.run(plan.run_id, self.actor_obj)

    def guard(self):
        local = self

        class _G:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                if isinstance(exc, g.GraphError):
                    cc.fail(exc.status, exc.code, exc.message, local.as_json, **exc.extra)
                from fastapi import HTTPException

                if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
                    cc.fail(exc.status_code, exc.detail.get("code", "error"), exc.detail.get("message", ""),
                            local.as_json, **{k: v for k, v in exc.detail.items() if k not in ("code", "message")})
                return False

        return _G()


def _local(hub: Path, as_json: bool, user: str | None) -> _Local:
    loc = _Local(hub, as_json)
    loc.actor_obj = loc.actor(user)
    return loc


def _read_file(path: Path, as_json: bool) -> dict:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        cc.fail(400, "invalid_request", f"{path}: {exc}", as_json)
    if not isinstance(data, dict):
        cc.fail(400, "invalid_request", f"{path} holds a JSON object.", as_json)
    return data


# -- conversations ---------------------------------------------------------------------
@conversations_app.command("create")
def conv_create(hub: Path = HUB, temporary: bool = typer.Option(False, "--temporary"), user: str = USER,
                key: str = KEY, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        out = loc.engine.create_conversation(loc.actor_obj, agent=loc.agent(), temporary=temporary,
                                             key=key or str(uuid.uuid4()))
    cc.emit(out, as_json)


@conversations_app.command("get")
def conv_get(conversation_id: str, hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(loc.engine.snapshot(conversation_id, loc.actor_obj), as_json)


@conversations_app.command("save")
def conv_save(conversation_id: str, hub: Path = HUB, expected_revision: int = typer.Option(..., "--expected-revision"),
              user: str = USER, key: str = KEY, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(loc.engine.save_conversation(conversation_id, loc.actor_obj, expected_revision=expected_revision,
                                             key=key or str(uuid.uuid4())), as_json)


@conversations_app.command("delete")
def conv_delete(conversation_id: str, hub: Path = HUB,
                expected_revision: int = typer.Option(..., "--expected-revision"), user: str = USER,
                key: str = KEY, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        out = loc.engine.delete_conversation(conversation_id, loc.actor_obj, expected_revision=expected_revision,
                                             key=key or str(uuid.uuid4()))
    from .store import remove_chat_files

    remove_chat_files(loc.hub, out["chat_key"])
    cc.emit(out, as_json)


# -- operations ------------------------------------------------------------------------
@operations_app.command("submit")
def op_submit(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"),
              file: Path = typer.Option(..., "--file", help="The operation as JSON (kind, branch_id, ...)."),
              user: str = USER, key: str = KEY, as_json: bool = JSON) -> None:
    """Run send, respond, revise, regenerate, continue, delete, fork or synthesize.
    A reply is written before the command returns (the CLI has no background
    worker; queue a message through a running hub to have it sent later)."""
    loc = _local(hub, as_json, user)
    body = _read_file(file, as_json)
    with loc.guard():
        out, plan = loc.engine.submit(conversation, loc.actor_obj, body, key=key or str(uuid.uuid4()))
    result = dict(out.body)
    if out.replayed:
        result["replayed"] = True
    if plan is not None:
        with loc.guard():
            result["run"] = loc.run_plan(plan)
        cc.emit(result, as_json)
        return
    cc.emit(result, as_json)
    if out.status == 202:
        raise typer.Exit(cc.EXIT_PENDING)


@operations_app.command("get")
def op_get(operation_id: str, hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(loc.engine.operation(operation_id, loc.actor_obj), as_json)


# -- branches and view -----------------------------------------------------------------
@branches_app.command("list")
def br_list(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"), user: str = USER,
            as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit({"items": loc.engine.branches(conversation, loc.actor_obj)}, as_json)


@branches_app.command("create")
def br_create(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"),
              from_message: str = typer.Option(None, "--from"), user: str = USER, key: str = KEY,
              as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(loc.engine.create_branch(conversation, loc.actor_obj, from_message_id=from_message,
                                         key=key or str(uuid.uuid4())), as_json)


@view_app.command("set")
def view_set(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"),
             branch: str = typer.Option(None, "--branch"), head: str = typer.Option(None, "--head"),
             expected_revision: int = typer.Option(None, "--expected-revision"), user: str = USER,
             as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(loc.engine.set_view(conversation, loc.actor_obj, branch_id=branch, head_id=head,
                                    expected_revision=expected_revision), as_json)


# -- runs ---------------------------------------------------------------------------------
@runs_app.command("get")
def runs_get(run_id: str, hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        out = loc.engine.run(run_id, loc.actor_obj)
    cc.emit(out, as_json)
    if out["status"] in g.ACTIVE_RUN:
        raise typer.Exit(cc.EXIT_PENDING)


@runs_app.command("watch")
def runs_watch(run_id: str, hub: Path = HUB, user: str = USER, interval: float = 0.5,
               as_json: bool = JSON) -> None:
    """Wait for a run to end and print it (any process may be writing it)."""
    import time

    loc = _local(hub, as_json, user)
    with loc.guard():
        while True:
            out = loc.engine.run(run_id, loc.actor_obj)
            if out["status"] not in g.ACTIVE_RUN:
                break
            time.sleep(interval)
    cc.emit(out, as_json)


@runs_app.command("cancel")
def runs_cancel(run_id: str, hub: Path = HUB, keep_queue: bool = typer.Option(False, "--keep-queue"),
                user: str = USER, as_json: bool = JSON) -> None:
    """Record a stop: the branch's queue is parked; the process writing the run stops it."""
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(loc.engine.request_cancel(run_id, loc.actor_obj, park_queue=not keep_queue), as_json)


# -- queue --------------------------------------------------------------------------------
@queue_app.command("list")
def q_list(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"), user: str = USER,
           as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit({"items": loc.engine.queue_list(conversation, loc.actor_obj)}, as_json)


@queue_app.command("add")
def q_add(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"),
          branch: str = typer.Option(..., "--branch"), text: str = typer.Option(..., "--text"),
          lane: str = typer.Option("queue", "--lane"), after: str = typer.Option(None, "--after"),
          user: str = USER, key: str = KEY, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        out, plan = loc.engine.enqueue(conversation, loc.actor_obj, {
            "branch_id": branch, "content": text, "lane": lane, "predecessor_id": after},
            key=key or str(uuid.uuid4()))
        result = dict(out.body)
        if plan is not None:
            result["run"] = loc.run_plan(plan)
    cc.emit(result, as_json)


def _patch(action: str):
    def cmd(item_id: str, hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"),
            expected_revision: int = typer.Option(..., "--expected-revision"),
            text: str = typer.Option(None, "--text"), lane: str = typer.Option(None, "--lane"),
            before: str = typer.Option(None, "--before"), user: str = USER, as_json: bool = JSON) -> None:
        loc = _local(hub, as_json, user)
        body = {"action": action, "expected_revision": expected_revision}
        if text is not None:
            body["content"] = text
        if lane is not None:
            body["lane"] = lane
        if before is not None:
            body["before_id"] = before
        with loc.guard():
            cc.emit(loc.engine.queue_patch(conversation, loc.actor_obj, item_id, body), as_json)

    cmd.__doc__ = f"{action.capitalize()} a queued message."
    return cmd


for _action in ("edit", "move", "remove", "rebase"):
    queue_app.command(_action)(_patch(_action))


def _control(action: str):
    def cmd(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"),
            branch: str = typer.Option(..., "--branch"),
            expected_revision: int = typer.Option(None, "--expected-revision"), user: str = USER,
            as_json: bool = JSON) -> None:
        loc = _local(hub, as_json, user)
        with loc.guard():
            branch_out, plan = loc.engine.queue_control(conversation, loc.actor_obj, {
                "branch_id": branch, "action": action, "expected_revision": expected_revision})
            result = {"branch": branch_out}
            if plan is not None:
                result["run"] = loc.run_plan(plan)
        cc.emit(result, as_json)

    cmd.__doc__ = f"{action.capitalize()} the branch's queue."
    return cmd


for _action in ("pause", "resume", "clear"):
    queue_app.command(_action)(_control(_action))


@deletion_app.command("preview")
def del_preview(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"),
                message: str = typer.Option(..., "--message"),
                expected_revision: int = typer.Option(None, "--expected-revision"), user: str = USER,
                as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(loc.engine.deletion_preview(conversation, loc.actor_obj, message_id=message,
                                            expected_revision=expected_revision), as_json)

