"""Restricted-tool review fixtures, modelled on the owner's review deployment.

Two managed hubs in one deployment, each with fictional read-only restricted
tools (no external calls, no writes) and one unrestricted hub-local tool:

  review-hub      sample_inventory, sample_reports, sample_budget   + word_count
  operations-hub  sample_inventory, sample_reports, sample_incidents + hello

`sample_inventory` and `sample_reports` exist in both hubs under the same
permission name, so a test can prove a grant stays in its own hub. Labels come
from each hub's `identity/permissions.yaml`, as in the review hubs.

Nothing here calls a model or the network. Accounts go through the in-memory
Open WebUI from `tests.test_access_service`.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from agents.tool_context import ToolContext
from starlette.requests import Request

from hubzoid import deployment
import hubzoid.access as access
from hubzoid.access import Identity, identity_scope
from hubzoid.access.accounts import OwuiAccounts
from hubzoid.access.guard import visible
from hubzoid.access.service import AccessService, Actor

REVIEW = "review-hub"
OPERATIONS = "operations-hub"
OWNER = "owner@example.org"  # organization administrator with entry to both hubs
SERVICE = "svc@x.org"  # the Console's Open WebUI service account (FakeOwui's)

# permission -> (tool name, fictional payload). File name = permission.
RESTRICTED = {
    REVIEW: {
        "sample_inventory": ("read_sample_inventory", {"notebooks": 24, "pens": 80}),
        "sample_reports": ("read_sample_report",
                           {"week": "sample week", "requests": 42, "completed": 38}),
        "sample_budget": ("read_sample_budget",
                          {"currency": "USD", "budget": 1200, "spent": 450}),
    },
    OPERATIONS: {
        "sample_inventory": ("read_sample_inventory", {"bolts": 300, "washers": 450}),
        "sample_reports": ("read_sample_report",
                           {"week": "sample week", "orders": 18, "dispatched": 16}),
        "sample_incidents": ("read_sample_incidents", {"incidents": [
            {"id": "DEMO-01", "status": "resolved", "summary": "Fictional printer issue"}]}),
    },
}
UNRESTRICTED = {REVIEW: "word_count", OPERATIONS: "hello"}

_LABELS = {
    "sample_inventory": "View sample inventory",
    "sample_reports": "View sample reports",
    "sample_budget": "View sample budget",
    "sample_incidents": "View sample incidents",
}

_LOCAL = {
    "word_count": '''from __future__ import annotations
from agents import function_tool


@function_tool
def word_count(text: str) -> str:
    """Return a quick count of words, characters, and lines in a string."""
    lines = text.count("\\n") + 1 if text else 0
    return f"{len(text.split())} words, {len(text)} chars, {lines} lines"
''',
    "hello": '''from __future__ import annotations
from agents import function_tool


@function_tool
def hello(name: str = "there") -> str:
    """Return a one-line greeting from the hub."""
    return f"Hello {name}, from your hub."
''',
}


def payload(hub: str, permission: str) -> str:
    """The exact string the fictional tool returns."""
    return json.dumps({"demo": True, "hub": hub, "data": RESTRICTED[hub][permission][1]})


def _restricted_source(hub: str, permission: str) -> str:
    tool, _data = RESTRICTED[hub][permission]
    label = _LABELS[permission].lower()
    return (
        "from __future__ import annotations\n"
        "from agents import function_tool\n\n"
        "@function_tool\n"
        f"def {tool}() -> str:\n"
        f'    """Return fictional {label} for permission testing. No external access or writes."""\n'
        f"    return {payload(hub, permission)!r}\n"
    )


def write_hub(root: Path, hub: str) -> Path:
    path = root / hub
    (path / "restricted").mkdir(parents=True)
    (path / "tools_local").mkdir()
    (path / "identity").mkdir()
    (path / "AGENTS.md").write_text(
        f"---\nname: {hub}\ndescription: Review fixture for restricted tools.\n---\n"
        "Answer briefly using the hub's tools.\n")
    yaml_lines = []
    for permission in RESTRICTED[hub]:
        (path / "restricted" / f"{permission}.py").write_text(_restricted_source(hub, permission))
        yaml_lines += [f"{permission}:", f"  label: {_LABELS[permission]}",
                       "  description: Read fictional sample data for access testing; no external systems",
                       "    or writes."]
    (path / "identity" / "permissions.yaml").write_text("\n".join(yaml_lines) + "\n")
    local = UNRESTRICTED[hub]
    (path / "tools_local" / f"{local}.py").write_text(_LOCAL[local])
    return path


_ENV = ("HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB", "DATABASE_URL", "HUBZOID_DEPLOYMENT",
        "OWUI_INTERNAL_URL", "WEBUI_URL", "HUBZOID_PUBLIC_URL", "HUBZOID_OWUI_DB",
        "HUBZOID_GATEWAY_ADMIN_EMAIL", "HUBZOID_GATEWAY_ADMIN_PASSWORD",
        "HUBZOID_RESTRICTED_SURFACES", "HUBZOID_MANAGEMENT_TOOLS", "HUBZOID_CHANGE_REQUEST_TTL",
        "HUBZOID_PORTAL_DEV", "HUBZOID_PORTAL_DEV_USER")


def reset_access_state(monkeypatch) -> None:
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    from hubzoid import migrations
    from hubzoid.access import audit as auditlib

    access._stores.clear()
    migrations._done.clear()
    auditlib._IMPORTED.clear()


def make_review_deployment(tmp_path: Path, monkeypatch, *, owui=None) -> SimpleNamespace:
    """Both review hubs registered in one deployment on one shared SQLite
    operational store, both managed (authoritative). OWNER is the organization
    administrator and, like the owner provisioned at setup, may enter both hubs.
    No restricted grant exists yet."""
    from tests.test_access_service import FakeOwui

    reset_access_state(monkeypatch)
    hubs = {key: write_hub(tmp_path, key) for key in (REVIEW, OPERATIONS)}
    deployment.save(
        tmp_path / "gateway" / "deployment.json",
        hubs=[dict(key=k, name=k.replace("-", " ").title(), path=str(p), model_id=k)
              for k, p in hubs.items()],
        operational_url=f"sqlite:///{tmp_path / 'gateway' / 'ops.db'}",
        owui_url="http://owui.internal",
        owui_db=str(tmp_path / "gateway" / "webui.db"),
    )
    gs = access.store_for(hubs[REVIEW])
    gs.bootstrap([OWNER])
    for key in hubs:
        gs.set_authoritative(True, hub=key)
        gs.grant(OWNER, key, "use_hub", actor="owner-setup")
    owui = owui or FakeOwui()
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_EMAIL", SERVICE)
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_PASSWORD", "svc-secret")
    directory = OwuiAccounts("http://owui.internal", SERVICE, "svc-secret",
                             transport=httpx.MockTransport(owui))
    return SimpleNamespace(hubs=hubs, gs=gs, owui=owui,
                           svc=AccessService(hubs[REVIEW], accounts=directory))


def actor(subject: str, surface: str = "console") -> Actor:
    return Actor(subject=subject, surface=surface, via="session")


def person(email: str, surface: str = "owui") -> Identity:
    return Identity.make(email, surface=surface)


# ---- the tool level -----------------------------------------------------------

def registry(hub_dir: Path) -> dict:
    """The hub's tools as every runtime builds them: hub-local tools, then the
    restricted ones through the access guard (`access.apply`)."""
    from hubzoid.loaders import tools_local

    return access.apply(hub_dir, tools_local.load_all(hub_dir))


def shown_to(hub_dir: Path, ident: Identity) -> set[str]:
    """Tool names the model is shown for this caller on the Claude and Codex
    runtimes (both filter with `guard.visible` per turn)."""
    with identity_scope(ident):
        return {name for name, tool in registry(hub_dir).items() if visible(tool)}


def call(hub_dir: Path, tool_name: str, ident: Identity, args: dict | None = None) -> str:
    """Invoke a tool directly, as a model naming it would, under `ident`."""
    tool = registry(hub_dir)[tool_name]
    raw = json.dumps(args or {})
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=raw)
    with identity_scope(ident):
        return asyncio.run(tool.on_invoke_tool(ctx, raw))


def chat_request(email: str, account_id: str | None = None) -> Request:
    """The request Open WebUI forwards to the bridge for a signed-in person."""
    headers = [(b"x-openwebui-user-email", email.encode())]
    if account_id:
        headers.append((b"x-openwebui-user-id", account_id.encode()))
    return Request({"type": "http", "headers": headers})
