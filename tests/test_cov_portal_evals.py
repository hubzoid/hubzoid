"""Eval results for the Console (hubzoid.portal_evals): which result files are
read (newest first, never index.json, symlinks or stray names), an unreadable
file is skipped, and each case's latest verdict comes from the newest run
that ran it. Runs are made with the CLI and a fake model."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from hubzoid import portal_evals as pe
from hubzoid.cli import app
from tests.test_evals_cli import _hub, fake_model  # noqa: F401


def run_evals(hub, *args):
    r = CliRunner().invoke(app, ["eval", "run", str(hub), *args])
    return r.exit_code


def test_latest_verdict_per_case_from_the_newest_run_that_ran_it(tmp_path, fake_model):  # noqa: F811
    hub = _hub(tmp_path, {"a.md": "---\ncontains: [pong]\n---\nsay pong",
                          "b.md": "---\ncontains: [pong]\ntags: [solo]\n---\nsay pong"})
    fake_model("nope")
    run_evals(hub)                                   # both fail
    fake_model("pong")
    run_evals(hub, "--tag", "solo")                  # only b runs, and passes (often in the same second)
    files = pe.run_files(hub)
    assert len(files) == 2
    latest = pe.latest_by_case(hub, {"a", "b"})
    assert latest["b"]["passed"] is True and latest["b"]["stamp"] == files[0].stem
    assert latest["a"]["passed"] is False and latest["a"]["stamp"] == files[1].stem
    assert pe.latest_by_case(hub, set()) == {}


def test_only_result_files_are_read_and_a_broken_one_is_skipped(tmp_path, fake_model):  # noqa: F811
    hub = _hub(tmp_path, {"a.md": "---\ncontains: [pong]\n---\nsay pong"})
    fake_model("pong")
    run_evals(hub)
    runs = pe._runs_dir(hub)
    good = pe.run_files(hub)[0]
    (runs / "index.json").write_text("{}")
    broken = runs / ("9" + good.name[1:])            # sorts first, and is not JSON
    broken.write_text("{not json")
    (runs / ("8" + good.name[1:])).symlink_to(good)
    names = [p.name for p in pe.run_files(hub)]
    assert names == [broken.name, good.name]         # no index.json, no symlink
    assert pe.load(broken) is None and pe.load(runs / "missing.json") is None
    assert pe.latest_by_case(hub, {"a"})["a"]["stamp"] == good.stem
    assert pe._schema({"schema": "x"}) == 1 and pe._schema({"schema": 2}) == 2
    assert json.loads(good.read_text())["cases"]
