"""Deterministic checks on a pull request's added lines: a secret sweep, risky
patterns, and any open-source analyzer that prints SARIF. A secret is a
finding by itself; everything else is evidence the reviewer weighs."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import unquote

from .diff import added_lines

SECRETS = [
    ("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("Stripe live key", re.compile(r"sk_live_[0-9A-Za-z]{20,}")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("hard-coded secret", re.compile(
        r"""(?i)(?:secret|passwd|password|api[_-]?key|token|access[_-]?key)\w*["']?\s*[:=]\s*['"][^'"\s]{12,}['"]""")),
]
PLACEHOLDER = re.compile(r"(?i)(your[_-]?|example|placeholder|changeme|xxxx|dummy|sample|<.*?>|redacted|\.\.\.|"
                         r"os\.environ|getenv|process\.env)")
NOT_SECRET_FILES = re.compile(r"(?i)(test|fixture|example|sample|mock|\.lock$|\.env\.example$)")
RISKY = [
    ("SQL built from a string", re.compile(
        r"""(?i)\b(?:execute|executemany|executescript|raw|query)\s*\(\s*(?:f["']|["'][^"']*["']\s*(?:%|\+|\.format))""")),
    ("shell=True", re.compile(r"""subprocess\.\w+\([^)]*shell\s*=\s*True""")),
    ("os.system with interpolation", re.compile(r"""os\.system\(\s*f?["'][^"']*(?:\{|["']\s*\+)""")),
    ("unsafe YAML load", re.compile(r"""yaml\.(?:load|full_load|unsafe_load)\((?![^)]*SafeLoader)""")),
    ("pickle on untrusted data", re.compile(r"""pickle\.loads?\(""")),
    ("TLS verification turned off", re.compile(r"""verify\s*=\s*False""")),
    ("eval of dynamic code", re.compile(r"""(?<![\w.])eval\(""")),
]


def sweep(files: list[dict]) -> list[dict]:
    out = []
    for f in files:
        name = f["filename"]
        for line, text in added_lines(f.get("patch") or ""):
            if not NOT_SECRET_FILES.search(name) and not PLACEHOLDER.search(text):
                for label, pattern in SECRETS:
                    if pattern.search(text):
                        out.append({"tool": "gitzoid", "rule": label, "level": "error", "path": name,
                                    "line": line, "secret": True,
                                    "message": f"An added line looks like a live {label}."})
                        break
            for label, pattern in RISKY:
                if pattern.search(text):
                    out.append({"tool": "gitzoid", "rule": label, "level": "warning", "path": name,
                                "line": line, "secret": False, "message": f"Added line matches: {label}."})
                    break
    return out


def secret_findings(checks: list[dict]) -> list[dict]:
    return [{"path": c["path"], "line": c["line"], "severity": "high", "confidence": "high",
             "category": "security", "rule": f"secret: {c['rule']}",
             "body": "A secret appears to be committed in this change. Rotate it, then load it from the "
                     "environment or a secret store instead."}
            for c in checks if c.get("secret")]


def run_analyzers(files: list[dict], read_head, analyzers: list[dict]) -> tuple[list[dict], list[str]]:
    """Run each configured analyzer over the changed files and keep results on
    added lines. One that is not installed is skipped with a note."""
    if not analyzers:
        return [], []
    checks, notes = [], []
    added = {(f["filename"], n) for f in files for n, _ in added_lines(f.get("patch") or "")}
    with tempfile.TemporaryDirectory(prefix="gitzoid-") as tmp:
        root = Path(os.path.realpath(tmp))
        for f in files:
            if f.get("status") == "removed":
                continue
            text = read_head(f["filename"])
            target = (root / f["filename"]).resolve()
            if text is None or root not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")}
        for a in analyzers:
            name, cmd = a.get("name") or a["run"][0], [str(x) for x in a["run"]]
            exe = shutil.which(cmd[0])
            if exe is None:
                notes.append(f"{name}: not installed, skipped")
                continue
            try:
                proc = subprocess.run([exe, *cmd[1:]], cwd=root, env=env, capture_output=True, text=True,
                                      timeout=int(a.get("timeout", 120)))
                doc = json.loads(proc.stdout)
            except subprocess.TimeoutExpired:
                notes.append(f"{name}: timed out, skipped")
                continue
            except json.JSONDecodeError:
                notes.append(f"{name}: no SARIF output (exit {proc.returncode})")
                continue
            hits = [c for c in parse_sarif(doc, name, root) if (c["path"], c["line"]) in added]
            checks += hits
            notes.append(f"{name}: {len(hits)} result(s) on added lines")
    return checks, notes


def parse_sarif(doc: dict, tool: str, root: Path | None = None) -> list[dict]:
    out = []
    for run in doc.get("runs") or []:
        for r in run.get("results") or []:
            loc = ((r.get("locations") or [{}])[0].get("physicalLocation") or {})
            uri = unquote((loc.get("artifactLocation") or {}).get("uri", "")).removeprefix("file://")
            if root is not None and os.path.isabs(uri):
                real = os.path.realpath(uri)
                uri = os.path.relpath(real, root) if real.startswith(str(root)) else uri
            out.append({"tool": tool, "rule": r.get("ruleId") or "", "level": r.get("level") or "warning",
                        "path": uri.removeprefix("./"), "line": (loc.get("region") or {}).get("startLine"),
                        "secret": False, "message": ((r.get("message") or {}).get("text") or "")[:300]})
    return out
