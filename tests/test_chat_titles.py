"""Conversation titles: the first message at once, then one direct model call
(complete_once, never an agent run) in the background."""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from hubzoid import runtime as runtime_lib
from hubzoid import testing_runtime
from hubzoid.chat import titles
from tests.chat_helpers import ORIGIN, build_app, chat_body, events, make_hub, wait_titles


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def app(hub):
    return build_app()


@pytest.fixture
def client(app):
    return TestClient(app)


def _first(client, app, conv, text_, **kw):
    return client.post("/api/chat", headers=ORIGIN, json=chat_body(
        conv, text_, agent=app.state.chat.model_label, message_id="m_user00001",
        assistant_id="m_asst00001", **kw))


def _conv(client, conv):
    return client.get(f"/api/conversations/{conv}").json()["conversation"]


def test_provisional_title_rules():
    assert titles.provisional("  Plan\nthe   offsite  ") == "Plan the offsite"
    long = titles.provisional("Please summarise the attached quarterly report and highlight "
                              "the three biggest risks for the board")
    assert long.endswith("…") and len(long) <= 61 and " " in long
    assert not long[:-1].endswith(" ")
    assert titles.provisional("") == "New conversation"
    assert titles.provisional("x" * 100) == "x" * 60 + "…"


def test_clean_model_titles():
    assert titles.clean('"Quarterly Budget Review."') == "Quarterly Budget Review"
    assert titles.clean("Title: Offsite planning\nExtra line") == "Offsite planning"
    assert titles.clean("**Hiring plan**") == "Hiring plan"
    assert titles.clean("   ") is None and titles.clean(None) is None
    assert titles.clean("one two three four five six seven eight nine ten") == \
        "one two three four five six seven eight"


def test_title_is_a_direct_call_not_an_agent_run(app, client, monkeypatch):
    replies = []
    completions = []
    runtime = app.state.chat.runtime
    original_stream = runtime.stream_events

    def counting_stream(prompt):
        replies.append(prompt)
        return original_stream(prompt)

    original_complete = testing_runtime.complete

    def counting_complete(spec, *, model_id):
        completions.append(spec)
        return original_complete(spec, model_id=model_id)

    monkeypatch.setattr(runtime, "stream_events", counting_stream)
    monkeypatch.setattr(testing_runtime, "complete", counting_complete)
    monkeypatch.setattr(runtime_lib, "run_once",
                        lambda *a, **k: pytest.fail("a title must never run the agent"))
    r = _first(client, app, "c_title0001", "Plan the quarterly budget review")
    assert r.status_code == 200
    wait_titles()
    assert len(replies) == 1 and len(completions) == 1
    assert "Plan the quarterly budget review" in completions[0]["prompt"]
    conv = _conv(client, "c_title0001")
    assert conv["title"] == "About plan the quarterly budget" and conv["title_source"] == "auto"
    # later messages never retitle
    client.post("/api/chat", headers=ORIGIN, json=chat_body(
        "c_title0001", "another question", agent=None, message_id="m_user00002",
        assistant_id="m_asst00002", parent_id="m_asst00001"))
    wait_titles()
    assert len(completions) == 1
    assert _conv(client, "c_title0001")["title"] == "About plan the quarterly budget"


def test_title_known_during_the_stream_is_sent(app, client, monkeypatch):
    runs = app.state.chat.runs

    async def waits_for_the_title(prompt):  # noqa: ARG001
        import asyncio

        for _ in range(1000):
            run = runs._runs["m_asst00001"]
            if any(c.get("type") == "data-title" and c["data"]["title"].startswith("About")
                   for c in run.history):
                break
            await asyncio.sleep(0.01)
        yield "done"

    monkeypatch.setattr(app.state.chat.runtime, "stream_events", waits_for_the_title)
    r = _first(client, app, "c_title0002", "Plan the offsite")
    evs = events(r.text)
    titles_sent = [e["data"]["title"] for e in evs
                   if isinstance(e, dict) and e.get("type") == "data-title"]
    assert titles_sent == ["Plan the offsite", "About plan the offsite"]
    assert evs[-2]["type"] == "finish"


def test_a_title_arriving_while_the_reply_is_saved_is_still_sent():
    """The final save happens before 'finish'; a title in that window goes out."""
    import asyncio
    from types import SimpleNamespace

    from hubzoid.chat.runs import RunManager
    from hubzoid.chat.stream import MessageBuilder

    async def go():
        manager = RunManager(SimpleNamespace())
        run = SimpleNamespace(conversation_id="c1", closed=False, finished=True,
                              loop=asyncio.get_running_loop(), history=[], subscribers=set(),
                              builder=MessageBuilder("m1", "c1"))
        manager._runs["m1"] = run
        manager.title_ready("c1", "Late title")
        await asyncio.sleep(0)
        return run.history

    assert asyncio.run(go()) == [{"type": "data-title", "data": {"title": "Late title"},
                                  "transient": True}]


def test_a_failed_title_call_keeps_the_provisional_title(app, client, monkeypatch):
    def failing(*a, **k):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(runtime_lib, "complete_once", failing)
    _first(client, app, "c_title0003", "Where are the onboarding documents?")
    wait_titles()
    conv = _conv(client, "c_title0003")
    assert conv["title"] == "Where are the onboarding documents?"
    assert conv["title_source"] == "fallback"


def test_an_unusable_title_keeps_the_provisional_one(app, client, monkeypatch):
    monkeypatch.setattr(runtime_lib, "complete_once", lambda *a, **k: {"text": " \n "})
    _first(client, app, "c_title0004", "hello")
    wait_titles()
    assert _conv(client, "c_title0004")["title_source"] == "fallback"


def test_a_persons_own_title_is_never_replaced(app, client, monkeypatch):
    release = threading.Event()
    original = runtime_lib.complete_once

    def slow(*a, **k):
        release.wait(10)
        return original(*a, **k)

    monkeypatch.setattr(runtime_lib, "complete_once", slow)
    _first(client, app, "c_title0005", "draft the newsletter")
    client.patch("/api/conversations/c_title0005", headers=ORIGIN, json={"title": "Newsletter"})
    release.set()
    wait_titles()
    conv = _conv(client, "c_title0005")
    assert conv["title"] == "Newsletter" and conv["title_source"] == "user"


def test_title_model_can_be_chosen(app, client, monkeypatch):
    specs = []

    def capture(hub_dir, spec, **kwargs):
        specs.append((spec, kwargs))
        return {"text": "Chosen model title"}

    monkeypatch.setenv("HUBZOID_TITLE_MODEL", "hubzoid-test/titles")
    monkeypatch.setattr(runtime_lib, "complete_once", capture)
    _first(client, app, "c_title0006", "hello")
    wait_titles()
    spec, kwargs = specs[0]
    assert spec["model"] == "hubzoid-test/titles"
    assert kwargs == {"subject": "admin@localhost", "surface": "web", "kind": "background",
                      "chat_id": "c_title0006"}
    assert _conv(client, "c_title0006")["title"] == "Chosen model title"


def test_a_file_only_first_message_is_titled_by_its_files(app, client):
    client.post("/api/conversations", headers=ORIGIN,
                json={"agent": app.state.chat.model_label, "id": "c_title0007"})
    up = client.post("/api/conversations/c_title0007/files", headers=ORIGIN,
                     files={"file": ("q3-report.pdf", b"%PDF-1", "application/pdf")}).json()
    body = chat_body("c_title0007", None, agent=None, message_id="m_user00001",
                     assistant_id="m_asst00001", files=(up["file_id"],))
    r = client.post("/api/chat", headers=ORIGIN, json=body)
    assert {"type": "data-title", "data": {"title": "q3-report.pdf"}, "transient": True} in events(r.text)
