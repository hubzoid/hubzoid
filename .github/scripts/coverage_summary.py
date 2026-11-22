"""Turn coverage.json into a short Markdown summary for the nightly job.

Usage: python .github/scripts/coverage_summary.py coverage.json >> "$GITHUB_STEP_SUMMARY"

Prints the total, one row per top-level package of hubzoid (top-level modules
share one row), and the ten lowest-covered modules of at least 50 statements.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import PurePosixPath

MIN_STATEMENTS = 50


def _pct(covered: int, statements: int) -> float:
    return 100.0 * covered / statements if statements else 100.0


def summarize(data: dict) -> str:
    files = data["files"]
    packages: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    modules = []
    for path, info in files.items():
        parts = PurePosixPath(path).parts
        summary = info["summary"]
        statements, covered = summary["num_statements"], summary["covered_lines"]
        package = f"hubzoid/{parts[1]}" if len(parts) > 2 else "hubzoid (top-level modules)"
        packages[package][0] += covered
        packages[package][1] += statements
        if statements >= MIN_STATEMENTS:
            modules.append((_pct(covered, statements), statements, path))

    totals = data["totals"]
    lines = [
        "## Coverage (whole suite, as the release workflow runs it)",
        "",
        f"**Total: {totals['percent_covered']:.1f}%** "
        f"({totals['covered_lines']:,} of {totals['num_statements']:,} statements)",
        "",
        "| Package | Statements | Covered |",
        "|---|---:|---:|",
    ]
    for name, (covered, statements) in sorted(packages.items(), key=lambda kv: _pct(*kv[1])):
        lines.append(f"| `{name}` | {statements:,} | {_pct(covered, statements):.0f}% |")
    lines += [
        "",
        f"### Ten lowest-covered modules ({MIN_STATEMENTS}+ statements)",
        "",
        "| Module | Statements | Covered |",
        "|---|---:|---:|",
    ]
    for pct, statements, path in sorted(modules)[:10]:
        lines.append(f"| `{path}` | {statements:,} | {pct:.0f}% |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as fh:
        sys.stdout.write(summarize(json.load(fh)))
