"""The supervisor's unit logs (hubzoid.supervisor): each unit writes to one
log file, rotated by size into a fixed number of older files; a process's
output is copied to its log and to the supervisor's output with its name; a
unit without a log folder writes nothing; a write that fails never raises."""
from __future__ import annotations

import io
from types import SimpleNamespace

from hubzoid import supervisor as sv


def test_logs_rotate_by_size_and_keep_a_fixed_number(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "LOG_BYTES", 10)
    monkeypatch.setattr(sv, "LOG_KEEP", 3)
    log = sv._Log(tmp_path / "logs" / "sales.log")
    for i in range(6):
        log.write(f"line {i} " + "x" * 10 + "\n")
    files = sorted(p.name for p in (tmp_path / "logs").iterdir())
    assert files == ["sales.log", "sales.log.1", "sales.log.2", "sales.log.3"]
    assert (tmp_path / "logs" / "sales.log").read_text().startswith("line 5")
    assert (tmp_path / "logs" / "sales.log.1").read_text().startswith("line 4")


def test_one_log_per_unit_and_none_without_a_folder(tmp_path):
    assert sv.log_for("ops", tmp_path) is sv.log_for("ops", tmp_path)
    silent = sv.log_for("ops", None)
    silent.write("nothing happens\n")
    assert silent.path is None
    broken = sv._Log(tmp_path / "x.log")
    broken.path = tmp_path / "missing-dir" / "deeper" / "x.log"     # the folder is gone: no raise
    broken.write("lost\n")


def test_output_is_copied_to_the_log_and_the_supervisor_output(tmp_path, monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sv.sys, "stdout", out)
    sink = sv._Log(tmp_path / "ops.log")
    proc = SimpleNamespace(stdout=[b"started\n", "ready \xe2\n".encode("latin-1")])
    sv._pump(proc, "ops", sink)
    assert (tmp_path / "ops.log").read_text().splitlines()[0] == "started"
    assert out.getvalue().startswith("[ops] started\n[ops] ready ")
    sv._pump(SimpleNamespace(stdout=None), "ops", sink)                # no output stream: nothing to copy
    sv._pump(SimpleNamespace(stdout=42), "ops", sink)                  # not iterable: swallowed
