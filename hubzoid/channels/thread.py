"""`hub.thread`: a workflow's handle on the channel thread its work came from.

The handle is bound to the work item's stored origin. A workflow can post to
that thread and read who asked, but it can never name another channel, thread
or recipient: there is no parameter for one.

    @workflow(work_items=True)
    def triage():
        hub.thread.post("Looking at it now.", key="started", kind="progress")
        ...
        hub.thread.post(summary, key="summary")
        return {"posted": True}    # the channel then adds no "Done" line
"""
from __future__ import annotations

from pathlib import Path

from ..work_items.errors import Parked
from . import config as config_lib
from . import replies, store
from .errors import ChannelError


KINDS = ("progress", "result", "notice", "question", "approval")
MAX_FILES = 5
MAX_FILE_BYTES = 10 * 1024 * 1024


def _files(hub_dir: Path, files) -> tuple[str, ...]:
    """The files a post may carry: ones this hub wrote under its output
    folder, at most five, each at most 10 MB. Anything else is refused, so a
    workflow can never send a key, a database or the hub's own files out."""
    from .. import datadir

    if not files:
        return ()
    if isinstance(files, (str, Path)):
        files = [files]
    files = list(files)
    if len(files) > MAX_FILES:
        raise ValueError(f"post at most {MAX_FILES} files")
    paths = []
    for f in files:
        path = Path(f) if Path(f).is_absolute() else datadir.resolve(hub_dir, f)
        _check_output_file(hub_dir, path, shown=str(f))
        paths.append(str(path.resolve()))
    return tuple(paths)


def _check_output_file(hub_dir: Path, path: Path, *, shown: str) -> Path:
    """`path` is a regular file under the hub's real output folder (not a
    symlink to elsewhere) and nowhere under its state folder."""
    from .. import datadir

    out = datadir.resolve(hub_dir, "output")
    if out.is_symlink():
        raise ValueError("this hub's output folder is a symlink: files are not posted from it")
    real = Path(path).resolve()
    if (not real.is_relative_to(out.resolve()) or real.is_relative_to(datadir.state(hub_dir).resolve())
            or not real.is_file()):
        raise ValueError(f"{shown} is not a file under this hub's output folder")
    if real.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"{real.name} is larger than 10 MB")
    return real


def read_posted_file(hub_dir: Path, path: str) -> tuple[str, bytes]:
    """Check a posted file again when it is sent and read it once: what is
    uploaded is what passed the check, at most 10 MB."""
    real = _check_output_file(Path(hub_dir), Path(path), shown=Path(path).name)
    with open(real, "rb") as fh:
        data = fh.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"{real.name} is larger than 10 MB")
    return real.name, data


class Thread:
    def __init__(self, hub_dir: Path, thread: dict, item: dict):
        self._hub_dir = Path(hub_dir)
        self._thread = thread
        self._item = item

    @property
    def id(self) -> str:
        return self._thread["id"]

    @property
    def channel(self) -> str:
        return self._thread["connection_id"]

    @property
    def provider(self) -> str:
        return (self._thread.get("origin_json") or {}).get("provider", "")

    @property
    def url(self) -> str:
        return (self._thread.get("origin_json") or {}).get("url", "")

    @property
    def requested_by(self) -> dict | None:
        """The verified provider account that asked (display data, not authority)."""
        return (self._item.get("origin_json") or {}).get("requested_by")

    @property
    def approvals_here(self) -> bool:
        """Whether the requester can settle a plan approval by replying in
        this thread (the channel file allows it and the provider verifies
        who wrote the reply). Otherwise approvals are decided in Hubzoid."""
        try:
            conn = config_lib.active(self._hub_dir, self.channel)
        except Exception:  # noqa: BLE001 - no live connection: decide in Hubzoid
            return False
        return bool(conn.adapter.approvals_in_thread(conn))

    @property
    def attachments(self) -> bool:
        """Whether files posted here are attached (otherwise left out)."""
        try:
            return bool(config_lib.active(self._hub_dir, self.channel).adapter.attachments)
        except Exception:  # noqa: BLE001
            return False

    def post(self, text: str, *, key: str, kind: str = "result", actions=None, files=None) -> list[dict]:
        """Post to this thread. `key` names this post within the work item
        (stage plus purpose, never an attempt id): posting the same key again
        sends nothing new. `kind` is progress, result, notice, question or
        approval (the work now waits for an approval). `files` are files this
        hub wrote under its output folder, attached where the provider can
        (`attachments`), else left out. A customer-visible reply names its
        action (`customer_reply.send`, `email.send`) so the hub's write mode
        applies."""
        if kind not in KINDS:
            raise ValueError("kind is " + ", ".join(KINDS))
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise ValueError("key names the post, 1 to 200 characters")
        paths = _files(self._hub_dir, files)
        conn = config_lib.active(self._hub_dir, self.channel)
        if kind == "progress" and (not conn.config.progress or conn.adapter.customer_facing):
            return []                   # customers get answers, never progress chatter
        send = getattr(conn.adapter, "send_action", None)
        if conn.adapter.customer_facing and send:
            # A message to someone outside always performs the adapter's send
            # action, whatever else the caller names, so its write mode applies.
            named = [actions] if isinstance(actions, str) else list(actions or [])
            actions = named + ([send] if send not in named else [])
        draft = getattr(conn.adapter, "draft", None)
        if kind == "result" and conn.adapter.feedback_target == "workflow":
            # The rating line is part of the text the approver sees and approves.
            from . import feedback

            footer = feedback.whatsapp_footer(conn, kind, self._recorded_text(kind, key))
            if footer:
                text = f"{text.rstrip()}\n\n{footer}"
        try:
            return replies.post(conn, self._thread, kind=kind, text=text, key=f"wf:{key}", actions=actions,
                                draft=draft, files=paths if conn.adapter.attachments else ())
        except Parked as parked:
            if parked.kind == "approval" and conn.adapter.customer_facing and self._unattended():
                self._stop_unapprovable(parked.operation_id, text)
            raise

    def posted(self, key: str, *, kind: str = "result") -> bool:
        """Whether this work item already recorded a post under `key` and
        `kind` (sent, or waiting). A workflow that changes how it posts can
        keep an earlier run's post as it was."""
        return self._recorded_text(kind, key) is not None

    def _recorded_text(self, kind: str, key: str) -> str | None:
        """The last part of a reply already recorded under ``key`` (waiting
        for approval, or sent), or None. A repeat keeps exactly that text, so
        a settings change never alters a reply someone reviewed."""
        from ..work_items import operations as ops
        from ..work_items import store as wstore
        from ..workflows import context

        try:
            ctx = context._ctx()
        except Exception:  # noqa: BLE001 - no run bound
            return None
        work = ctx.work or {}
        item = work.get("item")
        if item is None:
            return None
        target = {"channel": self.channel, "thread": self.id}

        def row(part: int):   # the same logical key operations.run gives this part
            return wstore.op_by_key(self._hub_dir, ops.logical_key(
                [ctx.hub, "item", item["id"], item["generation"], work.get("stage"), f"channel.{kind}", target,
                 f"wf:{key}:{part}"]))

        first = row(1)
        if first is None:
            return None
        args = ops.row_arguments(self._hub_dir, first)
        parts = int(args.get("parts") or 1)
        last = row(parts) if parts > 1 else first
        return (ops.row_arguments(self._hub_dir, last) if last else args).get("text") or ""

    def _unattended(self) -> bool:
        requester = self._item.get("requester") or ""
        return not requester or requester.startswith("workflow:")

    def _stop_unapprovable(self, op_id: str, text: str) -> None:
        """Unattended work (an admitted sender nobody released) has no person
        who could approve a reply. The operation milestone D just parked for
        approval is closed at once, the reply is kept on the intake item,
        unsent, and the work stops with the reason, so nothing waits for
        nobody (HUB-93). Released messages have a requester who approves, and
        draft mode never parks, so both go on as usual. This is the one place
        the decision is made, on the mode D itself chose."""
        from ..work_items import store as wstore
        from ..work_items.operations import OperationFailed

        wstore.update_op(self._hub_dir, op_id, expect_state=("awaiting_approval",), state="abandoned",
                         resolution_json={"decision": "abandon", "by": "system", "reason": "approval_unavailable",
                                          "at": wstore.now()})
        intake_id = (self._item.get("origin_json") or {}).get("intake_id")
        if intake_id:
            from ..intake import service as intake

            intake.keep_unsent_reply(self._hub_dir, intake_id, text, reason="approval_unavailable")
        raise OperationFailed(
            "approval_unavailable", "Nobody attended this work, so nobody can approve a reply to the sender. The "
            "reply was kept on the intake item and not sent. Use draft mode for this channel, or have a person "
            "release messages from this sender so they approve the replies.", op_id)


def for_current_run() -> Thread:
    from ..workflows import context

    ctx = context._ctx()
    item = (ctx.work or {}).get("item")
    origin = (item or {}).get("origin_json") or {}
    tid = origin.get("thread_id")
    if not item or origin.get("surface") != "channel" or not tid:
        raise ChannelError("no_thread", "This run did not come from a channel thread, so there is nowhere to "
                           "post. Use hub.thread only in work started by a channel.", status=409)
    thread = store.get_thread(ctx.hub_dir, tid)
    if thread is None:
        raise ChannelError("no_thread", "The channel thread for this work is gone.", status=409)
    return Thread(ctx.hub_dir, thread, item)
