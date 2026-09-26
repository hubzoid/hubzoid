# Hubzoid published artifacts. Apache-2.0 licensed like the rest of the repository.
"""Published artifacts: a file a workflow generated, owned by the person the run
acted as, private until that person shares it.

Publishing is separate from generating: `publish` copies an existing file into
`<hub>/.hubzoid/artifacts/<id>/` (private to agent file tools, unreachable from
the legacy `/artifacts/<chat>/<file>` route, included in backups) and records
owner, hub, workflow, run, content type, size, hash, storage and audience in
`hz_artifacts`. Every publish gets a new id, so a later run never overwrites an
earlier report even when the file names match.

Who may open an artifact is decided here, in `role`, and nowhere else:

  * owner     the account the run acted as. View, download, share, revoke, delete.
  * viewer    view and download only, when the audience allows it:
                owner   only the owner (the default)
                people  listed accounts or groups who can currently use the hub
                hub     anyone who can currently use the hub
                link    anyone holding an unexpired, unrevoked public link
  * nobody else. Organization admins and workflow managers get nothing extra.

`people` and `hub` need a Console-managed hub, whose membership Hubzoid can
check. A public link needs the owner to hold `share_public_links` in the hub,
when it is created and every time it is opened. Link tokens are 256-bit random
values stored only as a SHA-256 hash. A workflow or model can never create one.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

log = logging.getLogger("hubzoid.artifacts")

PUBLIC_LINK_PERMISSION = "share_public_links"
PUBLIC_LINK_META = {
    "label": "Share reports by public link",
    "description": ("Create 'anyone with the link' links for reports you own. Anyone who has "
                    "such a link can open the report without signing in."),
    "sensitive": True,
}
# Registered where it is enforced (create_link / open_link below), so the
# Console lists it (hubzoid.capabilities; this module is in REGISTRANTS).
from ..capabilities import Capability, register  # noqa: E402

SHARE_PUBLIC = register(Capability(
    permission=PUBLIC_LINK_PERMISSION, label=PUBLIC_LINK_META["label"], group="tools",
    description=PUBLIC_LINK_META["description"], surfaces=(), sensitive=True,
))
AUDIENCES = ("owner", "people", "hub", "link")
STORE_DIR = ".hubzoid/artifacts"
ID_RE = re.compile(r"^a[A-Za-z0-9_-]{16,40}$")
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_LINK_DAYS = 7
MAX_LINK_DAYS = 90
MAX_SHARES = 200

# Preview kinds by extension (never by the file's own claim of its type).
_KINDS = {
    ".html": "html", ".htm": "html",
    ".pdf": "pdf",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
    ".svg": "svg",
    ".csv": "csv", ".tsv": "csv",
    ".txt": "text", ".md": "text", ".json": "text", ".log": "text",
    ".yaml": "text", ".yml": "text",
}


class ArtifactError(Exception):
    """A refused artifact operation. `status` is an HTTP status and `message`
    is safe to show (it never contains a token or file content)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Artifact:
    id: str
    hub: str
    owner: str
    owner_account: str | None
    workflow: str | None
    run_id: str | None
    title: str
    filename: str
    content_type: str
    size: int
    sha256: str
    storage: str
    audience: str
    created: float
    updated: float
    deleted: float | None

    @property
    def kind(self) -> str:
        return kind_for(self.filename)


def kind_for(filename: str) -> str:
    return _KINDS.get(Path(filename).suffix.lower(), "download")


def _now() -> float:
    return time.time()


def _engine(hub_dir):
    from .. import db
    from ..migrations import upgrade

    eng = db.operational_engine(Path(hub_dir))
    upgrade(eng, "operational")
    return eng


def _store(hub_dir):
    from ..access import store_for

    return store_for(Path(hub_dir))


def max_bytes() -> int:
    try:
        return max(1, int(os.environ.get("HUBZOID_ARTIFACT_MAX_BYTES") or DEFAULT_MAX_BYTES))
    except ValueError:
        return DEFAULT_MAX_BYTES


def public_base_url(hub_dir=None) -> str:
    """Where people open Hubzoid in a browser (the edge, fronting /portal).

    A gateway bridge's HUBZOID_PUBLIC_URL ends in its download prefix
    (`/b/<hub>`), which the edge routes only for `/artifacts` and `/mcp`. The
    report viewer and public links live at the site root, so drop it. A process
    without either setting (a CLI command running a job) uses the address the
    gateway recorded in the deployment manifest."""
    base = (os.environ.get("HUBZOID_PUBLIC_URL") or "").rstrip("/")
    base = re.sub(r"/b/[^/]+$", "", base) or (os.environ.get("WEBUI_URL") or "").rstrip("/")
    if not base and hub_dir is not None:
        from .. import deployment
        try:
            base = (deployment.read(Path(hub_dir)).get("public_url") or "").rstrip("/")
        except (OSError, ValueError):
            base = ""
    return base or f"http://127.0.0.1:{os.environ.get('PORT') or '3080'}"


def viewer_url(artifact_id: str, hub_dir=None) -> str:
    return f"{public_base_url(hub_dir)}/portal/artifacts/{artifact_id}"


def _safe_filename(name: str) -> str:
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1].replace("\x00", "").strip()
    name = re.sub(r"[\r\n\t\"]", "_", name)
    if name in ("", ".", ".."):
        return "artifact"
    return name[:200]


def _row(r) -> Artifact:
    return Artifact(**dict(r._mapping))


_COLS = ("id, hub, owner, owner_account, workflow, run_id, title, filename, content_type, "
         "size, sha256, storage, audience, created, updated, deleted")


def get(hub_dir, artifact_id: str, *, include_deleted: bool = False) -> Artifact | None:
    if not isinstance(artifact_id, str) or not ID_RE.match(artifact_id):
        return None
    with _engine(hub_dir).connect() as c:
        r = c.execute(text(f"SELECT {_COLS} FROM hz_artifacts WHERE id=:i"),
                      {"i": artifact_id}).fetchone()
    if r is None:
        return None
    art = _row(r)
    if art.deleted and not include_deleted:
        return None
    return art


def content_path(hub_dir, art: Artifact) -> Path:
    """The stored file, resolved inside its hub's artifact store (any bridge of
    the deployment can serve any hub's artifact)."""
    from .. import deployment

    try:
        hub_root = deployment.hub_path(Path(hub_dir), art.hub)
    except KeyError:
        raise ArtifactError(404, "This report is not available.")
    base = (Path(hub_root) / STORE_DIR).resolve()
    target = (Path(hub_root) / art.storage).resolve()
    if base not in target.parents or not target.is_file():
        raise ArtifactError(404, "This report is not available.")
    return target


# ---- publishing -----------------------------------------------------------------


def publish(hub_dir, *, hub: str, owner: str, owner_account: str | None, source: Path,
            title: str | None = None, workflow: str | None = None, run_id: str | None = None,
            idem_key: str | None = None, audience: str = "owner",
            share_with=()) -> dict:
    """Store `source` as a new artifact owned by `owner` and return
    {id, url, title, filename, content_type, size}. The caller has already
    established `owner` from the run's identity, never from an argument.

    `idem_key` makes a retried publish (a DBOS step re-run after a crash) return
    the artifact the first attempt stored instead of storing it twice."""
    from ..access import normalize

    hub = normalize(hub)
    owner = normalize(owner)
    eng = _engine(hub_dir)
    if idem_key:
        with eng.connect() as c:
            r = c.execute(text(f"SELECT {_COLS} FROM hz_artifacts WHERE idem_key=:k"),
                          {"k": idem_key}).fetchone()
        if r is not None:
            return _summary(_row(r), hub_dir)
    source = Path(source)
    if not source.is_file():
        raise ArtifactError(400, f"Nothing to publish: {source} is not a file.")
    size = source.stat().st_size
    if size > max_bytes():
        raise ArtifactError(413, f"{source.name} is {size} bytes, over the "
                                 f"HUBZOID_ARTIFACT_MAX_BYTES limit of {max_bytes()}.")
    if audience not in ("owner", "people", "hub"):
        raise ArtifactError(400, "A workflow can publish to 'owner', 'people' or 'hub'. "
                                 "Public links are created by the owner in the viewer.")
    filename = _safe_filename(source.name)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    artifact_id = "a" + secrets.token_urlsafe(18)
    rel = f"{STORE_DIR}/{artifact_id}/{filename}"
    target = Path(hub_dir) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    tmp = target.with_name(f".{filename}.{os.getpid()}.tmp")
    with source.open("rb") as src, tmp.open("wb") as dst:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            digest.update(chunk)
            dst.write(chunk)
    tmp.replace(target)
    now = _now()
    row = dict(id=artifact_id, hub=hub, owner=owner, owner_account=owner_account,
               workflow=workflow, run_id=run_id, idem_key=idem_key,
               title=(title or source.stem)[:300], filename=filename,
               content_type=content_type, size=size, sha256=digest.hexdigest(),
               storage=rel, audience="owner", created=now, updated=now)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO hz_artifacts (id, hub, owner, owner_account, workflow, run_id, idem_key, "
            "title, filename, content_type, size, sha256, storage, audience, created, updated) "
            "VALUES (:id, :hub, :owner, :owner_account, :workflow, :run_id, :idem_key, :title, "
            ":filename, :content_type, :size, :sha256, :storage, :audience, :created, :updated)"),
            row)
    art = get(hub_dir, artifact_id)
    if audience != "owner":
        set_audience(hub_dir, art, owner, audience, share_with)
        art = get(hub_dir, artifact_id)
    log.info("artifacts: published %s (%s, %d bytes) for %s in %s", artifact_id,
             filename, size, owner, hub)
    return _summary(art, hub_dir)


def _summary(art: Artifact, hub_dir=None) -> dict:
    return dict(id=art.id, url=viewer_url(art.id, hub_dir), title=art.title, filename=art.filename,
                content_type=art.content_type, size=art.size, audience=art.audience)


# ---- who may open it ------------------------------------------------------------


def hub_managed(hub_dir, hub: str) -> bool:
    try:
        return bool(_store(hub_dir).is_authoritative(hub))
    except Exception:  # noqa: BLE001 — unknown means unmanaged (fail closed)
        log.exception("artifacts: access store unavailable")
        return False


def hub_member(hub_dir, hub: str, subject: str) -> bool:
    """Can `subject` currently use `hub`? Only answerable for managed hubs."""
    from ..access.store import USE_HUB

    try:
        gs = _store(hub_dir)
        return bool(gs.is_authoritative(hub) and gs.can(subject, hub, USE_HUB))
    except Exception:  # noqa: BLE001 — fail closed
        log.exception("artifacts: membership check failed")
        return False


def _active(hub_dir, subject: str) -> bool:
    try:
        return bool(subject) and not _store(hub_dir).is_suspended(subject)
    except Exception:  # noqa: BLE001 — fail closed
        log.exception("artifacts: account check failed")
        return False


def shares(hub_dir, artifact_id: str) -> list[dict]:
    with _engine(hub_dir).connect() as c:
        rows = c.execute(text("SELECT kind, principal, account FROM hz_artifact_shares "
                              "WHERE artifact_id=:a ORDER BY kind, principal"),
                         {"a": artifact_id}).fetchall()
    return [dict(kind=k, principal=p, account=a) for k, p, a in rows]


def _account_of(hub_dir, subject: str) -> str | None:
    """The chat-app account id currently bound to `subject`, or None."""
    try:
        return (_store(hub_dir).identity(subject) or {}).get("owui_id") or None
    except Exception:  # noqa: BLE001 — unknown binds nothing
        log.exception("artifacts: identity lookup failed")
        return None


def _local_owner(hub_dir, subject: str) -> bool:
    """The single local quickstart account (authentication off, no deployment),
    which may publish before the chat app has created its account row."""
    from ..access.session import LOCAL_OWNER
    from ..workflows.identity import local_quickstart

    return subject == LOCAL_OWNER and local_quickstart(Path(hub_dir))


def _owner_current(hub_dir, art: Artifact) -> bool:
    """Is the report's recorded owner still that owner, now? The same chat-app
    account it was published under (an email reused by a replacement account
    inherits nothing), not blocked, and on a Console-managed hub still able to
    use the hub. A report recorded without an account id is honoured only for
    the local quickstart account. Legacy hubs keep their membership in the chat
    app, so only the account checks apply there."""
    owner = art.owner
    if not _active(hub_dir, owner):
        return False
    if art.owner_account:
        if _account_of(hub_dir, owner) != art.owner_account:
            return False
    elif not _local_owner(hub_dir, owner):
        return False
    if hub_managed(hub_dir, art.hub) and not hub_member(hub_dir, art.hub, owner):
        return False
    return True


def _groups_of(hub_dir, hub: str, subject: str) -> set[str]:
    from .. import deployment
    from ..access import effective_groups, normalize

    try:
        path = deployment.hub_path(Path(hub_dir), hub)
    except KeyError:
        return set()
    try:
        return {normalize(g) for g in effective_groups(path, email=subject)}
    except Exception:  # noqa: BLE001 — a failed lookup grants nothing
        return set()


def role(hub_dir, art: Artifact | None, subject: str) -> str | None:
    """'owner', 'viewer' or None for a signed-in `subject`. The one decision."""
    from ..access import normalize

    subject = normalize(subject)
    if art is None or art.deleted or not _active(hub_dir, subject):
        return None
    if subject == art.owner:
        return "owner" if _owner_current(hub_dir, art) else None
    if art.audience == "hub":
        return "viewer" if hub_member(hub_dir, art.hub, subject) else None
    if art.audience == "people":
        if not hub_member(hub_dir, art.hub, subject):
            return None
        listed = shares(hub_dir, art.id)
        mine = [s for s in listed if s["kind"] == "user" and s["principal"] == subject]
        # A share names the account it was made for: a replacement account under
        # the same email is not that person.
        if any(not s["account"] or s["account"] == _account_of(hub_dir, subject) for s in mine):
            return "viewer"
        wanted = {s["principal"] for s in listed if s["kind"] == "group"}
        if wanted and wanted & _groups_of(hub_dir, art.hub, subject):
            return "viewer"
    return None


# ---- the owner's controls -------------------------------------------------------


def _require_owner(hub_dir, art: Artifact | None, actor: str) -> Artifact:
    if role(hub_dir, art, actor) != "owner":
        raise ArtifactError(404, "This report is not available.")
    return art


def _audit(hub_dir, hub: str, action: str, target: str, actor: str) -> None:
    try:
        _store(hub_dir).audit_event(actor, action, hub=hub, permission=target, surface="web")
    except Exception:  # noqa: BLE001 — the change itself is recorded on the row
        log.exception("artifacts: could not audit %s", action)


def set_audience(hub_dir, art: Artifact | None, actor: str, audience: str,
                 people=()) -> None:
    """Change who can view `art`. Only the owner. `people` is a list of
    {"kind": "user"|"group", "principal": ...} (or plain emails)."""
    from ..access import normalize

    art = _require_owner(hub_dir, art, actor)
    if audience not in ("owner", "people", "hub"):
        raise ArtifactError(400, "Choose only you, specific people, or everyone in this hub. "
                                 "Public links have their own action.")
    entries: list[tuple[str, str]] = []
    if audience in ("people", "hub") and not hub_managed(hub_dir, art.hub):
        raise ArtifactError(409, "This agent's access is still managed in the chat app, so "
                                 "Hubzoid cannot check who belongs to it. Only you, or a "
                                 "public link, are available until it is migrated.")
    if audience == "people":
        for p in list(people or [])[:MAX_SHARES + 1]:
            kind, principal = ("user", p) if isinstance(p, str) else (p.get("kind"), p.get("principal"))
            principal = normalize(principal or "")
            if kind not in ("user", "group") or not principal:
                raise ArtifactError(400, "Each person needs an account email or a group name.")
            if kind == "user":
                if "@" not in principal:
                    raise ArtifactError(400, f"{principal!r} is not an account email.")
                if principal != art.owner and not hub_member(hub_dir, art.hub, principal):
                    raise ArtifactError(409, f"{principal} cannot use this agent, so the "
                                             "report cannot be shared with them.")
            entries.append((kind, principal))
        entries = sorted(set(entries))
        if len(entries) > MAX_SHARES:
            raise ArtifactError(400, f"Share with at most {MAX_SHARES} people or groups.")
        if not entries:
            raise ArtifactError(400, "Add at least one person or group.")
    now = _now()
    with _engine(hub_dir).begin() as c:
        c.execute(text("DELETE FROM hz_artifact_shares WHERE artifact_id=:a"), {"a": art.id})
        for kind, principal in entries:
            account = _account_of(hub_dir, principal) if kind == "user" else None
            c.execute(text("INSERT INTO hz_artifact_shares (artifact_id, kind, principal, "
                           "account, added_by, added) VALUES (:a, :k, :p, :acc, :b, :t)"),
                      {"a": art.id, "k": kind, "p": principal, "acc": account, "b": actor,
                       "t": now})
        # Leaving "anyone with the link" ends every link at once.
        c.execute(text("UPDATE hz_artifact_links SET revoked=:t "
                       "WHERE artifact_id=:a AND revoked IS NULL"), {"a": art.id, "t": now})
        c.execute(text("UPDATE hz_artifacts SET audience=:au, updated=:t WHERE id=:a"),
                  {"au": audience, "t": now, "a": art.id})
    _audit(hub_dir, art.hub, "artifact_audience", f"{art.id}:{audience}", actor)


def can_create_link(hub_dir, art: Artifact, subject: str) -> bool:
    try:
        return bool(_store(hub_dir).can(subject, art.hub, PUBLIC_LINK_PERMISSION))
    except Exception:  # noqa: BLE001 — fail closed
        log.exception("artifacts: permission check failed")
        return False


def link_days(requested=None) -> int:
    try:
        default = int(os.environ.get("HUBZOID_ARTIFACT_LINK_DAYS") or DEFAULT_LINK_DAYS)
    except ValueError:
        default = DEFAULT_LINK_DAYS
    try:
        days = int(requested) if requested is not None else default
    except (TypeError, ValueError):
        raise ArtifactError(400, "Link lifetime must be a whole number of days.")
    return max(1, min(days, MAX_LINK_DAYS))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_link(hub_dir, art: Artifact | None, actor: str, *, days=None) -> dict:
    """A new public link for `art` (any earlier link stops working). Only the
    owner, and only while they hold `share_public_links` in the hub. The token
    is returned once and stored only as a hash."""
    art = _require_owner(hub_dir, art, actor)
    if not can_create_link(hub_dir, art, actor):
        raise ArtifactError(403, "You do not have permission to create public links in this "
                                 "agent. Ask an administrator for 'Share reports by public link'.")
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = now + link_days(days) * 86400
    with _engine(hub_dir).begin() as c:
        c.execute(text("UPDATE hz_artifact_links SET revoked=:t "
                       "WHERE artifact_id=:a AND revoked IS NULL"), {"a": art.id, "t": now})
        c.execute(text("DELETE FROM hz_artifact_shares WHERE artifact_id=:a"), {"a": art.id})
        c.execute(text("INSERT INTO hz_artifact_links (id, artifact_id, token_hash, created, "
                       "created_by, expires) VALUES (:i, :a, :h, :t, :b, :e)"),
                  {"i": "l" + secrets.token_urlsafe(12), "a": art.id, "h": _token_hash(token),
                   "t": now, "b": actor, "e": expires})
        c.execute(text("UPDATE hz_artifacts SET audience='link', updated=:t WHERE id=:a"),
                  {"t": now, "a": art.id})
    _audit(hub_dir, art.hub, "artifact_public_link", art.id, actor)
    return {"url": f"{public_base_url(hub_dir)}/portal/p/#{token}", "expires": expires}


def revoke_links(hub_dir, art: Artifact | None, actor: str) -> None:
    """End every public link for `art` now; it becomes owner-only."""
    art = _require_owner(hub_dir, art, actor)
    now = _now()
    with _engine(hub_dir).begin() as c:
        c.execute(text("UPDATE hz_artifact_links SET revoked=:t "
                       "WHERE artifact_id=:a AND revoked IS NULL"), {"a": art.id, "t": now})
        c.execute(text("UPDATE hz_artifacts SET audience='owner', updated=:t "
                       "WHERE id=:a AND audience='link'"), {"t": now, "a": art.id})
    _audit(hub_dir, art.hub, "artifact_link_revoked", art.id, actor)


def active_link(hub_dir, artifact_id: str) -> dict | None:
    with _engine(hub_dir).connect() as c:
        r = c.execute(text("SELECT id, created, expires FROM hz_artifact_links "
                           "WHERE artifact_id=:a AND revoked IS NULL AND expires > :t "
                           "ORDER BY created DESC"), {"a": artifact_id, "t": _now()}).fetchone()
    return dict(id=r[0], created=r[1], expires=r[2]) if r else None


def open_link(hub_dir, token: str) -> tuple[Artifact, str] | None:
    """(artifact, link id) for a live public link, or None. Checks expiry,
    revocation, the audience and the owner's current permission every time."""
    if not isinstance(token, str) or not (20 <= len(token) <= 200):
        return None
    with _engine(hub_dir).connect() as c:
        r = c.execute(text("SELECT id, artifact_id, expires, revoked FROM hz_artifact_links "
                           "WHERE token_hash=:h"), {"h": _token_hash(token)}).fetchone()
    if r is None or r[3] is not None or r[2] <= _now():
        return None
    art = get(hub_dir, r[1])
    if (art is None or art.audience != "link" or not _owner_current(hub_dir, art)
            or not can_create_link(hub_dir, art, art.owner)):
        return None
    return art, r[0]


def link_live(hub_dir, artifact_id: str, link_id: str) -> Artifact | None:
    """The artifact if `link_id` is still a live public link for it."""
    with _engine(hub_dir).connect() as c:
        r = c.execute(text("SELECT artifact_id, expires, revoked FROM hz_artifact_links "
                           "WHERE id=:i"), {"i": link_id}).fetchone()
    if r is None or r[0] != artifact_id or r[2] is not None or r[1] <= _now():
        return None
    art = get(hub_dir, artifact_id)
    if (art is None or art.audience != "link" or not _owner_current(hub_dir, art)
            or not can_create_link(hub_dir, art, art.owner)):
        return None
    return art


def delete(hub_dir, art: Artifact | None, actor: str) -> None:
    """Remove the stored file and end every way of opening it. Only the owner."""
    art = _require_owner(hub_dir, art, actor)
    now = _now()
    with _engine(hub_dir).begin() as c:
        c.execute(text("UPDATE hz_artifact_links SET revoked=:t "
                       "WHERE artifact_id=:a AND revoked IS NULL"), {"a": art.id, "t": now})
        c.execute(text("DELETE FROM hz_artifact_shares WHERE artifact_id=:a"), {"a": art.id})
        c.execute(text("UPDATE hz_artifacts SET deleted=:t, updated=:t, audience='owner' "
                       "WHERE id=:a"), {"t": now, "a": art.id})
    try:
        folder = content_path(hub_dir, art).parent
        shutil.rmtree(folder, ignore_errors=True)
    except ArtifactError:
        pass
    _audit(hub_dir, art.hub, "artifact_deleted", art.id, actor)


# ---- short-lived view cookie for public links ------------------------------------


def _view_key(hub_dir) -> bytes:
    """A deployment-wide key (in the shared store) for the short-lived cookie a
    public-link page uses to fetch content, so any bridge can check it."""
    eng = _engine(hub_dir)
    with eng.begin() as c:
        c.execute(text("INSERT INTO hz_meta (k, v) VALUES ('artifact_view_key', :v) "
                       "ON CONFLICT (k) DO NOTHING"), {"v": json.dumps(secrets.token_hex(32))})
        raw = c.execute(text("SELECT v FROM hz_meta WHERE k='artifact_view_key'")).scalar()
    return json.loads(raw).encode()


VIEW_SECONDS = 900


def view_cookie(hub_dir, artifact_id: str, link_id: str) -> str:
    exp = int(_now()) + VIEW_SECONDS
    msg = f"{artifact_id}.{link_id}.{exp}"
    sig = hmac.new(_view_key(hub_dir), msg.encode(), hashlib.sha256).hexdigest()[:40]
    return f"{link_id}.{exp}.{sig}"


def check_view_cookie(hub_dir, artifact_id: str, value: str | None) -> Artifact | None:
    try:
        link_id, exp, sig = (value or "").split(".")
        if int(exp) < _now():
            return None
    except ValueError:
        return None
    msg = f"{artifact_id}.{link_id}.{exp}"
    want = hmac.new(_view_key(hub_dir), msg.encode(), hashlib.sha256).hexdigest()[:40]
    if not hmac.compare_digest(want, sig):
        return None
    return link_live(hub_dir, artifact_id, link_id)


# ---- previews -------------------------------------------------------------------

PREVIEW_ROWS = 500
PREVIEW_COLS = 60
PREVIEW_TEXT_BYTES = 1_000_000


def preview(hub_dir, art: Artifact) -> dict | None:
    """Data for the viewer to render safely (as text nodes), for CSV and text."""
    if art.kind == "csv":
        import csv
        import io

        raw = content_path(hub_dir, art).read_bytes()[: 4 * PREVIEW_TEXT_BYTES]
        body = raw.decode("utf-8-sig", errors="replace")
        delimiter = "\t" if art.filename.lower().endswith(".tsv") else ","
        rows = []
        for i, row in enumerate(csv.reader(io.StringIO(body), delimiter=delimiter)):
            if i >= PREVIEW_ROWS:
                break
            rows.append([cell[:500] for cell in row[:PREVIEW_COLS]])
        return {"kind": "csv", "rows": rows, "truncated": i >= PREVIEW_ROWS if rows else False}
    if art.kind == "text":
        raw = content_path(hub_dir, art).read_bytes()
        return {"kind": "text", "text": raw[:PREVIEW_TEXT_BYTES].decode("utf-8", errors="replace"),
                "truncated": len(raw) > PREVIEW_TEXT_BYTES}
    return None
