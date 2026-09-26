"""Workflow identity through real DBOS runs: `run_as`, configuration changes
between runs (two people, one workflow), recovery that keeps the captured
person, and markdown tasks. Each scenario runs in a subprocess because DBOS is
a process-global singleton. Model-free; email uses the preview outbox.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="uses SIGKILL")

_WORKFLOW = textwrap.dedent('''
    import os, time
    from pathlib import Path
    from hubzoid import workflow, step, hub

    @step
    def pause_once():
        marker = Path(os.environ["PAUSE_MARKER"]) if os.environ.get("PAUSE_MARKER") else None
        if marker and not marker.exists():
            marker.write_text("1")
            time.sleep(60)                     # first attempt: killed here

    def body():
        runs = hub.state.get("runs", 0) + 1
        hub.state["runs"] = runs
        pause_once()
        report = hub.run_dir / "report.html"
        report.write_text(f"<h1>Report for {hub.user.id}</h1>")
        art = hub.publish_artifact(report, title="Weekly report")
        mail = hub.send_email("Your weekly report", "It is ready.", artifacts=[art])
        return {"user": hub.user.id, "runs": runs, "artifact": art["id"],
                "mail": mail["status"], "recipient": mail["recipient"]}

    @workflow()
    def weekly():
        return body()

    @workflow(run_as="carol@company.com")
    def carols():
        return body()
''')

_SETUP = textwrap.dedent('''
    import sys
    from hubzoid.access import store_for
    gs = store_for(sys.argv[1])
    for who in ("alice@company.com", "bob@company.com", "carol@company.com"):
        gs.upsert_identity(email=who, owui_id="id-" + who)
''')

_RUN = textwrap.dedent('''
    import json, os, sys
    from hubzoid.workflows import runtime
    runtime.init(sys.argv[1], hub_name="team")
    runtime.load_workflows(sys.argv[1])
    runtime.launch()
    out = []
    for name, user in json.loads(sys.argv[2]):
        if user:
            os.environ["HUBZOID_WORKFLOW_USER"] = user
        try:
            out.append(runtime.run_now(name))
        except Exception as exc:
            out.append({"error": str(exc)})
    print("RESULT " + json.dumps(out), flush=True)
    runtime.shutdown()
''')

_START = textwrap.dedent('''
    import sys, time
    from hubzoid.workflows import runtime
    runtime.init(sys.argv[1], hub_name="team")
    runtime.load_workflows(sys.argv[1])
    runtime.launch()
    handle = runtime.start("weekly")
    print("STARTED", handle.get_workflow_id(), flush=True)
    time.sleep(120)
''')

_RESUME = textwrap.dedent('''
    import json, sys, time
    from dbos import DBOS
    from hubzoid.workflows import runtime
    runtime.init(sys.argv[1], hub_name="team")
    runtime.load_workflows(sys.argv[1])
    runtime.launch()
    deadline = time.time() + 60
    while time.time() < deadline:
        st = DBOS.get_workflow_status(sys.argv[2])
        if st.status in ("SUCCESS", "ERROR"):
            break
        time.sleep(0.5)
    print("RESULT " + json.dumps({"status": st.status, "output": st.output,
                                  "error": str(st.error) if st.error else None}), flush=True)
    runtime.shutdown()
''')


@pytest.fixture
def team(tmp_path):
    hub = tmp_path / "team"
    (hub / "workflows" / "weekly").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: team\n---\nbody")
    (hub / "workflows" / "weekly" / "main.py").write_text(_WORKFLOW)
    env = {k: v for k, v in os.environ.items()
           if k not in ("HUBZOID_WORKFLOW_USER", "HUBZOID_DEPLOYMENT", "DATABASE_URL",
                        "HUBZOID_SMTP_HOST", "HUBZOID_GATEWAY_ADMIN_EMAIL")}
    env.update(HUBZOID_OPERATIONAL_DB=f"sqlite:///{tmp_path / 'ops.db'}",
               HUBZOID_DBOS_DB=f"sqlite:///{tmp_path / 'dbos.db'}",
               WEBUI_AUTH="true", HUBZOID_EMAIL_DELIVERY="preview")
    subprocess.run([sys.executable, "-c", _SETUP, str(hub)], env=env, check=True,
                   capture_output=True, text=True)
    return hub, env


def _run(hub, env, plan):
    proc = subprocess.run([sys.executable, "-c", _RUN, str(hub), json.dumps(plan)],
                          env=env, capture_output=True, text=True, timeout=180)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, proc.stderr[-3000:]
    return json.loads(line[7:])


def _owner_of(env, artifact_id):
    from sqlalchemy import create_engine, text

    eng = create_engine(env["HUBZOID_OPERATIONAL_DB"])
    with eng.connect() as c:
        return c.execute(text("SELECT owner FROM hz_artifacts WHERE id=:i"),
                         {"i": artifact_id}).scalar()


def test_same_workflow_for_two_people_keeps_everything_apart(team):
    hub, env = team
    out = _run(hub, env, [["weekly", "alice@company.com"], ["weekly", "bob@company.com"],
                          ["weekly", "alice@company.com"], ["carols", "bob@company.com"]])
    a1, b1, a2, carol = out
    assert a1["user"] == "alice@company.com" and a1["runs"] == 1
    assert b1["user"] == "bob@company.com" and b1["runs"] == 1        # fresh state for bob
    assert a2["user"] == "alice@company.com" and a2["runs"] == 2      # alice's state kept
    assert b1["recipient"] == "bob@company.com" and a2["recipient"] == "alice@company.com"
    assert b1["mail"] == a2["mail"] == "previewed"
    assert _owner_of(env, b1["artifact"]) == "bob@company.com"
    assert _owner_of(env, a2["artifact"]) == "alice@company.com"
    # An explicit run_as wins over the configured default.
    assert carol["user"] == "carol@company.com" and carol["recipient"] == "carol@company.com"
    # Each person's email preview is in their own outbox folder.
    outboxes = sorted(p.name.split("-")[0] for p in (hub / ".hubzoid" / "outbox").iterdir())
    assert outboxes == ["alice", "bob", "carol"]


def test_a_missing_or_blocked_account_fails_the_run_without_fallback(team):
    hub, env = team
    out = _run(hub, env, [["weekly", "ghost@company.com"]])
    assert "no signed-in account" in out[0]["error"]


def test_recovery_keeps_the_person_the_run_started_as(team, tmp_path):
    hub, env = team
    env = dict(env, PAUSE_MARKER=str(tmp_path / "paused"), HUBZOID_WORKFLOW_USER="alice@company.com")
    proc = subprocess.Popen([sys.executable, "-c", _START, str(hub)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wid = None
    deadline = time.time() + 90
    while time.time() < deadline and wid is None:
        line = proc.stdout.readline()
        if line.startswith("STARTED"):
            wid = line.split()[1]
    assert wid, proc.stderr.read()[-2000:] if proc.poll() is not None else "no start"
    deadline = time.time() + 60
    while time.time() < deadline and not (tmp_path / "paused").exists():
        time.sleep(0.2)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    # The configuration changes while the run is interrupted.
    env["HUBZOID_WORKFLOW_USER"] = "bob@company.com"
    rec = subprocess.run([sys.executable, "-c", _RESUME, str(hub), wid], env=env,
                         capture_output=True, text=True, timeout=180)
    line = next((l for l in rec.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, rec.stderr[-3000:]
    result = json.loads(line[7:])
    assert result["status"] == "SUCCESS", result
    assert result["output"]["user"] == "alice@company.com"
    assert result["output"]["recipient"] == "alice@company.com"


_MD_TASK = """---
schedule: "0 3 * * *"
run: 'printf "%s" "$HUBZOID_RUN_AS" > who.txt'
run_as: dana@company.com
---
Record who this run acts as.
"""

_MD_RUN = textwrap.dedent('''
    import sys, time
    from dbos import DBOS
    from hubzoid.workflows import runtime, markdown
    runtime.init(sys.argv[1], hub_name="team")
    runtime.launch()
    h = markdown.enqueue_task("who", "2026-09-26T03:00")
    try:
        print("OUT", h.get_result(), flush=True)
    except Exception as exc:
        print("ERR", exc, flush=True)
    steps = DBOS.list_workflow_steps(h.get_workflow_id())
    print("STEPS", [s["function_name"] for s in steps], flush=True)
    runtime.shutdown()
''')


def test_markdown_task_acts_as_its_run_as(team):
    hub, env = team
    (hub / "schedule").mkdir()
    (hub / "schedule" / "who.md").write_text(_MD_TASK)
    subprocess.run([sys.executable, "-c",
                    "import sys\nfrom hubzoid.access import store_for\n"
                    "store_for(sys.argv[1]).upsert_identity(email='dana@company.com', owui_id='d')",
                    str(hub)], env=env, check=True)
    proc = subprocess.run([sys.executable, "-c", _MD_RUN, str(hub)], env=env,
                          capture_output=True, text=True, timeout=180)
    assert "OUT {" in proc.stdout, proc.stdout + proc.stderr[-2000:]
    assert (hub / "who.txt").read_text() == "dana@company.com"
    assert "hz_md_identity" in proc.stdout
    folders = [p.name for p in (hub / ".hubzoid" / "schedule").iterdir()]
    assert any(f.startswith("who@dana-company.com-") for f in folders), folders
