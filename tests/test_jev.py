"""The Jev adapter (`hubzoid/jev.py`): the request and the answers follow the
documented Decisions API contract, only the dedicated JEV_OPENROUTER_API_KEY is
used, and every failure is a clear error, never an empty answer. No network:
`httpx.post` is replaced. Live calls are in tests/e2e/test_jev_e2e.py.
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest
from sqlalchemy import create_engine, text

from hubzoid import jev

KEY = "sk-or-v1-unit-test-SECRET-0123456789"

NOUL = {"is_billing": {"type": "noul", "instructions": "Is this a billing problem?",
                       "criteria": {"true": "charges or refunds", "false": "anything else"}}}
CHOICE = {"route": {"type": "choice", "instructions": "Which team?",
                    "criteria": {"billing": "charges", "technical_support": "errors",
                                 "account_support": "logins"}}}
SCORE = {"urgency": {"type": "score", "instructions": "How urgent?",
                     "criteria": ["low", "medium", "high"]}}
ALL = {**NOUL, **CHOICE, **SCORE}

# The documented example reply, shaped like the live one (2026-09-26).
REPLY = {
    "id": "gen-dec-1-abc", "model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe",
    "answers": {
        "is_billing": {"type": "noul", "noul": 0.99},
        "route": {"type": "choice", "choice": "billing", "confidence": 0.98,
                  "probabilities": {"billing": 0.99, "technical_support": 0.01, "account_support": 0}},
        "urgency": {"type": "score", "score": 0.76, "confidence": 0.64,
                    "probabilities": {"0": 0.24, "1": 0.76, "2": 0},
                    "legend": {"0": "low", "1": "medium", "2": "high"}},
    },
    "usage": {"input_tokens": 512, "output_tokens": 75, "cost": 2.1504e-05},
}


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    d = tmp_path / "support"
    d.mkdir()
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.delenv("HUBZOID_DEPLOYMENT", raising=False)
    monkeypatch.setenv(jev.KEY_ENV, KEY)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(jev, "_sleep", lambda s: None)
    return d


def _rows(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}")
    try:
        with eng.connect() as c:
            return [dict(r._mapping) for r in c.execute(text("SELECT * FROM hz_usage ORDER BY id"))]
    except Exception:  # noqa: BLE001 — no table: no rows
        return []


def _serve(monkeypatch, *replies):
    """Replace httpx.post with a queue of replies (a Response, a dict for a 200,
    or an exception to raise). Returns the list of requests made."""
    sent, queue = [], list(replies)

    def fake_post(url, json=None, timeout=None, headers=None):
        sent.append({"url": url, "body": json, "headers": headers, "timeout": timeout})
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)

    monkeypatch.setattr(httpx, "post", fake_post)
    return sent


def _reply_for(questions: dict) -> dict:
    return {**REPLY, "answers": {k: REPLY["answers"][k] for k in questions}}


# --- the request and the answers ---------------------------------------------
@pytest.mark.parametrize("questions", [NOUL, CHOICE, SCORE, ALL], ids=["noul", "choice", "score", "combined"])
def test_each_type_and_the_combined_request(hub_dir, tmp_path, monkeypatch, questions):
    sent = _serve(monkeypatch, _reply_for(questions))
    out = jev.call(hub_dir, {"state": {"ticket": "charged twice"}, "questions": questions},
                   subject="workflow:triage")
    assert sent[0]["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert sent[0]["body"] == {"model": "typesafe/jev-1.13", "state": {"ticket": "charged twice"},
                               "questions": questions}
    assert set(out["answers"]) == set(questions)
    for name, q in questions.items():
        assert out["answers"][name]["type"] == q["type"]
    (row,) = _rows(tmp_path)
    assert (row["kind"], row["surface"], row["subject"], row["status"]) == (
        "jev", "workflow", "workflow:triage", "ok")
    assert (row["model"], row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (
        "typesafe/jev-1.13-20260917", 512, 75, 2.1504e-05)


def test_answers_are_matched_by_name_not_order(hub_dir, monkeypatch):
    reply = {**REPLY, "answers": dict(reversed(list(REPLY["answers"].items())))}
    _serve(monkeypatch, reply)
    out = jev.call(hub_dir, {"state": "x", "questions": ALL})
    assert out["answers"]["route"]["choice"] == "billing"
    assert out["answers"]["urgency"]["type"] == "score"


def test_the_dedicated_key_is_sent_not_openrouter_api_key(hub_dir, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-chat-key")
    sent = _serve(monkeypatch, _reply_for(NOUL))
    jev.call(hub_dir, {"state": "x", "questions": NOUL})
    assert sent[0]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_openrouter_api_key_is_never_a_fallback(hub_dir, tmp_path, monkeypatch):
    monkeypatch.delenv(jev.KEY_ENV)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-chat-key")
    sent = _serve(monkeypatch, _reply_for(NOUL))
    with pytest.raises(jev.JevConfigError, match="JEV_OPENROUTER_API_KEY"):
        jev.call(hub_dir, {"state": "x", "questions": NOUL})
    assert sent == [] and _rows(tmp_path) == []


def test_missing_key_is_a_clear_configuration_error(hub_dir, monkeypatch):
    monkeypatch.setenv(jev.KEY_ENV, "  ")
    with pytest.raises(jev.JevConfigError) as exc:
        jev.call(hub_dir, {"state": "x", "questions": NOUL})
    assert "dedicated OpenRouter key" in str(exc.value)
    assert "OPENROUTER_API_KEY and the chat model are never used" in str(exc.value)


@pytest.mark.parametrize("questions,match", [
    ({}, "non-empty"),
    ({"q": {"type": "essay", "instructions": "x"}}, "noul"),
    ({"q": {"type": "noul"}}, "instructions"),
    ({"q": {"type": "noul", "instructions": "x", "criteria": {"yes": "a", "no": "b"}}}, '"true" and "false"'),
    ({"q": {"type": "choice", "instructions": "x", "criteria": {"a": "only one"}}}, "two or more choice"),
    ({"q": {"type": "choice", "instructions": "x", "criteria": ["a", "b"]}}, "two or more choice"),
    ({"q": {"type": "score", "instructions": "x", "criteria": {"low": "a", "high": "b"}}}, "list of two"),
    ({"q": {"type": "score", "instructions": "x", "criteria": ["only"]}}, "list of two"),
    ({"q": {"type": "choice", "instructions": "x", "options": {"a": "1", "b": "2"}}}, "unknown field"),
    ({"q": {"type": "noul", "instructions": 7}}, "instructions"),
    ({"q": {"type": "noul", "instructions": "   "}}, "instructions"),
    ({"q": {"type": "choice", "instructions": "x", "criteria": {"a": "1", " ": "2"}}}, "two or more choice"),
    ({"q": {"type": "choice", "instructions": "x", "criteria": {"a": "1", "b": ""}}}, "guidance"),
    ({"q": {"type": "score", "instructions": "x", "criteria": ["low", None]}}, "guidance"),
    ({"q": {"type": "noul", "instructions": "x", "criteria": {"true": "yes", "false": 0}}}, "guidance"),
    ({" ": {"type": "noul", "instructions": "x"}}, "non-empty string name"),
], ids=["empty", "bad-type", "no-instructions", "noul-keys", "choice-one", "choice-list",
        "score-object", "score-one", "unknown-field", "instructions-number", "instructions-blank",
        "blank-label", "empty-guidance", "score-null-level", "noul-number-guidance", "blank-name"])
def test_invalid_questions_fail_before_any_request(hub_dir, tmp_path, monkeypatch, questions, match):
    sent = _serve(monkeypatch, _reply_for(NOUL))
    with pytest.raises(jev.JevQuestionError, match=match) as exc:
        jev.call(hub_dir, {"state": "x", "questions": questions})
    assert isinstance(exc.value, ValueError)   # 1.0.1 raised ValueError here
    assert sent == [] and _rows(tmp_path) == []


def test_noul_criteria_are_optional(hub_dir, monkeypatch):
    _serve(monkeypatch, _reply_for(NOUL))
    q = {"is_billing": {"type": "noul", "instructions": "Is this a billing problem?"}}
    assert jev.call(hub_dir, {"state": "x", "questions": q})["answers"]["is_billing"]["noul"] == 0.99


def test_instructions_and_criteria_may_be_objects_or_lists(hub_dir, monkeypatch):
    _serve(monkeypatch, _reply_for(CHOICE))
    q = {"route": {"type": "choice", "instructions": {"ask": "Which team?", "context": ["support"]},
                   "criteria": {"billing": {"covers": "charges"}, "technical_support": ["errors"],
                                "account_support": "logins"}}}
    assert jev.call(hub_dir, {"state": ["line one", "line two"], "questions": q})["answers"]["route"]


def test_model_must_be_a_model_id(hub_dir):
    with pytest.raises(jev.JevQuestionError, match="model"):
        jev.call(hub_dir, {"state": "x", "questions": NOUL, "model": 5})


@pytest.mark.parametrize("state", [None, "", "   ", {}, [], 42])
def test_state_must_be_text_an_object_or_a_list(hub_dir, state):
    with pytest.raises(jev.JevQuestionError, match="state"):
        jev.call(hub_dir, {"state": state, "questions": NOUL})


def _bad(**over):
    answers = {k: dict(v) for k, v in REPLY["answers"].items()}
    for name, patch in over.items():
        answers[name] = patch if patch is None else {**answers[name], **patch}
    return {**REPLY, "answers": {k: v for k, v in answers.items() if v is not None}}


@pytest.mark.parametrize("reply,match", [
    (httpx.Response(200, text="<html>gateway</html>"), "not JSON"),
    (httpx.Response(200, json=[1, 2]), "not a JSON object"),
    ({"model": "m", "usage": {}}, "no answers"),
    ({**REPLY, "answers": {}}, "no noul answer"),
    (_bad(urgency=None), "no score answer for question 'urgency'"),
    (_bad(route={"type": "noul"}), "no choice answer"),
    (_bad(route={"choice": "legal"}), "not one of the choices"),
    (_bad(is_billing={"noul": 1.5}), "between 0 and 1"),
    (_bad(is_billing={"noul": True}), "between 0 and 1"),
    (httpx.Response(200, content=json.dumps(_bad(is_billing={"noul": float("nan")})).encode()),
     "between 0 and 1"),
    (_bad(urgency={"score": 3}), "between 0 and 2"),
    (_bad(urgency={"score": "high"}), "between 0 and 2"),
    (_bad(route={"probabilities": {"legal": 1}}), "probabilities"),
    (_bad(urgency={"probabilities": {"0": 0.5, "7": 0.5}}), "probabilities"),
    (_bad(route={"confidence": 2}), "confidence"),
    (_bad(urgency={"legend": {"0": "low", "5": "off the scale"}}), "legend"),
    (_bad(urgency={"legend": ["low", "medium", "high"]}), "legend"),
], ids=["html", "list", "no-answers", "empty-answers", "missing-one", "wrong-type", "choice-outside",
        "noul-range", "noul-bool", "noul-nan", "score-range", "score-text", "choice-probs",
        "score-probs", "confidence", "legend-level", "legend-list"])
def test_malformed_replies_fail_instead_of_returning_an_answer(hub_dir, tmp_path, monkeypatch, reply, match):
    sent = _serve(monkeypatch, reply)
    with pytest.raises(jev.JevResponseError, match=match):
        jev.call(hub_dir, {"state": "x", "questions": ALL})
    assert len(sent) == 1                       # a malformed reply is not retried
    (row,) = _rows(tmp_path)
    assert row["status"] == "error"


# --- HTTP failures ---------------------------------------------------------------
def _error(status, message="boom", **headers):
    return httpx.Response(status, json={"error": {"code": status, "message": message}}, headers=headers)


def test_auth_error_is_clear_and_not_retried(hub_dir, tmp_path, monkeypatch):
    sent = _serve(monkeypatch, _error(401, "User not found."))
    with pytest.raises(jev.JevError) as exc:
        jev.call(hub_dir, {"state": "x", "questions": NOUL})
    assert str(exc.value) == "OpenRouter rejected JEV_OPENROUTER_API_KEY (HTTP 401): User not found."
    assert (exc.value.status, exc.value.retryable, len(sent)) == (401, False, 1)
    (row,) = _rows(tmp_path)
    assert (row["status"], row["model"]) == ("error", "typesafe/jev-1.13")


@pytest.mark.parametrize("status,match", [(400, "Jev rejected the request"),
                                          (402, "out of credits"), (403, "rejected JEV_OPENROUTER_API_KEY")])
def test_client_errors_are_not_retried(hub_dir, monkeypatch, status, match):
    sent = _serve(monkeypatch, _error(status))
    with pytest.raises(jev.JevError, match=match):
        jev.call(hub_dir, {"state": "x", "questions": NOUL})
    assert len(sent) == 1


def test_rate_limit_is_retried_once_honouring_retry_after(hub_dir, monkeypatch):
    waits = []
    monkeypatch.setattr(jev, "_sleep", waits.append)
    sent = _serve(monkeypatch, _error(429, "Rate limit exceeded", **{"Retry-After": "3"}), _reply_for(NOUL))
    out = jev.call(hub_dir, {"state": "x", "questions": NOUL})
    assert out["answers"]["is_billing"]["noul"] == 0.99
    assert (len(sent), waits) == (2, [3.0])


def test_retry_after_longer_than_the_cap_is_not_waited_for(hub_dir, monkeypatch):
    waits = []
    monkeypatch.setattr(jev, "_sleep", waits.append)
    sent = _serve(monkeypatch, _error(429, "Rate limit exceeded", **{"Retry-After": "3600"}), _reply_for(NOUL))
    with pytest.raises(jev.JevError) as exc:
        jev.call(hub_dir, {"state": "x", "questions": NOUL})
    assert (len(sent), waits, exc.value.status) == (1, [], 429)
    assert "asked to wait 3600s, longer than Hubzoid waits (10s), so it was not retried" in str(exc.value)


def test_retry_after_as_an_http_date(hub_dir, monkeypatch):
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    waits = []
    monkeypatch.setattr(jev, "_sleep", waits.append)
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=4), usegmt=True)
    _serve(monkeypatch, _error(503, "busy", **{"Retry-After": when}), _reply_for(NOUL))
    jev.call(hub_dir, {"state": "x", "questions": NOUL})
    assert len(waits) == 1 and 2 <= waits[0] <= 4


@pytest.mark.parametrize("value", ["soon", "-5"])
def test_unreadable_or_negative_retry_after_falls_back_safely(hub_dir, monkeypatch, value):
    waits = []
    monkeypatch.setattr(jev, "_sleep", waits.append)
    _serve(monkeypatch, _error(429, **{"Retry-After": value}), _reply_for(NOUL))
    jev.call(hub_dir, {"state": "x", "questions": NOUL})
    assert waits == ([1.0] if value == "soon" else [0.0])


def test_chat_calls_are_attributed_to_the_person_and_chat(hub_dir, tmp_path, monkeypatch):
    _serve(monkeypatch, _reply_for(NOUL))
    jev.call(hub_dir, {"state": "x", "questions": NOUL}, subject="ana@example.org",
             surface="owui", chat_id="chat-42")
    (row,) = _rows(tmp_path)
    assert (row["kind"], row["surface"], row["subject"], row["chat_id"]) == (
        "jev", "web", "ana@example.org", "chat-42")


def test_persistent_rate_limit_fails_after_two_attempts(hub_dir, tmp_path, monkeypatch):
    sent = _serve(monkeypatch, _error(429, "Rate limit exceeded"))
    with pytest.raises(jev.JevError) as exc:
        jev.call(hub_dir, {"state": "x", "questions": NOUL})
    assert str(exc.value) == ("rate limited by OpenRouter (HTTP 429): Rate limit exceeded; "
                              "gave up after 2 attempts")
    assert (exc.value.status, len(sent)) == (429, 2)
    (row,) = _rows(tmp_path)                    # one row per call, not per attempt
    assert row["status"] == "error"


@pytest.mark.parametrize("status", [500, 502, 503, 524, 529])
def test_server_errors_are_retried_once(hub_dir, monkeypatch, status):
    sent = _serve(monkeypatch, _error(status, "Provider returned error"), _reply_for(NOUL))
    assert jev.call(hub_dir, {"state": "x", "questions": NOUL})["answers"]["is_billing"]["type"] == "noul"
    assert len(sent) == 2


def test_persistent_server_error_fails_clearly(hub_dir, monkeypatch):
    _serve(monkeypatch, _error(502, "Provider returned error"))
    with pytest.raises(jev.JevError, match=r"server error \(HTTP 502\): Provider returned error; gave up"):
        jev.call(hub_dir, {"state": "x", "questions": NOUL})


def test_timeouts_are_retried_then_fail_clearly(hub_dir, monkeypatch):
    sent = _serve(monkeypatch, httpx.ReadTimeout("read timed out"))
    with pytest.raises(jev.JevError, match=r"did not answer within 30s; gave up after 2 attempts") as exc:
        jev.call(hub_dir, {"state": "x", "questions": NOUL})
    assert (exc.value.status, exc.value.retryable, len(sent)) == (None, True, 2)
    assert sent[0]["timeout"] == jev.TIMEOUT_S


def test_a_timeout_then_success_returns_the_answer(hub_dir, monkeypatch):
    _serve(monkeypatch, httpx.ConnectTimeout("connect timed out"), _reply_for(NOUL))
    assert jev.call(hub_dir, {"state": "x", "questions": NOUL})["answers"]["is_billing"]["noul"] == 0.99


def test_connection_errors_fail_clearly(hub_dir, monkeypatch):
    _serve(monkeypatch, httpx.ConnectError("nodename nor servname provided"))
    with pytest.raises(jev.JevError, match=r"could not reach OpenRouter \(ConnectError\)"):
        jev.call(hub_dir, {"state": "x", "questions": NOUL})


def test_the_key_never_appears_in_errors_or_logs(hub_dir, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _serve(monkeypatch, _error(500, f"upstream echoed Authorization: Bearer {KEY}"))
    with pytest.raises(jev.JevError) as exc:
        jev.call(hub_dir, {"state": "x", "questions": NOUL})
    assert KEY not in str(exc.value) and "[redacted]" in str(exc.value)
    assert KEY not in caplog.text
    assert "retrying" in caplog.text            # the retry is logged, without the key


def test_errors_survive_a_pickle_round_trip():
    """DBOS stores a failed step's exception; it must come back readable."""
    import pickle

    err = pickle.loads(pickle.dumps(jev.JevError("rate limited (HTTP 429)", status=429, retryable=True)))
    assert str(err) == "rate limited (HTTP 429)"
    q = pickle.loads(pickle.dumps(jev.JevQuestionError("bad")))
    assert isinstance(q, ValueError) and str(q) == "bad"
