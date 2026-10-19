"""Setting up a GitHub connection: the customer's own App through GitHub's
manifest flow, an App the customer already has, or a local personal token.

The manifest flow (https://docs.github.com/en/apps/sharing-github-apps/
registering-a-github-app-from-a-manifest):

1. ``start`` records a one-time setup with a random state (only its hash is
   kept) and returns the hub's registration page URL.
2. The page posts the manifest to GitHub, under the customer's account or
   organization. The customer creates the App there. GitHub owns that step.
3. GitHub returns to ``callback`` with a temporary code. The hub converts it
   (``POST /app-manifests/{code}/conversions``), checks the App's owner is the
   one requested, and stores the private key and webhook secret in the secret
   store. Neither the browser, the CLI nor a model ever sees them.
4. The customer installs the App on chosen repositories. GitHub returns to
   ``installed``; the installation id in that URL is only a hint: the hub
   reads the installation back with the App's own credentials and checks its
   owner before saving it.

Foreman gets its App from Vercel Connect, one App per deployment, owned by
the deployer (README.md:37-50). This is the self-hosted equivalent of that
step, with the same ownership.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path

from .. import config as config_lib
from .. import store
from ..doctor import webhook_url
from ..errors import ChannelError
from ..protocol import Check
from . import client as gh

log = logging.getLogger("hubzoid.channels.github.setup")

SETUP_SECONDS = 15 * 60
PERMISSIONS = {"metadata": "read", "contents": "write", "issues": "write", "pull_requests": "write",
               "checks": "read", "actions": "read"}
EVENTS = ["issues", "issue_comment", "pull_request", "pull_request_review_comment", "check_suite", "check_run",
          "workflow_run"]


def _hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _public() -> str:
    base = (os.environ.get("HUBZOID_PUBLIC_URL") or "").rstrip("/")
    if not base:
        raise ChannelError("public_url_required", "Set HUBZOID_PUBLIC_URL to the hub's HTTPS address first: GitHub "
                           "sends deliveries and returns from setup there.", status=422)
    return base


def _base(conn) -> str:
    return f"{_public()}/api/hubs/{conn.hub}/channels/{conn.id}"


def _put_secret(conn, name: str, value: str, actor: str) -> str:
    from ...secretstore import store as secret_store

    secret_store.put(conn.hub_dir, scope="channel", target=f"{conn.hub}/{conn.id}", name=name, value=value,
                     actor=actor)
    return f"secret:channel/{conn.hub}/{conn.id}/{name}"


def manifest(conn, name: str) -> dict:
    base = _base(conn)
    return {"name": name[:34], "url": _public(), "public": False,
            "hook_attributes": {"url": webhook_url(conn), "active": True},
            "redirect_url": f"{base}/github/callback", "setup_url": f"{base}/github/installed",
            "setup_on_update": True, "default_permissions": dict(PERMISSIONS), "default_events": list(EVENTS)}


def start(conn, *, owner: str, owner_type: str, name: str, actor: str) -> dict:
    if conn.provider != "github":
        raise ChannelError("not_found", "This channel is not a GitHub connection.", status=404)
    if owner_type not in ("user", "organization"):
        raise ChannelError("validation_failed", "owner type is user or organization.", status=422)
    _public()
    state = secrets.token_urlsafe(32)
    setup = {"id": secrets.token_hex(8), "state_hash": _hash(state), "owner": owner, "owner_type": owner_type,
             "name": name, "status": "awaiting_registration", "expires": time.time() + SETUP_SECONDS,
             "actor": actor}
    store.set_connection_state(conn.hub_dir, conn.hub, conn.id, github_setup=setup)
    config_lib.audit(conn.hub_dir, actor, "github_setup_start", f"{conn.id}:{owner}")
    return {"setup_id": setup["id"], "state": "awaiting_registration",
            "registration_url": f"{_base(conn)}/github/register?setup={setup['id']}&state={state}",
            "expires_in_seconds": SETUP_SECONDS}


def _setup(conn, state: str | None, *, want: tuple[str, ...], current: dict | None = None) -> dict:
    setup = current if current is not None else ((conn.state or {}).get("github_setup") or {})
    if not setup or not state or not secrets.compare_digest(setup.get("state_hash", ""), _hash(state)):
        raise ChannelError("setup_state_invalid", "This setup link is not valid. Start a new setup.", status=400)
    if time.time() > float(setup.get("expires") or 0):
        raise ChannelError("setup_expired", "This setup expired. Start a new one.", status=410)
    if setup.get("status") not in want:
        if setup.get("status") == "converting":
            raise ChannelError("setup_conversion_unknown", "A previous attempt may have created the App before it "
                               "was saved. Check your GitHub Apps: import that App, or delete it, then start again.",
                               status=409)
        raise ChannelError("setup_state_invalid", f"This setup is {setup.get('status')}.", status=409)
    return setup


def registration_page(conn, setup_id: str, state: str) -> str:
    """The page that posts the manifest to GitHub. It holds no credential."""
    import html

    setup = _setup(conn, state, want=("awaiting_registration",))
    if setup["id"] != setup_id:
        raise ChannelError("setup_state_invalid", "This setup link is not valid. Start a new setup.", status=400)
    web = gh.web_url()
    action = (f"{web}/organizations/{setup['owner']}/settings/apps/new" if setup["owner_type"] == "organization"
              else f"{web}/settings/apps/new")
    data = html.escape(json.dumps(manifest(conn, setup["name"])), quote=True)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Create the GitHub App</title>
<style>body{{font:16px system-ui,sans-serif;max-width:640px;margin:48px auto;padding:0 16px;color:#1b1f24}}
code{{background:#f3f4f6;padding:2px 4px;border-radius:4px}}button{{font:inherit;padding:10px 16px}}</style></head>
<body><h1>Create your GitHub App</h1>
<p>The App will be owned by <b>{html.escape(setup['owner'])}</b> ({html.escape(setup['owner_type'])}).
It asks for: metadata read, contents write, issues write, pull requests write, checks read and actions read.
It sends events to <code>{html.escape(webhook_url(conn) or '')}</code>.</p>
<p>GitHub shows the details and creates the App in your account. The App's private key and webhook secret go
straight to this hub's secret store. Nobody sees them here.</p>
<form id="f" method="post" action="{html.escape(action)}?state={html.escape(state)}">
<input type="hidden" name="manifest" value="{data}"><button type="submit">Continue to GitHub</button></form>
</body></html>"""


def callback(conn, code: str, state: str) -> str:
    """GitHub returned with a temporary code. Convert it, check the owner,
    store the secrets. Returns where to send the browser next."""
    def take(current: dict):
        # Check and mark the attempt in one locked step, so two callbacks for
        # the same link cannot both convert, and a late duplicate cannot move
        # a finished setup back.
        found = _setup(conn, state, want=("awaiting_registration",), current=current.get("github_setup") or {})
        return {"github_setup": {**found, "status": "converting"}}, found

    setup = store.swap_connection_state(conn.hub_dir, conn.hub, conn.id, take)
    try:
        resp = gh._gh(None, writes=True).request("POST", f"/app-manifests/{code}/conversions")
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - the App may exist now: say so, never retry blindly
        log.warning("channel %s: manifest conversion failed or its answer was lost", conn.id)
        raise ChannelError("setup_conversion_unknown", "GitHub did not confirm the new App. Check your GitHub Apps "
                           "before starting again.", status=502) from exc
    owner = data.get("owner") or {}
    if (owner.get("login") or "").lower() != setup["owner"].lower():
        store.set_connection_state(conn.hub_dir, conn.hub, conn.id, github_setup={**setup, "status": "failed",
                                                                                   "error": "app_owner_mismatch"})
        raise ChannelError("app_owner_mismatch", f"The App was created under {owner.get('login')}, not "
                           f"{setup['owner']}. Delete it on GitHub and start again.", status=409)
    actor = setup.get("actor") or "github-setup"
    _put_secret(conn, "private-key", data["pem"], actor)
    _put_secret(conn, "webhook-secret", data["webhook_secret"], actor)
    if data.get("client_secret"):
        _put_secret(conn, "client-secret", data["client_secret"], actor)
    store.set_connection_state(conn.hub_dir, conn.hub, conn.id, github_slug=data.get("slug"), github_setup={
        **setup, "status": "awaiting_installation", "app_id": data["id"], "slug": data.get("slug"),
        "owner_id": owner.get("id"), "html_url": data.get("html_url")})
    config_lib.audit(conn.hub_dir, actor, "github_app_registered", f"{conn.id}:{data['id']}")
    return f"{gh.web_url()}/apps/{data.get('slug')}/installations/new?state={state}"


def installed(conn, installation_id: int, state: str | None, *, actor: str | None = None) -> dict:
    """GitHub says the App was installed. Read the installation back with the
    App's own credentials and check its owner before using it."""
    setup = (conn.state or {}).get("github_setup") or {}
    if state is not None:
        setup = _setup(conn, state, want=("awaiting_installation", "ready"))
    elif setup.get("status") not in ("awaiting_installation", "ready"):
        raise ChannelError("setup_state_invalid", "There is no App waiting for installation.", status=409)
    from ...secretstore import resolve

    pem = resolve(f"secret:channel/{conn.hub}/{conn.id}/private-key", conn.hub_dir)
    app = gh.app_client_raw(setup["app_id"], pem)
    try:
        inst = app.request("GET", f"/app/installations/{int(installation_id)}").json()
    except Exception as exc:  # noqa: BLE001
        raise ChannelError("installation_pending", "GitHub does not show that installation yet. Approve the "
                           "installation (an organization owner may need to), then verify again.", status=409) from exc
    account = inst.get("account") or {}
    if str(account.get("id")) != str(setup.get("owner_id")) or int(inst.get("app_id") or 0) != int(setup["app_id"]):
        raise ChannelError("installation_mismatch", "That installation belongs to another account or App.",
                           status=409)
    data = config_lib.export(conn.hub_dir, conn.id)
    cfg = json.loads(json.dumps(data["config"]))
    pc = cfg["provider_config"]
    pc.update({"mode": "app", "app_id": int(setup["app_id"]), "installation_id": int(installation_id),
               "owner": {"type": setup["owner_type"], "login": account.get("login") or setup["owner"],
                         "id": account.get("id")},
               "private_key_ref": f"secret:channel/{conn.hub}/{conn.id}/private-key",
               "webhook_secret_ref": f"secret:channel/{conn.hub}/{conn.id}/webhook-secret",
               "token_ref": None})
    out = config_lib.apply(conn.hub_dir, cfg, expected_revision=data["revision"],
                           actor=actor or setup.get("actor") or "github-setup", expected_id=conn.id)
    store.set_connection_state(conn.hub_dir, conn.hub, conn.id,
                               github_setup={**setup, "status": "ready", "installation_id": int(installation_id)},
                               github_permissions=inst.get("permissions") or {})
    return {"state": "ready", "installation_id": int(installation_id), "revision": out["revision"],
            "owner": account.get("login")}


def status(conn) -> dict:
    setup = (conn.state or {}).get("github_setup") or {}
    return {"state": setup.get("status") or "none", "app_id": setup.get("app_id"), "slug": setup.get("slug"),
            "installation_url": f"{gh.web_url()}/apps/{setup.get('slug')}/installations/new"
            if setup.get("slug") and setup.get("status") == "awaiting_installation" else None,
            "error": setup.get("error"), "revision": conn.revision}


def cancel(conn, actor: str) -> None:
    store.set_connection_state(conn.hub_dir, conn.hub, conn.id, github_setup={})
    config_lib.audit(conn.hub_dir, actor, "github_setup_cancel", conn.id)


def import_app(conn, *, app_id: int, installation_id: int, owner_id: int, actor: str) -> dict:
    """An App the customer already has. Its private key must already be in
    secret:channel/<hub>/<id>/private-key and its webhook secret in
    .../webhook-secret."""
    setup = {"id": "import", "status": "awaiting_installation", "app_id": int(app_id), "owner_id": owner_id,
             "owner_type": conn.settings.owner.type, "owner": conn.settings.owner.login, "actor": actor,
             "state_hash": "", "expires": time.time() + SETUP_SECONDS}
    store.set_connection_state(conn.hub_dir, conn.hub, conn.id, github_setup=setup)
    conn = config_lib.active(conn.hub_dir, conn.id)
    return installed(conn, installation_id, None, actor=actor)


def repositories(conn) -> list[dict]:
    """The repositories the installation (or the token) can reach."""
    try:
        if conn.settings.mode == "app":
            items = gh.installation_client(conn).request("GET", "/installation/repositories",
                                                         params={"per_page": 100}).json().get("repositories") or []
        else:
            items = gh.for_repo(conn, 0).request("GET", "/user/repos", params={"per_page": 100}).json() or []
    except ChannelError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ChannelError("provider_unavailable", "GitHub did not list the repositories.", status=503) from exc
    return [{"id": r["id"], "full_name": r["full_name"], "private": r.get("private")} for r in items]


def resolve_names(conn, names: list[str]) -> list[dict]:
    """Repository names to their immutable ids, through the installation."""
    known = {r["full_name"].lower(): r for r in repositories(conn)}
    out = []
    for name in names:
        hit = known.get(name.lower())
        if hit is None:
            raise ChannelError("repository_not_installed", f"{name} is not reachable by this App installation. Add "
                               "it to the installation on GitHub.", status=409)
        out.append({"id": hit["id"], "full_name": hit["full_name"]})
    return out


def configure(conn, *, expected_revision: int, actor: str, repositories_: list[str] | None = None,
              sets: dict | None = None, any_secret: bool = True) -> dict:
    data = json.loads(json.dumps(config_lib.export(conn.hub_dir, conn.id)["config"]))
    if repositories_:
        data["provider_config"]["repositories"] = resolve_names(conn, repositories_)
    for key, value in (sets or {}).items():
        target, parts = data, key.split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return config_lib.apply(conn.hub_dir, data, expected_revision=expected_revision, actor=actor,
                            expected_id=conn.id, any_secret=any_secret)


def connect_pat(hub_dir, cid: str, *, token: str, repositories_: list[str], actor: str,
                expected_revision) -> dict:
    """Local only: a fine-grained personal token. The replies use the
    token owner's account."""
    hub = config_lib.hub_of(hub_dir)
    try:
        me = gh.pat_client_raw(token).request("GET", "/user").json()
    except Exception as exc:  # noqa: BLE001
        raise ChannelError("credential_rejected", "GitHub refused the token.", status=401) from exc
    web = gh.pat_client_raw(token)
    repos = []
    for name in repositories_:
        try:
            r = web.request("GET", f"/repos/{name}").json()
        except Exception as exc:  # noqa: BLE001
            raise ChannelError("repository_not_installed", f"The token cannot read {name}.", status=409) from exc
        repos.append({"id": r["id"], "full_name": r["full_name"]})
    from ...secretstore import store as secret_store

    secret_store.put(Path(hub_dir), scope="channel", target=f"{hub}/{cid}", name="token", value=token, actor=actor)
    existing = None
    try:
        existing = config_lib.export(hub_dir, cid)
    except ChannelError:
        pass
    data = json.loads(json.dumps(existing["config"])) if existing else {
        "version": 1, "id": cid, "provider": "github", "enabled": False, "agent": "main", "allowed_groups": [],
        "provider_config": {}}
    data["provider_config"] = {**{k: v for k, v in data.get("provider_config", {}).items()
                                  if k not in ("app_id", "installation_id", "private_key_ref")},
                               "mode": "pat", "owner": {"type": "user", "login": me["login"], "id": me["id"]},
                               "repositories": repos, "token_ref": f"secret:channel/{hub}/{cid}/token"}
    return config_lib.apply(hub_dir, data, expected_revision=existing["revision"] if existing else "absent",
                            actor=actor, expected_id=cid)


def manual_intake(conn, *, url: str, task: str, key: str | None = None) -> dict:
    """Local token mode: start work from an issue URL, acting as the token's
    owner. Never accepts a caller-supplied sender."""
    import re

    from .. import admission
    from ..models import ExternalActor, InboundEvent, Origin, Verified

    if conn.settings.mode != "pat":
        raise ChannelError("pat_only", "Manual intake is for local token mode. App mode receives webhooks.",
                           status=422)
    m = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/(issues|pull)/(\d+)/?", url.strip())
    if not m:
        raise ChannelError("validation_failed", "Use an issue or pull request URL on github.com.", status=422)
    full, kind, number = m.group(1), m.group(2), int(m.group(3))
    repo = next((r for r in conn.settings.repositories if r.full_name.lower() == full.lower()), None)
    if repo is None:
        raise ChannelError("repository_not_configured", f"{full} is not in this connection's repositories.",
                           status=422)
    web = gh.for_repo(conn, repo.id)
    me = web.request("GET", "/user").json()
    issue = web.request("GET", f"/repos/{repo.full_name}/issues/{number}").json()
    did = key or f"manual:{secrets.token_hex(8)}"
    origin = Origin("github", conn.adapter.workspace(conn), f"repo:{repo.id}:issue:{number}",
                    {"repository_id": repo.id, "full_name": repo.full_name, "number": number},
                    url=issue.get("html_url") or url)
    event = InboundEvent(semantic_key=f"manual:{did}", kind="manual", origin=origin,
                         actor=ExternalActor(str(me["id"]), me["login"], False, {"source": "token_owner"}),
                         text=task, trigger=True, message_id=did,
                         data={"route": "plan", "provider_trusted": True, "title": issue.get("title"),
                               "is_pull_request": kind == "pull"})
    verified = Verified(delivery_id=did, workspace_id=origin.workspace_id, method="pat-manual",
                        body=json.dumps({"url": url, "task": task}).encode())
    out = admission.admit(conn, verified, [event], raw=verified.body)
    row = out.events[0]
    return {"delivery_id": row["id"], "thread_id": row.get("thread_id"), "status": row["status"]}


def doctor_checks(conn, inputs: dict) -> list[Check]:
    cfg = conn.settings
    out: list[Check] = []
    if cfg.mode == "pat":
        public = (os.environ.get("HUBZOID_PUBLIC_URL") or "")
        if public and not any(h in public for h in ("localhost", "127.0.0.1")) \
                and os.environ.get("HUBZOID_GITHUB_PAT_TUNNEL") != "1":
            out.append(Check("github:pat", "fail", "A personal token is for a local hub only.", "pat_remote_forbidden",
                             "Use App mode for a hosted hub, or set HUBZOID_GITHUB_PAT_TUNNEL=1 for your own tunnel."))
        try:
            me = gh.for_repo(conn, 0).request("GET", "/user").json()
            out.append(Check("github:token", "ok", f"The token belongs to {me.get('login')}. Replies use that account."))
        except ChannelError:
            return out
        except Exception:  # noqa: BLE001
            return out + [Check("github:token", "fail", "GitHub refused the token.", "credential_rejected",
                                "Set a new token with hubzoid secrets set.")]
    else:
        if not (cfg.app_id and cfg.installation_id):
            return [Check("github:app", "warn", "The App is not set up yet.", "installation_pending",
                          "Run hubzoid channels github connect, or import-app for an existing App.")]
        try:
            app = gh.app_client(conn).request("GET", "/app").json()
            inst = gh.app_client(conn).request("GET", f"/app/installations/{cfg.installation_id}").json()
        except ChannelError:
            return out
        except Exception:  # noqa: BLE001
            return [Check("github:app", "fail", "GitHub refused the App's private key, or is unreachable.",
                          "credential_rejected", "Restore the private key in the secret store, then run doctor.")]
        owner_ok = cfg.owner.id is None or str((inst.get("account") or {}).get("id")) == str(cfg.owner.id)
        out.append(Check("github:app", "ok", f"App {app.get('slug')} ({app.get('id')}), owned by "
                         f"{(app.get('owner') or {}).get('login')}."))
        out.append(Check("github:installation", "ok" if owner_ok else "fail",
                         f"Installation {cfg.installation_id} on {(inst.get('account') or {}).get('login')}.",
                         "" if owner_ok else "installation_mismatch"))
        granted = inst.get("permissions") or {}
        missing = [k for k, v in PERMISSIONS.items() if granted.get(k) not in (v, "write" if v == "read" else v)]
        out.append(Check("github:permissions", "ok" if not missing else "fail",
                         "All needed permissions are granted." if not missing else
                         "Missing: " + ", ".join(f"{k} {PERMISSIONS[k]}" for k in missing),
                         "" if not missing else "permission_missing",
                         "" if not missing else "Accept the App's updated permissions on GitHub."))
        try:
            reachable = {r["id"] for r in repositories(conn)}
        except ChannelError:
            reachable = set()
        for r in cfg.repositories:
            ok = r.id in reachable
            out.append(Check(f"github:repo:{r.full_name}", "ok" if ok else "fail",
                             f"{r.full_name}: " + ("installed." if ok else "not reachable by the installation."),
                             "" if ok else "repository_not_installed",
                             "" if ok else "Add the repository to the App installation on GitHub."))
        if app.get("slug") and (conn.state or {}).get("github_slug") != app["slug"]:
            store.set_connection_state(conn.hub_dir, conn.hub, conn.id, github_slug=app["slug"])
    out.append(Check("github:mention", "ok", f"People mention @{conn.adapter.bot_login(conn)} to ask."))
    return out
