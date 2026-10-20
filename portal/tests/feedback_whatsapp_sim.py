"""WhatsApp ratings for the feedback browser journey (app-feedback-journey.cjs).

Local simulation, in process, while the hub's server is stopped: a signed
Meta webhook (real admission and signature check), Intake release, the
workflow's reply recorded and approved under ``customer_reply.send``, sent
to a fake Meta API (no network), then a signed 👎 swipe-reply that rates it.
Nothing reaches a real WhatsApp number. Prints a JSON summary.

    python feedback_whatsapp_sim.py <hub dir> <approver email>
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import textwrap
import time
from pathlib import Path

import httpx

HUB = Path(sys.argv[1]).resolve()
APPROVER = sys.argv[2]
SECRET, PNID, WABA, SENDER = "e2e-meta-app-secret", "100000000000002", "100000000000001", "919800000001"
os.environ.update(WA_E2E_SECRET=SECRET, WA_E2E_VERIFY="e2e-verify", WA_E2E_TOKEN="EAA-e2e-token")
sent: list[dict] = []


def fake_meta(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith(f"/{PNID}/messages"):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": f"wamid.e2e-out{len(sent)}"}]})
    return httpx.Response(404, json={})


def main() -> None:
    from hubzoid.channels import admission, config, router, store
    from hubzoid.channels.email import transports

    transports._client = httpx.Client(transport=httpx.MockTransport(fake_meta))
    wf = HUB / "workflows" / "requests"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "main.py").write_text(textwrap.dedent('''
        from hubzoid import hub, workflow
        from hubzoid.work_items import Route

        @workflow(work_items={"intake": Route(actions=("customer_reply.send",))})
        def requests():
            return {"ok": True}
    '''))
    (HUB / "work-items.yaml").write_text("version: 1\nwrite_modes:\n  customer_reply.send: approve\n")
    data = config.load_text(HUB, config.render({
        "version": 1, "id": "whatsapp-desk", "provider": "whatsapp_cloud", "enabled": True, "workflow": "requests",
        "run_as": "workflow", "feedback": True,
        "provider_config": {"waba_id": WABA, "phone_number_id": PNID, "app_secret_ref": "env:WA_E2E_SECRET",
                            "verify_token_ref": "env:WA_E2E_VERIFY", "access_token_ref": "env:WA_E2E_TOKEN"}})).data
    config.apply(HUB, data, expected_revision="absent", actor="e2e")
    conn = config.active(HUB, "whatsapp-desk")

    def deliver(*messages):
        body = {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"phone_number_id": PNID},
            "contacts": [{"wa_id": SENDER, "profile": {"name": "Synthetic Customer"}}], "messages": list(messages)}}]}]}
        raw = json.dumps(body).encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        admission.receive(HUB, conn.id, raw, {"X-Hub-Signature-256": sig}, {})
        for row in store.open_deliveries(HUB, config.hub_of(HUB)):
            router._route_one(HUB, row, None)

    from hubzoid import db
    from hubzoid.access import store_for
    from hubzoid.channels import thread
    from hubzoid.intake import service as intake
    from hubzoid.work_items import operations
    from hubzoid.work_items import service as wi
    from hubzoid.work_items import store as wstore
    from hubzoid.work_items.errors import Parked
    from hubzoid.workflows import context

    wi.start = lambda hub_dir, item: item          # no durable engine in this simulation
    operations._approver_still_allowed = lambda hub_dir, hub, who: bool(who)
    now = int(time.time())
    deliver({"from": SENDER, "id": "wamid.e2e-in1", "timestamp": str(now), "type": "text",
             "text": {"body": "Synthetic order 42: when will it ship?"}})
    item = next(i for i in intake.list_items(HUB)["items"] if i["channel_id"] == conn.id)
    store_for(HUB).grant(APPROVER, config.hub_of(HUB), "workflows_manage", actor="e2e")
    out = intake.release(HUB, item["id"], actor=APPROVER, reason="synthetic test sender",
                         expected_revision=item["revision"])
    work = wstore.get_item(HUB, out["work_item_id"])

    def post():
        with context.run_scope(hub=config.hub_of(HUB), workflow="requests", hub_dir=HUB,
                               engine=db.operational_engine(HUB), run_id="e2e-run", work={"item": work, "stage": None}):
            return thread.for_current_run().post("Order 42 ships on Friday from the North warehouse.",
                                                 key="answer", kind="result")

    try:
        post()
    except Parked as parked:
        op = wstore.get_op(HUB, parked.operation_id)
    wstore.update_op(HUB, op["id"], approval_json={"digest": op["args_digest"], "approver": APPROVER})
    post()
    deliver({"from": SENDER, "id": "wamid.e2e-r1", "timestamp": str(now + 5), "type": "text",
             "text": {"body": "👎 Friday is too late, we need it Wednesday"},
             "context": {"from": "15550100", "id": "wamid.e2e-out1"}})
    (wf / "main.py").unlink()                       # the server is not asked to run this workflow
    print(json.dumps({"sent": len(sent), "footer": sent[0]["text"]["body"].splitlines()[-1] if sent else None,
                      "approved_op": op["id"], "intake_item": item["id"]}))


if __name__ == "__main__":
    main()
