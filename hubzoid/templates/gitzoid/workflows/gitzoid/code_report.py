"""code_report: what changed in the code for any repos and period. Pull
requests merged, lines added and removed, sizes, areas changed, tests touched,
reverts, and GitZoid's findings over the period.

From chat or Claude Code: "code report for storefront, last month". "Keep it
fresh every month" makes it your schedule (inputs saved, runs as you). The
team version runs on the 1st of each month for groups that list `code` under
monthly_reports, and goes to the group's team page and list.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

from hubzoid import workflow


def _load(file: str):
    path = Path(__file__).with_name(file)
    name = f"gitzoid_{path.stem.strip('_')}_" + hashlib.sha1(str(path).encode()).hexdigest()[:10]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]

reports = _load("_reports.py")
gz = reports.gz


@workflow(inputs=reports.gz.reports.ReportRequest, schedule="0 7 1 * *", schedule_input={"period": "last_month"},
          as_tool=True, schedulable=True, catch_up=True,
          surfaces=("web", "owui", "mcp"))
def code_report():
    """Code report for a period: what merged, review findings and how they were resolved, per repo or digest group."""
    return reports.run_report("code")
