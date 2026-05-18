"""Open WebUI subprocess manager.

We start `open-webui serve` as a child process and point it at the hubzoid
bridge as its OpenAI-compatible upstream. Per-hub state (SQLite DB, uploads)
lives under `<hub>/.openwebui-data/` so each hub has isolated history.

`open-webui` is a required dep of hubzoid (`pip install hubzoid` bundles it).
If the binary is not on PATH we tell the user how to repair the install.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _find_binary() -> str | None:
    """Locate the open-webui executable.

    Order. First, next to the running Python (the common case when hubzoid
    is invoked via its console-script entry point inside a venv). Then PATH.
    """
    sibling = Path(sys.executable).parent / "open-webui"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return shutil.which("open-webui")


def is_available() -> bool:
    return _find_binary() is not None


def start(*, hub_dir: Path, bridge_port: int, ui_port: int, api_key: str, model_label: str, webui_name: str | None) -> subprocess.Popen:
    """Spawn Open WebUI as a subprocess. Returns the Popen handle."""
    binary = _find_binary()
    if binary is None:
        raise FileNotFoundError(
            "open-webui not found next to the running Python or on PATH. "
            "It is bundled with hubzoid; reinstall to repair:\n"
            "    pip install --force-reinstall hubzoid\n"
            "or install it directly:\n"
            "    pip install open-webui"
        )

    data_dir = hub_dir / ".openwebui-data"
    data_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "DATA_DIR": str(data_dir),
            "OPENAI_API_BASE_URL": f"http://127.0.0.1:{bridge_port}/v1",
            "OPENAI_API_KEY": api_key,
            "WEBUI_AUTH": env.get("WEBUI_AUTH", "False"),
            "ENABLE_OLLAMA_API": "False",
            "DEFAULT_MODELS": model_label,
        }
    )
    if webui_name:
        env["WEBUI_NAME"] = webui_name

    log_path = data_dir / "openwebui.log"
    log_file = log_path.open("ab", buffering=0)
    cmd = [binary, "serve", "--host", "127.0.0.1", "--port", str(ui_port)]
    proc = subprocess.Popen(cmd, env=env, stdout=log_file, stderr=subprocess.STDOUT)
    proc._log_path = log_path  # type: ignore[attr-defined]
    return proc
