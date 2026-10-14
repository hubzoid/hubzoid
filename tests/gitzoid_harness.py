"""Run the GitZoid template's workflows and tools in tests, on Hubzoid's own
parts: workflows through `hubzoid.workflows.testing.run_local` (the real run
context, input checks and email-list rules, with pages and email captured),
people and grants in the hub's real access store, and tools through the real
restricted-tool guard. No DBOS, no network and no model.

`scripted_llm` stands in for the model: a reviewer that reports exactly the
planted bugs in the sample (so tests check GitZoid's code, not a model).
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import yaml

import hubzoid

TEMPLATE = Path(hubzoid.__file__).resolve().parent / "templates" / "gitzoid"
BOT = "gitzoid@example.com"            # the automation account team runs act as
PERSON = "maya@example.com"            # who asks, in Run for me and the tools
PEOPLE = (BOT, PERSON, "ana@example.com", "boss@example.com")
TEAM_TRIGGERS = ("schedule", "operator", "cli", "webhook", "update")
# Settings from the shell that would point the hub at another deployment.
HUB_ENV = ("WEBUI_AUTH", "HUBZOID_DEPLOYMENT", "DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "HUBZOID_PUBLIC_URL",
           "WEBUI_URL", "HUBZOID_SHARE_DOMAINS", "HUBZOID_EMAIL_DELIVERY", "HUBZOID_SMTP_HOST",
           "HUBZOID_RESTRICTED_SURFACES", "HUBZOID_WORKFLOW_USER", "GITHUB_TOKEN",
           "GITHUB_APP_ID", "GITHUB_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY")


def isolate(monkeypatch) -> None:
    for k in HUB_ENV:
        monkeypatch.delenv(k, raising=False)


def make_hub(tmp: Path, name: str = "gitzoid-hub") -> Path:
    """A copy of the template with its people in the hub's access store: the
    automation account and three people, each able to use the agent; the
    people also hold `gitzoid` (the chat and MCP tools)."""
    from hubzoid.access import store_for

    hub_dir = Path(tmp) / name
    shutil.copytree(TEMPLATE, hub_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".hubzoid"))
    settings_path = hub_dir / "workflows" / "settings.yaml"
    settings = yaml.safe_load(settings_path.read_text())
    for list_name in settings.get("email_lists", {}):
        settings["email_lists"][list_name] = ["owner@example.com"]
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))
    gs = store_for(hub_dir)
    for email in PEOPLE:
        gs.upsert_identity(email=email, owui_id="id-" + email)
        gs.grant(email, name, "use_hub", actor="test")
        if email != BOT:
            gs.grant(email, name, "gitzoid", actor="test")
    return hub_dir


def grant(hub_dir: Path, email: str, *permissions: str) -> None:
    from hubzoid.access import store_for

    for p in permissions:
        store_for(hub_dir).grant(email, Path(hub_dir).name, p, actor="test")


def set_attrs(hub_dir: Path, email: str, **attrs) -> None:
    """Access attributes, as an admin sets them in the Console (always text)."""
    from hubzoid.access import store_for

    for k, v in attrs.items():
        store_for(hub_dir).set_attr(Path(hub_dir).name, email, k, ",".join(v) if isinstance(v, (list, tuple)) else str(v))


# ------------------------------------------------------------- workflows

def definitions(hub_dir: Path) -> dict:
    """The hub's workflows as Hubzoid reads them (decorators checked, steps plain)."""
    from hubzoid.workflows.runtime import load_definitions

    return {d.name: d for d in load_definitions(hub_dir)}


def wf(hub_dir: Path, name: str):
    return definitions(hub_dir)[name].fn


def flow(hub_dir: Path):
    """The review flow module both review workflows share (_flow.py)."""
    return wf(hub_dir, "pr_sweep").__globals__["flow"]


def lib(hub_dir: Path):
    return flow(hub_dir).gz


def template_lib():
    """The template's library, loaded in place (pure code: no hub, no fakes).
    No bytecode is written into the template folder."""
    root = TEMPLATE / "workflows" / "gitzoid" / "lib"
    name = "gztest_template_lib"
    if name not in sys.modules:
        before, sys.dont_write_bytecode = sys.dont_write_bytecode, True
        try:
            spec = importlib.util.spec_from_file_location(name, root / "package.py", submodule_search_locations=[str(root)])
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = before
    return sys.modules[name]


def scripted(answer):
    """Hubzoid's model seam (`workflows.configure(llm=...)`) around a test
    function answer(prompt, system, response_model) that returns the JSON as a
    dict, or text. Text is parsed as the hub runtime parses a reply."""
    from hubzoid import structured

    def seam(spec, hub_dir=None, subject=None):
        title = (spec.get("schema") or {}).get("title", "")
        out = answer(spec["prompt"], spec.get("system"), SimpleNamespace(__name__=title) if title else None)
        if isinstance(out, str):
            return {"text": out, "json": structured.extract_json(out) if spec.get("response_format") == "json" else None}
        return {"text": json.dumps(out), "json": out}
    return seam


def real_llm(model: str):
    """The hub's own model call (what the bridge wires), on `model`, for e2e runs."""
    from hubzoid import runtime

    def seam(spec, hub_dir=None, subject=None):
        return runtime.complete_once(hub_dir, {**spec, "model": spec.get("model") or model, "timeout": 600},
                                     subject=subject)
    return seam


@dataclass
class Result:
    value: object
    artifacts: list = field(default_factory=list)
    emails: list = field(default_factory=list)
    calls: list = field(default_factory=list)


@contextlib.contextmanager
def _no_company_links():
    """Publishing with audience="domain" fails as it does on a hub without
    company sign-in and HUBZOID_SHARE_DOMAINS."""
    from hubzoid.artifacts import ArtifactError
    from hubzoid.workflows import context

    real = context.Hub.publish_artifact

    def publish(self, path, **kw):
        if kw.get("audience") == "domain":
            raise ArtifactError(409, "Company links need company sign-in and HUBZOID_SHARE_DOMAINS.")
        return real(self, path, **kw)
    context.Hub.publish_artifact = publish
    try:
        yield
    finally:
        context.Hub.publish_artifact = real


def run_local(fn, *, hub_dir: Path, inputs: dict | None = None, as_user: str | None = None, trigger: str = "tool",
              llm=None, event: dict | None = None, domain_sharing: bool = True) -> Result:
    """Hubzoid's run_local, with the model seam set for the run. Team runs act
    as BOT, others as PERSON, unless `as_user` says otherwise."""
    from hubzoid.workflows import context, testing

    seam = llm or scripted(scripted_llm)
    calls: list = []

    def model(spec, hub_dir=None, subject=None):
        calls.append({"prompt": spec["prompt"], "system": spec.get("system"), "model": spec.get("model"),
                      "response_model": (spec.get("schema") or {}).get("title")})
        return seam(spec, hub_dir=hub_dir, subject=subject)

    user = as_user or (BOT if trigger in TEAM_TRIGGERS else PERSON)
    saved = context._LLM, context._LLM_STEP
    context._LLM, context._LLM_STEP = model, None
    try:
        with contextlib.nullcontext() if domain_sharing else _no_company_links():
            out = testing.run_local(fn, hub_dir=hub_dir, inputs=inputs, as_user=user, trigger=trigger, event=event)
    finally:
        context._LLM, context._LLM_STEP = saved
    links = {f"local{n}": a for n, a in enumerate(out.artifacts, 1)}
    artifacts = [{**a, "html": Path(a["path"]).read_text()} for a in out.artifacts]
    emails = []
    from hubzoid.email_delivery import MAX_HTML, MAX_LIST_HTML_TEXT, _html_text
    for e in out.emails:
        body = e.get("body") or ""
        if e.get("body_format") == "html":   # Hubzoid's html list email limits
            assert len(body) <= MAX_HTML and len(_html_text(body)) <= MAX_LIST_HTML_TEXT, "html email too long"
        else:
            assert len(body) <= 2000, "Hubzoid email bodies are at most 2,000 characters"
        emails.append({**e, "list": e.get("email_list"),
                       "links": [_url(links.get(i), hub_dir) for i in e.get("artifacts") or ()]})
    return Result(out.result, artifacts, emails, calls)


def _url(art: dict | None, hub_dir: Path) -> str | None:
    """The link run_local returned for a captured page (see testing.run_local)."""
    if art is None:
        return None
    url = f"local://{Path(hub_dir).name.lower()}/{art.get('key') or 'artifact'}"
    return url + "?company" if art.get("key") and art.get("audience") == "domain" else url


# ------------------------------------------------------------- tools

def tools(hub_dir: Path) -> dict:
    """GitZoid's restricted tools as the hub serves them, behind the access guard."""
    from hubzoid.access import guard

    return guard.apply(Path(hub_dir), {})


def restricted(hub_dir: Path) -> dict:
    """{tool name: (the tool without the guard, the permission it needs)}."""
    from hubzoid.access.loader import load_restricted

    return {t.name: (t, p) for t, p in load_restricted(Path(hub_dir))}


@contextlib.contextmanager
def as_person(email: str = PERSON, surface: str = "web"):
    """A signed-in person asking in chat (or over MCP with surface="mcp")."""
    from hubzoid.access import Identity, identity_scope

    with identity_scope(Identity.make(email, surface=surface)):
        yield


def invoke(tool, **kwargs) -> str:
    import asyncio

    from agents.tool_context import ToolContext
    args = json.dumps(kwargs)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=args)
    return asyncio.run(tool.on_invoke_tool(ctx, args))


# ------------------------------------------------------------- settings

SECTIONS = ("review", "security", "audit", "digest", "reports", "limits")  # merged; other keys are replaced


def settings_doc(hub_dir: Path, override: dict | None = None) -> dict:
    doc = yaml.safe_load((Path(hub_dir) / "workflows" / "settings.yaml").read_text())
    for k, v in (override or {}).items():
        if k in SECTIONS and isinstance(v, dict) and isinstance(doc["gitzoid"].get(k), dict):
            doc["gitzoid"][k] = {**doc["gitzoid"][k], **v}
        else:
            doc["gitzoid"][k] = v
    return doc


def write_settings(hub_dir: Path, override: dict) -> None:
    doc = settings_doc(hub_dir, override)
    (Path(hub_dir) / "workflows" / "settings.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))


# ------------------------------------------------------------- scripted model

def _numbered(prompt: str) -> list[tuple[str, int, str, str]]:
    """(file, line, kind, text) from the numbered diff in a review prompt."""
    m = re.search(r"<diff[^>]*>\n(.*)</diff>", prompt, re.S)
    out, current = [], None
    for raw in (m.group(1) if m else "").splitlines():
        h = re.match(r"^### (\S+) \(", raw)
        if h:
            current = h.group(1)
            continue
        n = re.match(r"^\s*(\d+) ([+ ])(.*)$", raw)
        if n and current:
            out.append((current, int(n.group(1)), n.group(2), n.group(3)))
    return out


def scripted_llm(prompt: str, system: str | None, response_model):
    name = getattr(response_model, "__name__", "")
    if name == "Review":
        lines = _numbered(prompt)
        findings = []
        for i, (path, line, kind, text) in enumerate(lines):
            if kind != "+":
                continue
            if "while True" in text:
                findings.append({"path": path, "line": line, "severity": "high", "confidence": "high", "category": "bug",
                                 "body": "This retries every error forever, so one supplier that keeps failing hangs the nightly sync.",
                                 "rule": "Nothing retries forever"})
            if re.search(r"@bp\.get\(\"/admin", text):
                near = [t for (p, _, _, t) in lines[i + 1:i + 3] if p == path]
                if not any("login_required" in t for t in near):
                    findings.append({"path": path, "line": line, "severity": "high", "confidence": "high",
                                     "category": "security", "rule": "Staff routes need sign-in",
                                     "body": "This admin route has no @login_required, so anyone can download customer emails and addresses."})
            if "execute(" in text or re.search(r"f\"SELECT .*\{", text):
                if re.search(r"f\"SELECT .*\{", text):
                    findings.append({"path": path, "line": line, "severity": "high", "confidence": "high",
                                     "category": "security", "body": "The search text goes into the SQL, so a crafted query can read any table."})
        return {"summary": "Scripted review of the change.", "findings": findings, "suggestions": []}
    if name == "Resolutions":
        items = json.loads(prompt.split("Data: ", 1)[1])
        result = []
        for item in items:
            code = item["current_code"]
            resolved = (("retries" in item["finding"].lower() and "for attempt in range(3)" in code and "while True" not in code)
                        or ("print" in item["finding"].lower() and "print(" not in code))
            result.append({"id": item["id"], "resolved": resolved,
                           "evidence": "for attempt in range(3); log.warning replaces print" if resolved else ""})
        return {"items": result}
    if name == "ChangeEvidence":
        return {"change": "The shown code changes a bounded operation.", "risks": [],
                "tests": "No test execution evidence.", "follow_ups": ["Check the linked diff and run regression tests."]}
    if name == "Verification":
        count = len(re.findall(r"^## Finding \d+", prompt, re.M))
        return {"verdicts": [{"index": i, "verdict": "confirmed", "severity": "high", "reason": "The code shows it."}
                             for i in range(count)]}
    if name == "DependencyReview":
        ids = re.findall(r"^- (\S+) · ", prompt, re.M)
        return {"items": [{"advisory": a, "keep": True, "impact": f"{a} is reachable from the app.", "fix": "Upgrade."}
                          for a in ids]}
    if name == "Profile":
        return {"what_it_is": "A fictional shop's storefront.", "parts": [{"name": "orders", "path": "app/orders.py",
                                                                         "purpose": "Order pages"}],
                "sign_in": "app/auth.py login_required", "data": "SQLite orders", "external_services": [],
                "sensitive_areas": ["app/orders.py", "app/auth.py"]}
    if name == "Digest":
        return {"headline": "Two changes merged.", "for_leadership": ["The sync is more reliable."],
                "technical": ["Sync fixes."], "areas": [{"name": "Sync", "changes": ["Fixed the sync (#90)."]}],
                "watch": [], "poem": ""}
    if name == "ReportNarrative":
        return {"headline": "Pull requests merged as shown.", "highlights": ["Numbers as given."], "watch": []}
    if name == "Audit":
        return {"findings": []}
    raise AssertionError(f"unexpected model call for {name or 'text'}")
