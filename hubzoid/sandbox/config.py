"""`sandbox.yaml` (the deployment's providers and limits) and sandbox profiles
(`sandboxes/<id>.yaml`, one per kind of workspace).

Both files are optional. Without `sandbox.yaml`, or with `enabled: false`, no
agent gets a sandbox. For a standalone hub the deployment folder is the hub
folder. Example:

```yaml
# sandbox.yaml
version: 1
sandbox:
  enabled: true
  default_provider: local-docker
  providers:
    local-docker:
      kind: docker
```

```yaml
# sandboxes/analysis.yaml
version: 1
description: Python and Node checkout for analysis
image:
  base: hubzoid-default
resources: {cpus: 2, memory_mib: 4096, pids: 1024}
network:
  setup: {mode: allowlist, presets: [registries-v1]}
  agent: {mode: deny_all}
setup:
  command: "uv sync --frozen"
  cwd: repo
```

Agent commands always run offline. Only the profile's own setup command, run
once by Hubzoid before any agent tool, reaches the allowed package registries.
No file holds a secret, and nothing secret is ever put into a sandbox.
Unknown keys, and keys this version cannot enforce yet, are refused with the
file and line.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..work_items.errors import WorkItemError

DEPLOYMENT_FILE = "sandbox.yaml"
PROFILE_DIR = "sandboxes"
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
#: Versioned, so a later release never changes what an existing profile allows.
PRESETS = {
    "registries-v1": (
        "registry.npmjs.org", "registry.yarnpkg.com", "pypi.org", "files.pythonhosted.org",
        "proxy.golang.org", "sum.golang.org", "index.crates.io", "static.crates.io", "crates.io",
        "rubygems.org", "repo.maven.apache.org", "repo1.maven.org",
    ),
}
_SECRET_PREFIXES = ("HUBZOID_", "GIT_", "GH_", "AWS_", "OPENAI_", "ANTHROPIC_")
_SECRET_WORDS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")
_PROXY_VARS = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY"}
#: Keys the specification names that this version cannot enforce yet. They
#: are refused rather than accepted with no effect.
_LATER = {
    "sandbox": {"auto_prepare_remote", "budget"},
    "limits": {"max_jobs_per_sandbox"},
}


def _fail(source, root, keys, message, code="profile_invalid"):
    line = _line(root, keys) if root is not None else None
    where = f"{source}:{line}" if line else source
    raise WorkItemError(code, f"{where}: {message}", status=422, file=source, line=line)


def _line(node, keys) -> int | None:
    for i, key in enumerate(keys):
        if not isinstance(node, yaml.MappingNode):
            return None
        match = next(((k, v) for k, v in node.value if getattr(k, "value", None) == key), None)
        if match is None:
            return None
        if i == len(keys) - 1:
            return match[0].start_mark.line + 1
        node = match[1]
    return None


def _load_yaml(path: Path, source: str):
    text = path.read_text(encoding="utf-8")
    try:
        root = yaml.compose(text) if text.strip() else None
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark else None
        raise WorkItemError("profile_invalid", f"{source}:{line or '?'}: not valid YAML", status=422,
                            file=source, line=line)
    if not isinstance(data or {}, dict):
        _fail(source, root, (), "the file must be a mapping")
    return data or {}, root, hashlib.sha256(text.encode()).hexdigest()


class _Reader:
    """Typed reads of one mapping, failing at the key's own line."""

    def __init__(self, source, root, data: dict, keys=()):
        self.source, self.root, self.data, self.keys = source, root, data, tuple(keys)

    def fail(self, key, message, code="profile_invalid"):
        _fail(self.source, self.root, self.keys + ((key,) if key else ()), message, code)

    def only(self, *allowed, later=()):
        for key in self.data:
            if key in later:
                self.fail(key, f"{key} is not supported in this version yet. Remove it")
            if key not in allowed:
                self.fail(key, f"unknown key {key!r}")

    def child(self, key) -> "_Reader":
        value = self.data.get(key) or {}
        if not isinstance(value, dict):
            self.fail(key, f"{key} must be a mapping")
        return _Reader(self.source, self.root, value, self.keys + (key,))

    def number(self, key, default, low, high, *, step=None, integer=True):
        value = self.data.get(key, default)
        ok = not isinstance(value, bool) and isinstance(value, (int, float) if not integer else int)
        if not ok or not low <= value <= high or (step and round(value / step, 9) % 1):
            unit = f" in steps of {step}" if step else ""
            self.fail(key, f"{key} must be a number from {low} to {high}{unit}")
        return value

    def choice(self, key, default, options):
        value = self.data.get(key, default)
        if value not in options:
            self.fail(key, f"{key} must be one of {', '.join(map(str, options))}")
        return value

    def text(self, key, default=None, *, max_len=4096, required=False):
        value = self.data.get(key, default)
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > max_len:
            self.fail(key, f"{key} must be text of 1 to {max_len} characters")
        return value


# -- deployment ------------------------------------------------------------------

@dataclass(frozen=True)
class Deployment:
    enabled: bool = False
    default_provider: str | None = None
    providers: dict = field(default_factory=dict)
    max_active_per_hub: int = 16
    max_probe_seconds: int = 120
    terminal_seconds: int = 0
    orphan_grace_seconds: int = 600
    reaper_interval_seconds: int = 300
    present: bool = False
    digest: str = ""

    def provider(self, name: str | None) -> tuple[str, dict]:
        name = self.default_provider if name in (None, "default") else name
        if name not in self.providers:
            raise WorkItemError("profile_invalid", f"No sandbox provider named {name!r} in {DEPLOYMENT_FILE}.",
                                status=422)
        return name, self.providers[name]

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "default_provider": self.default_provider,
                "providers": self.providers,
                "limits": {"max_active_per_hub": self.max_active_per_hub,
                           "max_probe_seconds": self.max_probe_seconds},
                "lifecycle_cleanup": {"terminal_seconds": self.terminal_seconds},
                "reaper": {"interval_seconds": self.reaper_interval_seconds,
                           "orphan_grace_seconds": self.orphan_grace_seconds}}


def load_deployment(hub_dir) -> Deployment:
    path = Path(hub_dir) / DEPLOYMENT_FILE
    if not path.is_file():
        return Deployment()
    data, root, digest = _load_yaml(path, DEPLOYMENT_FILE)
    top = _Reader(DEPLOYMENT_FILE, root, data)
    top.only("version", "sandbox")
    if data.get("version") != 1:
        top.fail("version", "version must be 1")
    box = top.child("sandbox")
    box.only("enabled", "default_provider", "providers", "limits", "lifecycle_cleanup", "reaper",
             later=_LATER["sandbox"])
    enabled = box.data.get("enabled", False)
    if not isinstance(enabled, bool):
        box.fail("enabled", "enabled is true or false")
    providers = {}
    plist = box.child("providers")
    if len(plist.data) > 8:
        plist.fail(None, "at most 8 providers")
    for name, raw in plist.data.items():
        if not isinstance(name, str) or not PROFILE_RE.match(name):
            plist.fail(name, "a provider name is a lowercase slug")
        p = plist.child(name)
        kind = p.data.get("kind")
        if kind in ("vercel", "e2b"):
            p.fail("kind", f"{kind} sandboxes need an approved provider account and are not available in "
                   "this version. Use kind: docker", code="provider_not_available")
        if kind != "docker":
            p.fail("kind", "kind must be docker")
        p.only("kind", "endpoint", "runtime", "platform")
        endpoint = p.text("endpoint")
        if endpoint is not None:
            if endpoint.startswith("tcp://"):
                p.fail("endpoint", "a tcp:// Docker endpoint needs TLS, which this version does not configure. "
                       "Use unix:// or ssh://")
            if not endpoint.startswith(("unix://", "ssh://")):
                p.fail("endpoint", "endpoint is unix:///path or ssh://user@host")
        providers[name] = {"kind": "docker", "endpoint": endpoint,
                           "runtime": p.choice("runtime", "runc", ("runc", "runsc")),
                           "platform": p.choice("platform", None, (None, "linux/amd64", "linux/arm64"))}
    default = box.data.get("default_provider")
    if enabled and default not in providers:
        box.fail("default_provider", "default_provider must name one of the providers")
    limits = box.child("limits")
    limits.only("max_active_per_hub", "max_probe_seconds", later=_LATER["limits"])
    cleanup = box.child("lifecycle_cleanup")
    cleanup.only("terminal_seconds")
    reaper = box.child("reaper")
    reaper.only("interval_seconds", "orphan_grace_seconds")
    return Deployment(
        enabled=enabled, default_provider=default, providers=providers,
        max_active_per_hub=limits.number("max_active_per_hub", 16, 1, 256),
        max_probe_seconds=limits.number("max_probe_seconds", 120, 1, 600),
        terminal_seconds=cleanup.number("terminal_seconds", 0, 0, 604800),
        reaper_interval_seconds=reaper.number("interval_seconds", 300, 30, 3600),
        orphan_grace_seconds=reaper.number("orphan_grace_seconds", 600, 60, 3600),
        present=True, digest="sha256:" + digest)


# -- profiles --------------------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    id: str
    description: str | None
    provider: str
    image: dict
    resources: dict
    lifecycle: dict
    network: dict
    setup: dict | None
    isolation: str
    transfer: dict
    env: dict
    source: str
    digest: str

    def setup_hosts(self) -> list[str]:
        setup = self.network["setup"]
        if setup["mode"] != "allowlist":
            return []
        hosts = [h for p in setup["presets"] for h in PRESETS[p]] + list(setup["allow_hosts"])
        return sorted(set(hosts))

    def to_json(self) -> dict:
        return {"id": self.id, "description": self.description, "provider": self.provider, "image": self.image,
                "resources": self.resources, "lifecycle": self.lifecycle, "network": self.network,
                "setup": self.setup, "isolation": self.isolation, "transfer": self.transfer, "env": self.env,
                "source": self.source, "digest": self.digest}


def profile_path(hub_dir, profile_id: str) -> Path:
    return Path(hub_dir) / PROFILE_DIR / f"{profile_id}.yaml"


def load_profile(hub_dir, profile_id: str) -> Profile:
    if not isinstance(profile_id, str) or not PROFILE_RE.match(profile_id):
        raise WorkItemError("profile_invalid", f"A sandbox profile is an id such as analysis, not {profile_id!r}. "
                            f"It names {PROFILE_DIR}/<id>.yaml.", status=422)
    path = profile_path(hub_dir, profile_id)
    source = f"{PROFILE_DIR}/{profile_id}.yaml"
    if not path.is_file():
        raise WorkItemError("profile_missing", f"No sandbox profile {source} in this hub.", status=404)
    data, root, _ = _load_yaml(path, source)
    top = _Reader(source, root, data)
    top.only("version", "description", "provider", "image", "resources", "lifecycle", "network", "setup",
             "isolation", "transfer", "env", "filesystem")
    if data.get("version") != 1:
        top.fail("version", "version must be 1")
    provider = data.get("provider", "default")
    if not isinstance(provider, str) or not (provider == "default" or PROFILE_RE.match(provider)):
        top.fail("provider", "provider is default or the name of a provider in sandbox.yaml")

    image_r = top.child("image")
    image_r.only("base", "ref", later=("dockerfile",))
    if len(image_r.data) != 1:
        top.fail("image", "image has exactly one of base or ref")
    if "base" in image_r.data:
        image = {"base": image_r.choice("base", None, ("hubzoid-default",))}
    else:
        ref = image_r.text("ref", required=True, max_len=512)
        if "@sha256:" not in ref:
            image_r.fail("ref", "an image ref must be pinned by digest (name@sha256:...)")
        image = {"ref": ref}

    res = top.child("resources")
    res.only("cpus", "memory_mib", "disk_mib", "pids", "require_disk_limit")
    resources = {"cpus": res.number("cpus", 2, 0.5, 16, step=0.5, integer=False),
                 "memory_mib": res.number("memory_mib", 4096, 512, 32768),
                 "disk_mib": res.number("disk_mib", 10240, 1024, 102400),
                 "pids": res.number("pids", 1024, 64, 32768),
                 "require_disk_limit": bool(res.data.get("require_disk_limit", False))}

    life = top.child("lifecycle")
    life.only("idle_stop_seconds", "max_lifetime_seconds", "max_job_seconds", "command_timeout_seconds")
    lifecycle = {"idle_stop_seconds": life.number("idle_stop_seconds", 900, 60, 86400),
                 "max_lifetime_seconds": life.number("max_lifetime_seconds", 86400, 600, 604800),
                 "max_job_seconds": life.number("max_job_seconds", 3600, 10, 86400),
                 "command_timeout_seconds": life.number("command_timeout_seconds", 120, 1, 3600)}

    net = top.child("network")
    net.only("setup", "agent")
    setup_net = net.child("setup")
    setup_net.only("mode", "presets", "allow_hosts")
    mode = setup_net.data.get("mode", "allowlist")
    if mode == "allow_all":
        setup_net.fail("mode", "allow_all is never allowed for agent work", code="network_mode_unsupported")
    if mode not in ("allowlist", "deny_all"):
        setup_net.fail("mode", "setup mode is allowlist or deny_all")
    presets = setup_net.data.get("presets", ["registries-v1"] if mode == "allowlist" else [])
    if not isinstance(presets, list) or any(p not in PRESETS for p in presets):
        setup_net.fail("presets", f"presets are from {', '.join(PRESETS)}")
    hosts = setup_net.data.get("allow_hosts") or []
    if not isinstance(hosts, list) or len(hosts) > 256 or not all(isinstance(h, str) and HOST_RE.match(h)
                                                                   for h in hosts):
        setup_net.fail("allow_hosts", "allow_hosts is a list of up to 256 exact host names, no wildcards")
    agent_net = net.child("agent")
    agent_net.only("mode")
    agent_mode = agent_net.data.get("mode", "deny_all")
    if agent_mode != "deny_all":
        agent_net.fail("mode", "agent commands run offline (deny_all). Registry access for agent commands "
                       "needs a request-filtering proxy that is not qualified yet", code="network_mode_unsupported")
    network = {"setup": {"mode": mode, "presets": list(presets), "allow_hosts": sorted(set(hosts))},
               "agent": {"mode": "deny_all"}}

    setup = None
    if "setup" in data:
        s = top.child("setup")
        s.only("command", "cwd", "timeout_seconds")
        cwd = s.text("cwd", ".", max_len=256)
        if cwd.startswith("/") or ".." in Path(cwd).parts:
            s.fail("cwd", "cwd is a folder inside /workspace, such as repo")
        setup = {"command": s.text("command", required=True), "cwd": cwd,
                 "timeout_seconds": s.number("timeout_seconds", 900, 1, 3600)}

    iso = top.child("isolation")
    iso.only("minimum")
    isolation = iso.choice("minimum", "container", ("container", "gvisor", "microvm"))

    tr = top.child("transfer")
    tr.only("max_file_bytes", "max_export_bytes")
    transfer = {"max_file_bytes": tr.number("max_file_bytes", 16 * 2**20, 1024, 64 * 2**20),
                "max_export_bytes": tr.number("max_export_bytes", 2 * 2**30, 2**20, 2 * 2**30)}

    fs = top.child("filesystem")
    fs.only("layout")
    if fs.choice("layout", "workspace", ("workspace", "analysis")) == "analysis":
        fs.fail("layout", "the analysis layout is not supported in this version yet. Use workspace")

    env_r = top.child("env")
    if len(env_r.data) > 64:
        top.fail("env", "at most 64 environment variables")
    env = {}
    for name, value in env_r.data.items():
        upper = str(name).upper()
        if (not ENV_RE.match(str(name)) or upper.startswith(_SECRET_PREFIXES) or upper in _PROXY_VARS
                or any(w in upper for w in _SECRET_WORDS)):
            env_r.fail(name, f"{name} looks like a credential or a proxy setting. Secrets never go into a "
                       "sandbox", code="secret_in_profile")
        if not isinstance(value, (str, int, float)) or isinstance(value, bool) or len(str(value)) > 4096:
            env_r.fail(name, "an environment value is plain text up to 4096 characters")
        from .. import redaction
        if redaction.scrub_text(str(value)) != str(value):
            env_r.fail(name, f"the value of {name} looks like a credential", code="secret_in_profile")
        env[str(name)] = str(value)

    body = {"provider": provider, "image": image, "resources": resources, "lifecycle": lifecycle,
            "network": network, "setup": setup, "isolation": isolation, "transfer": transfer, "env": env}
    digest = "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return Profile(id=profile_id, description=top.text("description", max_len=500), source=source,
                   digest=digest, **body)


def list_profiles(hub_dir) -> list[str]:
    folder = Path(hub_dir) / PROFILE_DIR
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.yaml") if PROFILE_RE.match(p.stem))


def check_profile(hub_dir, profile: Profile, deployment: Deployment | None = None) -> None:
    """Refuse a profile the deployment cannot run as written. No silent downgrade."""
    deployment = deployment or load_deployment(hub_dir)
    if not deployment.enabled:
        raise WorkItemError("sandbox_disabled", f"Sandboxes are off for this deployment. Add {DEPLOYMENT_FILE} "
                            "with enabled: true and a docker provider.", status=503)
    _, binding = deployment.provider(profile.provider)
    if profile.isolation == "microvm" or (profile.isolation == "gvisor" and binding["runtime"] != "runsc"):
        raise WorkItemError("isolation_insufficient",
                            f"{profile.source} needs {profile.isolation} isolation and the provider gives "
                            f"{'gvisor' if binding['runtime'] == 'runsc' else 'container'}.", status=422)


def profile_problem(hub_dir, profile_id: str) -> str | None:
    """Why a station could not use this profile, or None. Used by
    `hubzoid stations validate`."""
    try:
        check_profile(hub_dir, load_profile(hub_dir, profile_id))
    except WorkItemError as exc:
        return exc.message
    return None
