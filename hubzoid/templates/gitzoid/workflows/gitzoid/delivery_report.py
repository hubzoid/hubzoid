"""delivery_report: how work flows for any repos and period. Pull requests
merged per week, cycle time, time to first review, merges without review, and
what is stuck now.

From chat or Claude Code: "delivery report for storefront, last two weeks".
"Keep it fresh every Monday" makes it your schedule. The team version runs on
the 1st of each month for groups that list `delivery` under monthly_reports.
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
def delivery_report():
    """Delivery report for a period: merged pull requests, cycle time, review waits and stuck pull requests."""
    return reports.run_report("delivery")
