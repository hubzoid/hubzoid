"""JUnit XML for a suite, for CI systems that show test reports.

One <testcase> per eval case: a failed check or judge verdict is a <failure>,
a case that crashed or timed out is an <error>. Text is already redacted by the
time it reaches a SuiteResult (``calls.redact``); responses are not included.
"""
from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .results import SuiteResult


def to_xml(suite: SuiteResult) -> bytes:
    errors = sum(1 for c in suite.cases if c.error)
    failures = sum(1 for c in suite.cases if not c.passed and not c.error)
    total_time = sum(c.duration for c in suite.cases)
    root = ET.Element("testsuites", name="hubzoid evals", tests=str(len(suite.cases)),
                      failures=str(failures), errors=str(errors), time=f"{total_time:.3f}")
    ts = ET.SubElement(root, "testsuite", name=suite.hub, tests=str(len(suite.cases)), failures=str(failures),
                       errors=str(errors), time=f"{total_time:.3f}", timestamp=suite.started_at or "")
    props = ET.SubElement(ts, "properties")
    for key, value in (("model", suite.model), ("judge_model", suite.judge_model or ""),
                       ("trigger", suite.trigger or ""), ("run_as", suite.run_as or "")):
        ET.SubElement(props, "property", name=key, value=value or "")
    for case in suite.cases:
        tc = ET.SubElement(ts, "testcase", classname=f"{suite.hub}.evals", name=case.name,
                           time=f"{case.duration:.3f}")
        if case.error:
            ET.SubElement(tc, "error", message=case.error[:500]).text = case.error
        elif not case.passed:
            lines = [f"{c.kind}: {c.detail}" for c in case.checks if not c.passed]
            if case.judge is not None and not case.judge.passed:
                lines.append(f"judge: {case.judge.error or f'{case.judge.score}/10 < {case.judge.threshold}'}"
                             + (f"\n{case.judge.reasoning}" if case.judge.reasoning else ""))
            ET.SubElement(tc, "failure", message=case.reason[:500]).text = "\n".join(lines)
        if case.tool_calls:
            ET.SubElement(tc, "system-out").text = "tools: " + ", ".join(case.tool_calls)
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def write(suite: SuiteResult, path: Path) -> Path:
    """Write the XML atomically, creating the folder."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(to_xml(suite))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
