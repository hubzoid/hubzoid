"""Emailing a builder-declared list (hubzoid.email_delivery.send_to_list and
list_members): who may send, the list's own rules, and each refusal (subject,
body size, format, a report the run did not publish). Nothing is sent: every
case is refused before delivery, or no SMTP is configured."""
from __future__ import annotations

import pytest

from hubzoid import email_delivery as ed
from hubzoid.workflows.identity import LEGACY_SOURCE, RunIdentity


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "HUBZOID_SMTP_HOST", "HUBZOID_SMTP_FROM", "HUBZOID_EMAIL_DELIVERY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    h = tmp_path / "reports"
    (h / "workflows").mkdir(parents=True)
    (h / "AGENTS.md").write_text("---\nname: reports\n---\nx")
    (h / "workflows" / "settings.yaml").write_text(
        "email_lists:\n  leads: [' Ana@Example.org', ana@example.org, ben@example.org, '']\n  broken: lead@x.org\n")
    return h


PERSON = RunIdentity("ana@example.org", "ana@example.org", "test", "ana@example.org")
SERVICE = RunIdentity("workflow:weekly", None, LEGACY_SOURCE)  # an old service run, not a person


def test_list_members_are_cleaned_and_checked(hub):
    assert ed.list_members(hub, "leads") == ["ana@example.org", "ben@example.org"]
    with pytest.raises(ValueError, match="no email list named 'nope'"):
        ed.list_members(hub, "nope")
    with pytest.raises(ValueError, match="must be a list of addresses"):
        ed.list_members(hub, "broken")


@pytest.mark.parametrize("kw,message", [
    ({"subject": ""}, "The subject must be one line"),
    ({"subject": "Weekly\nBcc: everyone"}, "The subject must be one line"),
    ({"body_format": "pdf"}, "body_format must be text, markdown or html"),
    ({"body": "x" * (ed.MAX_LIST_BODY + 1)}, "Link the report instead"),
    ({"body": "<p>" + "x" * ed.MAX_HTML + "</p>", "body_format": "html"}, "Link the report instead"),
    ({"artifact_ids": ["art-missing"]}, "is not a report this run published"),
    ({"list_name": "nope"}, "no email list named"),
])
def test_each_refusal_sends_nothing(hub, kw, message):
    args = {"hub": "reports", "identity": PERSON, "list_name": "leads", "subject": "Weekly", "body": "Numbers."}
    out = ed.send_to_list(hub, **{**args, **kw})
    assert out["status"] == "refused" and out["sent"] is False and out["results"] == []
    assert message in out["message"]


def test_only_a_person_may_email_a_list(hub):
    out = ed.send_to_list(hub, hub="reports", identity=SERVICE, list_name="leads", subject="Weekly", body="x")
    assert out["status"] == "refused" and out["sent"] is False and "Sending email" in out["message"]


def test_list_footer_escapes_markdown():
    plain = ed._list_footer("leads", "reports", "text")
    assert plain == "You get this because you're on the leads list for reports. To stop, ask the agent's admin."
    md = ed._list_footer("leads_q4", "reports", "markdown")
    assert "leads\\_q4" in md and "admin\\." in md
