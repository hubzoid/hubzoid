"""Work items on the real workflow engine, in a separate process.

Covers what only the engine can show: a crash after a finished stage does not
repeat it, an approval continues the same work and sends once, an uncertain
send waits for its requester, and a code change stops work instead of moving it
silently. The engine is a process-global singleton, so it runs in a child
process and this test drives it the way the CLI does, through DBOS's client.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.skipif(os.name == "nt", reason="uses SIGKILL")]

OWNER = "admin@localhost"

WORKFLOW = textwrap.dedent('''
    import json, os, time
    from pathlib import Path
    from hubzoid import hub, workflow
    from hubzoid.work_items import Route

    LOG = Path(os.environ["WI_LOG"])

    def log(line):
        with LOG.open("a") as f:
            f.write(line + "\\n")

    def plan():
        log("plan")
        return {"body": "Hello " + hub.work_item.input["name"]}

    def slow_check():
        marker = Path(str(LOG) + ".slow")
        if hub.work_item.input.get("slow") and not marker.exists():
            marker.write_text("1")
            time.sleep(120)            # the first attempt is killed here
        log("check")
        return {"checked": True}

    def send(args):
        mode = hub.work_item.input.get("send")
        log("send " + json.dumps(args, sort_keys=True))
        if mode == "lose" and not Path(str(LOG) + ".lost").exists():
            Path(str(LOG) + ".lost").write_text("1")
            raise TimeoutError("the server accepted it, then the connection dropped")
        return {"message_id": "m-1"}

    @workflow(run_as="admin@localhost",
              work_items={"act": Route(stages=("plan", "check"), actions=("email.send",))})
    def desk():
        drafted = hub.work_item.checkpoint("plan", plan)
        hub.work_item.checkpoint("check", slow_check)
        sent = hub.operation.run("send_reply", target={"to": "c@example.com"}, arguments=drafted,
                                 recovery="escalate", actions="email.send", dispatch=send)
        return {"sent": sent, "version": VERSION}

    VERSION = 1
''')

ENGINE = textwrap.dedent('''
    import sys, time
    from pathlib import Path
    from hubzoid.workflows import runtime
    runtime.init(sys.argv[1])  # as the server does: the folder name as written
    runtime.load_workflows(sys.argv[1])
    runtime.launch()
    runtime._OWNER.heartbeat(runtime.ready_record())   # what the bridge's dispatcher does
    print("READY", flush=True)
    stop = Path(sys.argv[2])
    beat = time.time()
    while not stop.exists():
        time.sleep(0.2)
        if time.time() - beat > 5:
            runtime._OWNER.heartbeat(runtime.ready_record())
            beat = time.time()
    runtime.shutdown()
''')


@pytest.fixture
def desk(tmp_path, monkeypatch):
    for var in ("WEBUI_AUTH", "DATABASE_URL", "HUBZOID_OPERATIONAL_DB"):
        monkeypatch.delenv(var, raising=False)
    # Capitals in the folder name: 2.0.1 queued such hubs' items where no
    # worker listened (HUB-140 F5).
    root = tmp_path / "Desk-Hub"
    (root / "workflows" / "desk").mkdir(parents=True)
    (root / "AGENTS.md").write_text("---\nname: Desk\n---\nA desk.\n")
    (root / "workflows" / "desk" / "main.py").write_text(WORKFLOW)
    (root / "work-items.yaml").write_text("version: 1\nwrite_modes:\n  email.send: approve\n")
    log = tmp_path / "events.log"
    log.write_text("")
    monkeypatch.setenv("WI_LOG", str(log))
    return root, log, tmp_path


class Engine:
    def __init__(self, hub, tmp):
        self.hub, self.stop = hub, tmp / f"stop-{time.time_ns()}"
        script = tmp / "engine.py"
        script.write_text(ENGINE)
        self.proc = subprocess.Popen([sys.executable, str(script), str(hub), str(self.stop)],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     env=dict(os.environ))
        deadline = time.time() + 60
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if line.startswith("READY"):
                return
            if not line and self.proc.poll() is not None:
                break
        raise RuntimeError("the engine did not start")

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(10)

    def close(self):
        self.stop.write_text("1")
        try:
            self.proc.wait(20)
        except subprocess.TimeoutExpired:
            self.kill()


def wait_for(hub, item_id, states, timeout=60):
    from hubzoid.work_items import store

    deadline = time.time() + timeout
    while time.time() < deadline:
        item = store.get_item(hub, item_id)
        if item["state"] in states:
            return item
        time.sleep(0.3)
    raise AssertionError(f"item stayed {store.get_item(hub, item_id)['state']}")


def lines(log):
    return [line for line in log.read_text().splitlines() if line]


def test_crash_after_stage_and_approval_continuation(desk):
    from hubzoid.work_items import service, store

    hub, log, tmp = desk
    engine = Engine(hub, tmp)
    try:
        item, created = service.admit_and_start(hub, workflow="desk", key="operator:crash",
                                                input={"name": "Ana", "slow": True}, requester=OWNER)
        again, created_again = service.admit_and_start(hub, workflow="desk", key="operator:crash",
                                                       input={"name": "Ana", "slow": True},
                                                       requester=OWNER)
        assert created and not created_again and again["id"] == item["id"]
        deadline = time.time() + 30
        while "plan" not in lines(log) and time.time() < deadline:
            time.sleep(0.2)
        time.sleep(1)
        engine.kill()                   # killed inside the second stage
    finally:
        engine.close()
    engine = Engine(hub, tmp)           # recovery resumes the same execution
    try:
        waiting = wait_for(hub, item["id"], ("waiting",))
        assert waiting["reason"] == "approval_required"
        assert lines(log).count("plan") == 1          # the finished stage was not repeated
        op = store.list_ops(hub, "desk-hub", item_id=item["id"])[0]
        result = service.approve(hub, op["id"], OWNER, digest=op["args_digest"])
        assert result["work_item"]["execution"] == 2
        done = wait_for(hub, item["id"], ("completed",))
        assert done["result_json"]["result"]["sent"] == {"message_id": "m-1"}
        assert [line for line in lines(log) if line.startswith("send")] == ['send {"body": "Hello Ana"}']
        assert lines(log).count("plan") == 1 and lines(log).count("check") == 1
        shown = service.show(hub, item["id"], OWNER)
        assert [s["stage"] for s in shown["stages"]] == ["plan", "check"]
    finally:
        engine.close()


def test_uncertain_send_waits_for_its_requester(desk):
    from hubzoid.work_items import service, store

    hub, log, tmp = desk
    (hub / "work-items.yaml").write_text("version: 1\nwrite_modes:\n  email.send: approve\n")
    engine = Engine(hub, tmp)
    try:
        item, _ = service.admit_and_start(hub, workflow="desk", key="operator:lost",
                                          input={"name": "Bo", "send": "lose"}, requester=OWNER)
        wait_for(hub, item["id"], ("waiting",))
        op = store.list_ops(hub, "desk-hub", item_id=item["id"])[0]
        service.approve(hub, op["id"], OWNER, digest=op["args_digest"])
        blocked = wait_for(hub, item["id"], ("needs_input",))
        assert blocked["reason"] == "operation_ambiguous"
        op = store.get_op(hub, op["id"])
        assert op["state"] == "ambiguous" and "TimeoutError" in op["error"]
        sends = [line for line in lines(log) if line.startswith("send")]
        assert len(sends) == 1                   # no automatic second send
        service.resolve(hub, op["id"], OWNER, decision="retry", acknowledge_duplicate_risk=True)
        done = wait_for(hub, item["id"], ("completed",))
        assert done["result_json"]["result"]["sent"] == {"message_id": "m-1"}
        assert len([line for line in lines(log) if line.startswith("send")]) == 2
    finally:
        engine.close()


def test_incompatible_code_stops_before_model_and_restarts_from_stage(desk):
    from hubzoid.work_items import engine as wi_engine
    from hubzoid.work_items import service

    hub, log, tmp = desk
    engine = Engine(hub, tmp)
    try:
        item, _ = service.admit_and_start(hub, workflow="desk", key="operator:upgrade",
                                          input={"name": "Cy", "slow": True}, requester=OWNER)
        deadline = time.time() + 30
        while "plan" not in lines(log) and time.time() < deadline:
            time.sleep(0.2)
        time.sleep(1)
        engine.kill()                   # interrupted inside the second stage
    finally:
        engine.close()
    main = hub / "workflows" / "desk" / "main.py"
    main.write_text(main.read_text().replace("VERSION = 1", "VERSION = 2"))
    engine = Engine(hub, tmp)           # new code: the old execution must not resume on it
    try:
        blocked = wait_for(hub, item["id"], ("needs_input",))
        assert blocked["reason"] == "definition_changed"
        assert lines(log).count("check") == 0           # no stage ran on the new code
        with pytest.raises(Exception, match="no saved checkpoint"):
            service.restart(hub, item["id"], OWNER, stage="check", reason="checked the change")
        restarted = service.restart(hub, item["id"], OWNER, reason="checked the change")
        assert restarted["workflow_id"] == wi_engine.execution_id(item["id"], 2)
        wait_for(hub, item["id"], ("waiting",))
        assert lines(log).count("plan") == 1            # the earlier stage was reused
        assert lines(log).count("check") == 1
    finally:
        engine.close()
