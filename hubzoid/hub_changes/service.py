"""Proposing, reviewing and applying changes to a hub's own files.

What may change: ``AGENTS.md`` and Markdown files under ``knowledge/`` and
``skills/`` (found under any accepted spelling of those folders). Access, approval and run settings, workflow code,
local tools, secrets and anything outside the hub are never proposable.

A proposal stores, per file, the SHA-256 of the bytes it had when proposed
(none for a new file) and the full bytes it will have. Applying writes them
only if every file still has its proposed-on bytes: a second proposal made on
the same base conflicts once the first is applied, and nothing is written.

Who: anyone holding ``curator`` (Save shared knowledge) or managing the hub
may propose. Only someone managing the hub, checked when they decide, may
apply or reject, by a person's own click or command. A change an agent
proposes never applies itself, even for a manager: they apply it from the
chat card. A manager's knowledge-only change from the CLI or API applies at once.

Applying holds the hub write lock (shared with scheduled-task commits), writes
each file atomically and, when the hub is in a Git repository, commits exactly
those files with ``Hubzoid-Change``, ``Hubzoid-Proposed-By`` and
``Hubzoid-Approved-By`` trailers. A crash between writing and recording is
recovered by searching for that trailer. With ``HUBZOID_HUB_CHANGES_PUSH=1`` the
commit is pushed as it is: never rebased, and a rejected push is shown as
needing attention, never retried by force.
"""
from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

import sqlalchemy as sa

from ..control import canonical

log = logging.getLogger(__name__)

metadata = sa.MetaData()
changes = sa.Table(
    "hz_hub_changes", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("title", sa.String(300), nullable=False),
    sa.Column("reason", sa.Text),
    sa.Column("status", sa.String(24), nullable=False),   # pending | applying | applied | rejected | conflict | needs_attention
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("files_json", sa.Text, nullable=False),
    sa.Column("digest", sa.String(80), nullable=False),
    sa.Column("proposed_by", sa.String(320), nullable=False),
    sa.Column("proposed_via", sa.String(24), nullable=False),  # chat | workflow | mcp | api | cli
    sa.Column("conversation_id", sa.String(64)),
    sa.Column("undoes", sa.Integer),
    sa.Column("decided_by", sa.String(320)),
    sa.Column("note", sa.Text),
    sa.Column("commit_sha", sa.String(64)),
    sa.Column("publish_state", sa.String(24)),             # None | pushed | needs_attention
    sa.Column("problem", sa.Text),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

ROOTS = ("knowledge", "skills")
MAX_FILE_BYTES = 200_000
MAX_FILES = 20
REVIEW_PERMISSION = "manage_access"
PROPOSE_PERMISSION = "curator"
# Never proposable, whatever the folder: access, approvals, run settings, secrets.
RESERVED = re.compile(r"(^|/)(\.env[^/]*|\.git|\.hubzoid|\.mcp\.json|hitl\.ya?ml|access\.ya?ml|settings\.ya?ml"
                      r"|restricted|tools_local|workflows|output|secrets?)(/|$)", re.I)
SECRET = re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-(ant-|proj-)?[A-Za-z0-9_-]{20,}|\bgh[pousr]_[A-Za-z0-9]{30,}"
                    r"|\bxox[abpr]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{35}\b)")


class ChangeError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_change", status: int = 400, **extra):
        super().__init__(message)
        self.message, self.code, self.status, self.extra = message, code, status, extra


def engine(hub_dir: Path):
    from .. import db

    return db.operational_engine(Path(hub_dir))


def hub_id(hub_dir: Path) -> str:
    return Path(hub_dir).name.lower()


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


# -- who ---------------------------------------------------------------------------------
def _can(hub_dir: Path, email: str, permission: str) -> bool:
    from ..access import store_for

    try:
        return bool(email) and store_for(Path(hub_dir)).can(email, Path(hub_dir).name, permission)
    except Exception:  # noqa: BLE001 - fail closed
        log.exception("hub changes: access check failed")
        return False


def can_review(hub_dir: Path, email: str) -> bool:
    return _can(hub_dir, email, REVIEW_PERMISSION)


def can_propose(hub_dir: Path, email: str) -> bool:
    return can_review(hub_dir, email) or _can(hub_dir, email, PROPOSE_PERMISSION)


# -- paths and files ---------------------------------------------------------------------
def check_path(path: str) -> str:
    """The canonical hub-relative path, or ChangeError naming what may change."""
    raw = str(path or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    p = PurePosixPath(raw)
    if not raw or p.is_absolute() or ".." in p.parts:
        raise ChangeError(f"{path!r} is not a path inside this hub.")
    rel = str(p)
    if RESERVED.search(rel):
        raise ChangeError(f"{rel} can't be changed this way: access, approval and run settings, workflow "
                          "code, tools and secrets are changed by an admin directly.", code="reserved_path")
    if rel.lower() == "agents.md":
        return "AGENTS.md"
    if p.parts[0].lower() not in ROOTS or len(p.parts) < 2 or p.suffix.lower() != ".md":
        raise ChangeError(f"{rel} can't be changed this way. You can change AGENTS.md and Markdown files "
                          "under knowledge/ and skills/.", code="outside_scope")
    return str(PurePosixPath(p.parts[0].lower(), *p.parts[1:]))  # the folder by its canonical name


def _disk(hub_dir: Path, rel: str) -> Path:
    from .._fs import resolve_bucket

    parts = PurePosixPath(rel).parts
    if len(parts) > 1:
        root = resolve_bucket(Path(hub_dir), parts[0]) or (Path(hub_dir) / parts[0])
        target = root.joinpath(*parts[1:])
    else:
        target = Path(hub_dir) / rel
    resolved = target.resolve()
    if not str(resolved).startswith(str(Path(hub_dir).resolve()) + os.sep):
        raise ChangeError(f"{rel} is not inside this hub.")
    return target


def read_bytes(hub_dir: Path, rel: str) -> bytes | None:
    target = _disk(hub_dir, rel)
    return target.read_bytes() if target.is_file() else None


def read_text(hub_dir: Path, path: str) -> str | None:
    """A hub file an agent may read for a change: the same scope as proposals."""
    data = read_bytes(hub_dir, check_path(path))
    return None if data is None else data.decode("utf-8", errors="replace")


def build(hub_dir: Path, edits: list[dict]) -> list[dict]:
    """Turn exact edits into whole files: [{path, base_sha, base_text, text}].

    An edit is ``{path, old_text, new_text}`` (``old_text`` must occur exactly
    once), ``{path, append}`` or ``{path, content}`` (a new file only)."""
    if not isinstance(edits, list) or not edits:
        raise ChangeError("A change needs at least one edit.")
    files: dict[str, dict] = {}
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            raise ChangeError(f"Edit {i} is not an object.")
        rel = check_path(e.get("path"))
        if rel not in files:
            base = read_bytes(hub_dir, rel)
            text = None if base is None else base.decode("utf-8")
            files[rel] = {"path": rel, "base_sha": _sha(base), "base_text": text, "text": text}
        f = files[rel]
        if "content" in e:
            if f["base_sha"] is not None or f["text"] is not None:
                raise ChangeError(f"{rel} already exists. Change it with an exact edit instead.",
                                  code="file_exists")
            f["text"] = str(e["content"])
        elif "append" in e:
            if f["text"] is None:
                raise ChangeError(f"{rel} doesn't exist yet. Create it with content.", code="no_file")
            f["text"] = f["text"].rstrip("\n") + "\n\n" + str(e["append"]).strip("\n") + "\n"
        elif "old_text" in e:
            if f["text"] is None:
                raise ChangeError(f"{rel} doesn't exist yet. Create it with content.", code="no_file")
            old, new = str(e.get("old_text") or ""), str(e.get("new_text") or "")
            count = f["text"].count(old) if old else 0
            if count != 1:
                raise ChangeError(f"Edit {i}: the text to replace appears {count} times in {rel}; it must "
                                  "appear exactly once. Copy it exactly, with a little more context.",
                                  code="edit_not_unique" if count else "edit_not_found")
            f["text"] = f["text"].replace(old, new, 1)
        else:
            raise ChangeError(f"Edit {i} needs old_text and new_text, append, or content.")
    out = [f for f in files.values() if f["text"] != f["base_text"]]
    if not out:
        raise ChangeError("These edits change nothing.", code="no_change")
    if len(out) > MAX_FILES:
        raise ChangeError(f"A change may touch at most {MAX_FILES} files.")
    for f in out:
        if f["text"] is not None and len(f["text"].encode()) > MAX_FILE_BYTES:
            raise ChangeError(f"{f['path']} would be larger than {MAX_FILE_BYTES // 1000} KB.")
        if f["text"] is not None and SECRET.search(f["text"]) and not (f["base_text"] and SECRET.search(f["base_text"])):
            raise ChangeError(f"{f['path']} would contain what looks like a password or key. Secrets never "
                              "go in hub files.", code="secret_detected")
    return out


def diff(change: dict) -> str:
    parts = []
    for f in change["files"]:
        a = (f["base_text"] or "").splitlines(keepends=True)
        b = (f["text"] or "").splitlines(keepends=True)
        parts.append("".join(difflib.unified_diff(a, b, "/dev/null" if f["base_sha"] is None else f"a/{f['path']}",
                                                  "/dev/null" if f["text"] is None else f"b/{f['path']}")))
    return "\n".join(parts)


# -- the store -----------------------------------------------------------------------------
def _row(r) -> dict:
    d = dict(r._mapping)
    d["files"] = json.loads(d.pop("files_json"))
    for k in ("created_at", "updated_at"):
        d[k] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(d[k]))
    d["paths"] = [f["path"] for f in d["files"]]
    return d


def get(hub_dir: Path, change_id: int) -> dict | None:
    with engine(hub_dir).connect() as c:
        r = c.execute(sa.select(changes).where(changes.c.hub_id == hub_id(hub_dir),
                                               changes.c.id == int(change_id))).first()
    return _row(r) if r else None


def list_(hub_dir: Path, *, status: str | None = None, limit: int = 100) -> list[dict]:
    q = sa.select(changes).where(changes.c.hub_id == hub_id(hub_dir))
    if status == "attention":
        q = q.where(sa.or_(changes.c.status.in_(("conflict", "needs_attention")),
                           changes.c.publish_state == "needs_attention"))
    elif status:
        q = q.where(changes.c.status == status)
    with engine(hub_dir).connect() as c:
        rows = c.execute(q.order_by(changes.c.id.desc()).limit(limit)).all()
    return [_row(r) for r in rows]


def _set(hub_dir: Path, change_id: int, revision: int, **values) -> bool:
    values.setdefault("updated_at", time.time())
    with engine(hub_dir).begin() as c:
        done = c.execute(changes.update().where(changes.c.hub_id == hub_id(hub_dir), changes.c.id == change_id,
                                                changes.c.revision == revision)
                         .values(revision=revision + 1, **values))
    return done.rowcount == 1


def propose(hub_dir: Path, *, title: str, edits: list[dict], actor: str, via: str, reason: str = "",
            conversation_id: str | None = None, present: bool = True, undoes: int | None = None) -> dict:
    """Save a proposal. A hub manager's change to knowledge/ only, made by a
    person who is there, is applied at once."""
    actor = (actor or "").strip().lower()
    if not can_propose(hub_dir, actor):
        raise ChangeError("You can't propose changes to this hub. An admin can give you Save shared knowledge.",
                          code="permission_denied", status=403)
    title = " ".join(str(title or "").split())[:300]
    if not title:
        raise ChangeError("Give the change a short title.")
    files = build(hub_dir, edits)
    digest = canonical.digest({"files": [{"path": f["path"], "base": f["base_sha"], "text": f["text"]}
                                         for f in files]})
    now = time.time()
    with engine(hub_dir).begin() as c:
        cid = c.execute(changes.insert().values(
            hub_id=hub_id(hub_dir), title=title, reason=str(reason or "")[:4000] or None, status="pending",
            revision=1, files_json=json.dumps(files), digest=digest, proposed_by=actor, proposed_via=via,
            conversation_id=conversation_id, undoes=undoes, created_at=now, updated_at=now)).inserted_primary_key[0]
    log.info("hub change #%s proposed by %s: %s", cid, actor, ", ".join(f["path"] for f in files))
    # A model's tool call never applies anything, even for a manager (a
    # prompt-injected call must not change shared knowledge). A manager applies
    # it with one click on the chat card. A person proposing from their own
    # terminal or session may apply knowledge directly.
    trusted = (present and via in ("api", "cli") and can_review(hub_dir, actor)
               and all(f["path"].lower().startswith("knowledge/") for f in files))
    if trusted:
        return apply(hub_dir, cid, actor=actor, note="Applied directly by a hub manager (knowledge only).")
    return get(hub_dir, cid)


def decide(hub_dir: Path, change_id: int, *, actor: str, decision: str, expected_revision: int | None = None,
           note: str | None = None) -> dict:
    actor = (actor or "").strip().lower()
    ch = get(hub_dir, change_id)
    if ch is None:
        raise ChangeError(f"There is no change #{change_id}.", code="not_found", status=404)
    if not can_review(hub_dir, actor):
        raise ChangeError("Only someone who manages this hub can apply or reject changes.",
                          code="permission_denied", status=403)
    if expected_revision is not None and int(expected_revision) != ch["revision"]:
        raise ChangeError("This change was updated. Reload it and review again.", code="revision_conflict",
                          status=409, current_revision=ch["revision"])
    if ch["status"] != "pending":
        raise ChangeError(f"Change #{change_id} is already {ch['status']}.", code="not_pending", status=409)
    if decision == "reject":
        if not _set(hub_dir, ch["id"], ch["revision"], status="rejected", decided_by=actor, note=note or None):
            raise ChangeError("Someone else decided this change first.", code="revision_conflict", status=409)
        return get(hub_dir, ch["id"])
    if decision != "approve":
        raise ChangeError("Decide approve or reject.")
    return apply(hub_dir, ch["id"], actor=actor, note=note)


# -- applying -------------------------------------------------------------------------------
@contextlib.contextmanager
def write_lock(hub_dir: Path):
    """The hub write lock: hub changes and scheduled-task commits take turns."""
    from .. import _file_lock

    path = Path(hub_dir) / ".hubzoid" / "hub-write.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        _file_lock.lock(fh)
        try:
            yield
        finally:
            _file_lock.unlock(fh)


def _write(target: Path, text: str | None) -> None:
    if text is None:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        os.replace(tmp, target)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


def _git(top: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(top), *args], capture_output=True, text=True, check=check)


def _commit_message(ch: dict, approver: str) -> str:
    # Only what the reviewer approved goes into shared history: the title and
    # the trailers. The agent-written reason stays in Hubzoid (it can quote a
    # person's own notes, which must never reach the hub's Git history).
    return (f"{ch['title']}\n\nReviewed and applied in Hubzoid as change #{ch['id']}.\n\n"
            f"Hubzoid-Change: {ch['hub_id']}#{ch['id']}\nHubzoid-Proposed-By: {ch['proposed_by']}\n"
            f"Hubzoid-Approved-By: {approver}\n")


def git_identity(hub_dir: Path, email: str) -> tuple[list[str], dict]:
    """Who a hub commit is by: the approving or publishing person as author; as committer
    the identity configured for this checkout, else ``HUBZOID_GIT_COMMITTER``
    ("Name <email>"), else Hubzoid. Returns (git -c options, env)."""
    import re as _re

    env: dict = {}
    if email and "@" in email:  # no person given: the committer is the author too
        name = email.split("@")[0]
        try:
            from ..auth import users

            name = ((users.find_by_email(Path(hub_dir), email) or {}).get("name") or name).strip() or name
        except Exception:  # noqa: BLE001 - the address alone is enough
            pass
        env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email}
    configured = os.environ.get("HUBZOID_GIT_COMMITTER") or ""
    m = _re.match(r"\s*(.+?)\s*<([^>]+)>\s*$", configured)
    if m:
        env.update(GIT_COMMITTER_NAME=m.group(1), GIT_COMMITTER_EMAIL=m.group(2))
        return [], env
    from ..schedule_runner import repo_toplevel

    top = repo_toplevel(Path(hub_dir))
    has_own = top is not None and _git(top, "config", "user.email", check=False).stdout.strip()
    return ([] if has_own else ["-c", "user.name=Hubzoid", "-c", "user.email=hub-changes@hubzoid.invalid"]), env


def commit_files(hub_dir: Path, paths: list[Path], message: str, trailer: str, *,
                 author_email: str | None = None) -> str | None:
    """Commit exactly ``paths`` (files on disk under the hub) with ``message``,
    whose ``trailer`` line identifies the change. Nothing staged means it was
    committed already (a retry after a crash): that commit is found by its
    trailer. None when the hub is not in a Git repository. Every web-app change
    to hub files (hub changes, Workspace publications) commits this way, under
    ``write_lock``. ``author_email``: the person the commit is by (see
    ``git_identity``); the committer is this checkout's identity."""
    from ..schedule_runner import repo_toplevel

    top = repo_toplevel(Path(hub_dir))
    if top is None:
        return None
    specs = [os.path.relpath(p, top) for p in paths]
    _git(top, "add", "-A", "--", *specs)
    if not _git(top, "diff", "--cached", "--quiet", "--", *specs, check=False).returncode:
        r = _git(top, "log", "--format=%H", "-n", "1", "--fixed-strings", f"--grep={trailer}", check=False)
        return r.stdout.strip() or None
    opts, env = git_identity(hub_dir, author_email or "")
    subprocess.run(["git", "-C", str(top), *opts, "commit", "-q", "-m", message, "--", *specs],
                   capture_output=True, text=True, check=True, env={**os.environ, **env})
    return _git(top, "rev-parse", "HEAD").stdout.strip()


def _commit(hub_dir: Path, ch: dict, approver: str) -> str | None:
    return commit_files(hub_dir, [_disk(hub_dir, f["path"]) for f in ch["files"]], _commit_message(ch, approver),
                        f"Hubzoid-Change: {ch['hub_id']}#{ch['id']}", author_email=approver)


def _publish(hub_dir: Path) -> tuple[str | None, str | None]:
    """Push the current branch as it is. Never rebases or forces. Without
    ``HUBZOID_HUB_CHANGES_PUSH=1`` the commit stays on this server ("local"):
    shown in Hub changes, and someone pushes it from the hub folder."""
    if (os.environ.get("HUBZOID_HUB_CHANGES_PUSH") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return "local", None
    from ..schedule_runner import _push_args, repo_toplevel

    top = repo_toplevel(Path(hub_dir))
    if top is None:
        return None, None
    r = _git(top, *_push_args(top), check=False)
    if r.returncode == 0:
        return "pushed", None
    return "needs_attention", ("The push was refused, so the change is applied here but not published. "
                               "Someone who operates this hub must reconcile the branch by hand; nothing "
                               "is merged or rebased automatically. Git said: "
                               + (r.stderr.strip().splitlines() or ["(nothing)"])[-1][:300])


def apply(hub_dir: Path, change_id: int, *, actor: str, note: str | None = None) -> dict:
    hub_dir = Path(hub_dir)
    with write_lock(hub_dir):
        ch = get(hub_dir, change_id)
        if ch is None or ch["status"] not in ("pending", "applying"):
            raise ChangeError(f"Change #{change_id} can't be applied now.", code="not_pending", status=409)
        current = {f["path"]: _sha(read_bytes(hub_dir, f["path"])) for f in ch["files"]}
        target = {f["path"]: _sha(None if f["text"] is None else f["text"].encode()) for f in ch["files"]}
        at_base = all(current[p] == f["base_sha"] for p, f in ((f["path"], f) for f in ch["files"]))
        at_target = all(current[p] == target[p] for p in current)
        if ch["status"] == "pending":
            if not at_base:
                drifted = [p for p in current if current[p] != next(f for f in ch["files"] if f["path"] == p)["base_sha"]]
                _set(hub_dir, ch["id"], ch["revision"], status="conflict", decided_by=actor,
                     problem="These files changed after the proposal was made, so nothing was written: "
                             + ", ".join(drifted) + ". Propose the change again on the current files.")
                return get(hub_dir, ch["id"])
            if not _set(hub_dir, ch["id"], ch["revision"], status="applying", decided_by=actor, note=note):
                raise ChangeError("Someone else decided this change first.", code="revision_conflict", status=409)
            ch = get(hub_dir, ch["id"])
        elif not (at_base or at_target):
            _set(hub_dir, ch["id"], ch["revision"], status="needs_attention",
                 problem="Applying stopped part way and the files no longer match either version. Check "
                         "them by hand.")
            return get(hub_dir, ch["id"])
        try:
            if not at_target:
                for f in ch["files"]:
                    _write(_disk(hub_dir, f["path"]), f["text"])
            sha = _commit(hub_dir, ch, ch.get("decided_by") or actor)
        except Exception as exc:  # noqa: BLE001 - recorded, recoverable
            log.exception("hub change #%s: apply failed", ch["id"])
            _set(hub_dir, ch["id"], ch["revision"], status="needs_attention",
                 problem=f"Applying failed: {type(exc).__name__}. The files may be written but not committed; "
                         "it is retried when the hub starts.")
            return get(hub_dir, ch["id"])
        publish, problem = _publish(hub_dir) if sha else (None, None)
        _set(hub_dir, ch["id"], ch["revision"], status="applied", commit_sha=sha, publish_state=publish,
             problem=problem)
    log.info("hub change #%s applied by %s (%s)", ch["id"], actor, sha or "no git")
    return get(hub_dir, ch["id"])


def recover(hub_dir: Path) -> list[int]:
    """Finish changes a crash left in ``applying``: one commit each, never two."""
    done = []
    for ch in list_(hub_dir, status="applying"):
        try:
            apply(hub_dir, ch["id"], actor=ch.get("decided_by") or "recovery")
            done.append(ch["id"])
        except Exception:  # noqa: BLE001
            log.exception("hub change #%s: recovery failed", ch["id"])
    return done


def undo(hub_dir: Path, change_id: int, *, actor: str, via: str = "api") -> dict:
    """A new proposal that puts the files back. It needs its own review."""
    ch = get(hub_dir, change_id)
    if ch is None or ch["status"] != "applied":
        raise ChangeError(f"Only an applied change can be undone; #{change_id} is "
                          f"{ch['status'] if ch else 'missing'}.", code="not_applied", status=409)
    edits = []
    for f in ch["files"]:
        now_text = read_text(hub_dir, f["path"])
        if now_text != f["text"]:
            raise ChangeError(f"{f['path']} changed after #{change_id}, so it can't be undone as it was. "
                              "Propose the change you want instead.", code="drifted", status=409)
        if f["base_text"] is None:
            raise ChangeError(f"#{change_id} created {f['path']}; remove it by proposing an empty file or by "
                              "hand.", code="created_file", status=409)
        edits.append({"path": f["path"], "old_text": f["text"], "new_text": f["base_text"]})
    return propose(hub_dir, title=f"Undo #{ch['id']}: {ch['title']}"[:300], edits=edits, actor=actor, via=via,
                   reason=f"Puts back the files as they were before #{ch['id']}.", present=False, undoes=ch["id"])


def needs_reload(ch: dict) -> bool:
    """Whether a change touched what a runtime reads only when it is built."""
    return any(p == "AGENTS.md" or p.startswith("skills/") for p in ch.get("paths") or
               [f["path"] for f in ch.get("files") or []])


def public(ch: dict, *, with_diff: bool = True) -> dict:
    """A change as the API and the chat card show it (whole file bytes stay inside)."""
    out = {k: ch[k] for k in ("id", "title", "reason", "status", "revision", "proposed_by", "proposed_via",
                              "decided_by", "note", "commit_sha", "publish_state", "problem", "undoes",
                              "created_at", "updated_at", "paths", "conversation_id")}
    if with_diff:
        out["diff"] = diff(ch)
    return out
