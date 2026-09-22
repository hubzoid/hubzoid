"""Audit decision time filtering compares INSTANTS, not lexical strings, so a window
expressed with a different UTC offset / format than the stored rows still selects the
same events (review finding)."""
from __future__ import annotations

import json
from pathlib import Path

from hubzoid.access import audit


def _write(hub_dir: Path, rows):
    logs = hub_dir / "logs"
    logs.mkdir(parents=True)
    (logs / "access-2026-09.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_time_filter_is_instant_not_lexical(tmp_path):
    # Three events at the same instant expressed three ways, plus one an hour later.
    _write(tmp_path, [
        {"ts": "2026-09-19T12:00:00+00:00", "user": "a", "decision": "allow"},
        {"ts": "2026-09-19T12:00:00Z", "user": "b", "decision": "allow"},
        {"ts": "2026-09-19T17:30:00+05:30", "user": "c", "decision": "allow"},  # == 12:00Z
        {"ts": "2026-09-19T13:00:00Z", "user": "d", "decision": "allow"},
    ])
    # Window [12:30Z, 13:30Z) given with a non-UTC offset must select only 'd', even
    # though a lexical compare would mis-order the "+05:30"/"Z"/"+00:00" strings.
    since = "2026-09-19T18:00:00+05:30"  # 12:30Z
    until = "2026-09-19T13:30:00Z"
    users = {r["user"] for r in audit.read(tmp_path, since=since, until=until)}
    assert users == {"d"}
    # A window ending exactly at the shared instant (inclusive) selects a, b, c.
    early = {r["user"] for r in audit.read(tmp_path, until="2026-09-19T12:00:00Z")}
    assert early == {"a", "b", "c"}
    # No window → all rows.
    assert len(audit.read(tmp_path)) == 4


def test_naive_timestamp_read_as_utc(tmp_path):
    _write(tmp_path, [{"ts": "2026-09-19T12:00:00", "user": "x", "decision": "allow"}])
    # Naive stored ts is treated as UTC, so a UTC window includes it.
    assert [r["user"] for r in audit.read(
        tmp_path, since="2026-09-19T11:00:00Z", until="2026-09-19T13:00:00Z")] == ["x"]
    # And is excluded by a window that does not contain 12:00Z.
    assert audit.read(tmp_path, since="2026-09-19T13:00:00Z") == []
