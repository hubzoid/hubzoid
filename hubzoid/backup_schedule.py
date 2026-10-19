"""Scheduled backups: a verified archive of the deployment, weekly by default.

    HUBZOID_BACKUPS       weekly (default) | daily | off
    HUBZOID_BACKUP_KEEP   how many scheduled archives to keep (default 4)
    HUBZOID_BACKUP_DIR    where they go (default <data root>/.hubzoid/backups)

The bridge looks every half hour (the first look a minute after it starts) and
takes a backup when the last verified one is older than the schedule. A
service that slept through its slot backs up soon after it wakes. Only one
bridge of a gateway takes them: the first hub in the deployment manifest.
Every archive is verified before it counts (`backup.verify`), the oldest ones
beyond HUBZOID_BACKUP_KEEP are removed, and a failure is recorded for
`hubzoid doctor`.

The archives sit on the same disk as the data, which protects against a bad
change or a corrupted file, not a lost volume: copy them elsewhere too
(docs/BACKUP.md).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from . import datadir

log = logging.getLogger(__name__)

SETTING, KEEP_SETTING, DIR_SETTING = "HUBZOID_BACKUPS", "HUBZOID_BACKUP_KEEP", "HUBZOID_BACKUP_DIR"
INTERVALS = {"weekly": 7 * 86400, "daily": 86400, "off": None}
DEFAULT_KEEP = 4
FIRST_LOOK, EVERY = 60.0, 1800.0
RETRY_AFTER = 6 * 3600
_NAME = re.compile(r"hubzoid-backup-(\d{8}-\d{6})\.tar\.gz")


class ScheduleError(ValueError):
    pass


def interval(env=None) -> float | None:
    """Seconds between scheduled backups, or None when they are off."""
    value = ((os.environ if env is None else env).get(SETTING) or "weekly").strip().lower()
    if value not in INTERVALS:
        raise ScheduleError(f"{SETTING}={value} is not weekly, daily or off")
    return INTERVALS[value]


def keep(env=None) -> int:
    raw = ((os.environ if env is None else env).get(KEEP_SETTING) or str(DEFAULT_KEEP)).strip()
    if not raw.isdigit() or int(raw) < 1:
        raise ScheduleError(f"{KEEP_SETTING}={raw} is not a whole number of at least 1")
    return int(raw)


def folder(hub_dir: Path, env=None) -> Path:
    """Where scheduled archives go."""
    raw = ((os.environ if env is None else env).get(DIR_SETTING) or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    from .control import deploy

    return deploy.state_root(Path(hub_dir)) / datadir.STATE / "backups"


def archives(hub_dir: Path) -> list[dict]:
    """The scheduled archives, newest first."""
    base = folder(hub_dir)
    if not base.is_dir():
        return []
    out = []
    for path in base.iterdir():
        m = _NAME.fullmatch(path.name)
        if m and path.is_file() and not path.is_symlink():
            out.append({"path": str(path), "name": path.name, "bytes": path.stat().st_size,
                        "created": datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").isoformat()})
    return sorted(out, key=lambda a: a["name"], reverse=True)


def prune(hub_dir: Path, count: int) -> list[str]:
    """Remove the oldest scheduled archives beyond ``count``."""
    removed = []
    for a in archives(hub_dir)[count:]:
        Path(a["path"]).unlink(missing_ok=True)
        removed.append(a["path"])
    return removed


def responsible(hub_dir: Path) -> bool:
    """Whether this hub's bridge takes the deployment's scheduled backups: a
    standalone hub, or the first hub of a gateway."""
    from . import deployment

    hubs = deployment.read(Path(hub_dir), require_hub=False).get("hubs") or []
    return not hubs or Path(hubs[0]["path"]).resolve() == Path(hub_dir).resolve()


def _meta(hub_dir: Path, key: str) -> dict | None:
    from sqlalchemy import create_engine, inspect, text

    from . import db

    url = db.operational_url(Path(hub_dir))
    if url.startswith("sqlite") and not Path(url.split(":///", 1)[-1]).exists():
        return None
    engine = create_engine(db.sqlalchemy_url(url))
    try:
        if not inspect(engine).has_table("hz_meta"):
            return None
        with engine.connect() as c:
            row = c.execute(text("SELECT v FROM hz_meta WHERE k=:k"), {"k": key}).fetchone()
        return json.loads(row[0]) if row and row[0] else None
    finally:
        engine.dispose()


def due(hub_dir: Path, now: float | None = None) -> bool:
    """Whether a scheduled backup is due: on, and the last verified one is older
    than the schedule (or there is none)."""
    every = interval()
    if every is None:
        return False
    now = now or time.time()
    failed = _meta(hub_dir, "backup:failed") or {}
    last = _meta(hub_dir, "backup:last") or {}
    if float(failed.get("at") or 0) > float(last.get("at") or 0) and now - float(failed["at"]) < RETRY_AFTER:
        return False                                   # failed recently: doctor says why
    if not last.get("verified"):
        return True
    return now - float(last.get("at") or 0) >= every


def run_once(hub_dir: Path, *, say=log.info) -> dict:
    """Take, verify and keep one scheduled backup, then prune. Raises
    backup.BackupError when it fails (recorded for doctor by the backup)."""
    from . import backup

    base = folder(hub_dir)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    out = base / f"hubzoid-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
    index = backup.backup(Path(hub_dir), out, actor="schedule", say=say)
    removed = prune(hub_dir, keep())
    log.info("backup: scheduled backup written to %s (%d older removed)", out, len(removed))
    return {"path": str(out), "removed": removed, "files": index["verified"]["files"]}


async def loop(hub_dir: Path, *, first: float = FIRST_LOOK, every: float = EVERY) -> None:
    """The bridge's backup loop. Never raises: a failure is logged, recorded,
    and tried again at the next look."""
    try:
        if interval() is None or not responsible(hub_dir):
            return
    except Exception:  # noqa: BLE001 — a bad setting is reported by doctor
        log.warning("backup: scheduled backups are off: the settings cannot be read", exc_info=True)
        return
    await asyncio.sleep(first)
    while True:
        try:
            if await asyncio.to_thread(due, hub_dir):
                await asyncio.to_thread(run_once, hub_dir)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — recorded as backup:failed; tried again later
            log.exception("backup: the scheduled backup failed")
        await asyncio.sleep(every)
