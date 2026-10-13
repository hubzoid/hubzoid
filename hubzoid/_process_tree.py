"""Terminate a child tree on Windows without POSIX signals or shell commands."""
from __future__ import annotations

import os
import subprocess


def stop_windows_tree(proc):
    pid = getattr(proc, "pid", None)
    if type(pid) is not int or pid <= 1:
        proc.kill()
        return
    try:
        # Windows console processes cannot use SIGTERM handlers for a graceful
        # shutdown. Kill the tree while the supervisor still identifies it.
        taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        subprocess.run([taskkill, "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass
    # A failed taskkill must not leave the immediate child running.
    try:
        proc.kill()
    except (ProcessLookupError, PermissionError):
        pass
