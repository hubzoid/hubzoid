"""`hubzoid restore`: where a backup would go (--dry-run), a rehearsal that
changes nothing, refusals for a malformed --move and a missing PostgreSQL
variable, and a real restore to a new place that keeps what was there."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from hubzoid import backup as bk
from hubzoid.cli import app
from tests.test_backup import _standalone, clean  # noqa: F401 — clean is an autouse fixture


def restore(*args):
    r = CliRunner().invoke(app, ["restore", *args], env={"COLUMNS": "250"}, catch_exceptions=False)
    return r.exit_code, r.output


@pytest.mark.slow  # builds and rehearses a real archive (seconds)
def test_dry_run_rehearse_and_refusals(tmp_path, monkeypatch):
    hub = _standalone(tmp_path)
    archive = tmp_path / "b.tar.gz"
    bk.backup(hub, archive, wait=0)

    code, text = restore(str(archive), "--dry-run")
    assert code == 0 and str(hub) in text.replace("\n", "") and "(exists; kept aside)" in text.replace("\n", "")

    code, text = restore(str(archive), "--rehearse")
    assert code == 0 and "Rehearsal passed" in text and "Nothing was changed." in text
    code, out = restore(str(archive), "--rehearse", "--json")
    assert code == 0 and json.loads(out)["ok"] is True

    code, text = restore(str(archive), "--move", "no-equals-sign")
    assert code == 2 and "--move expects OLD=NEW" in text
    monkeypatch.delenv("HZ_RESTORE_PG", raising=False)
    code, text = restore(str(archive), "--rehearse", "--postgres-env", "HZ_RESTORE_PG")
    assert code == 2 and "HZ_RESTORE_PG is not set." in text


@pytest.mark.slow
def test_a_restore_to_a_new_place(tmp_path):
    hub = _standalone(tmp_path)                 # a backup holds the hub's state, not its files (Git has those)
    report = hub / "output" / "s1" / "report.txt"
    archive = tmp_path / "b.tar.gz"
    bk.backup(hub, archive, wait=0)
    elsewhere = tmp_path / "restored"
    code, text = restore(str(archive), "--move", f"{hub.resolve()}={elsewhere}")
    assert code == 0 and "Restore complete." in text, text
    assert (elsewhere / "output" / "s1" / "report.txt").read_text() == report.read_text()
    assert report.exists()                      # the original is untouched
