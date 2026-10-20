"""The public tree carries no keys, customer names or local paths.

Runs against the files git tracks, so it skips outside a git checkout (an
installed wheel or an sdist has nothing to check).
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Customer names and client-specific project or program labels that are not
# approved for public use, as SHA-256 of the lowercase word, so this file
# does not publish them either. Compared against
# every word of every tracked text file (camel case is split, so a name inside
# an identifier is caught; a longer word that merely contains one is not).
_PRIVATE_WORDS = {
    "59e0a7b720c83cd3240ed132a84699085d1fc022f8c829bf98794cad3fc99645",
    "6c43993dc2a74842f9f6f2a3ec5890467bba32b28c536d8310bc8049d5ef6524",
    "021017239d6ca1d28d69150a77efd161f0293292b1e1d8636b5f1c75830fc8cb",
    "384c0b6dc96e6cefe4c53959267583d261cdab098a4d65dae45b01ae610bf66f",
    "22ab8ca1ea7f6148f548736b9b05a006f16426b9f87cdee15d2787ab35ac3dac",
    "4b268431cc59851ae784ca6a212336644b2915289680e528334815fa4556d882",
    "d1b9724468ea35aa1d3dbab097b67c0ca61e4aa2df63be9c74b0dd688c2e44a6",
}
_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")

# Built from parts so this file does not match itself.
_LOCAL_PATH = re.compile(r"(?<![A-Za-z0-9_.-])/Users/[A-Za-z0-9._-]+/|" + "~" + "/Desktop/|" + "C:" + r"\\Users\\")

_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".woff", ".woff2",
                    ".ttf", ".otf", ".pdf", ".zip", ".gz", ".db", ".sqlite"}


def _tracked() -> list[str]:
    if not (ROOT / ".git").exists() or shutil.which("git") is None:
        pytest.skip("not a git checkout")
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return [p for p in out.stdout.decode().split("\0") if p]


def _text_files() -> list[tuple[str, str]]:
    files = []
    for rel in _tracked():
        path = ROOT / rel
        if rel.startswith("hubzoid/portal_dist/") or path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        try:
            files.append((rel, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return files


def test_key_files_are_ignored_and_not_tracked():
    tracked = set(_tracked())
    for name in (".webui_secret_key", "secret.key"):
        assert not [p for p in tracked if Path(p).name == name], name
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".webui_secret_key" in ignored and "secret.key" in ignored


def test_no_private_customer_names():
    hits = []
    for rel, text in _text_files():
        words = {w.lower() for w in _WORD.findall(text)}
        for word in words:
            if hashlib.sha256(word.encode()).hexdigest() in _PRIVATE_WORDS:
                hits.append(rel)
    assert not hits, f"customer names in {sorted(set(hits))}"


def test_no_local_absolute_paths():
    hits = [rel for rel, text in _text_files() if _LOCAL_PATH.search(text)]
    assert not hits, f"local paths in {hits}"
