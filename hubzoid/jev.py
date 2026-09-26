"""Jev: TypeSafe's decision model, called through OpenRouter's Decisions API.

The one adapter for every surface: a workflow's `hub.call_jev` and the
`call_jev` chat tool (gated by the `jev` capability) both call `call()`. Jev
reads a `state` (text, an object or a list) and answers typed questions with
probabilities. It writes no prose.

    POST https://openrouter.ai/api/alpha/decisions   (alpha: shapes may change)

    noul    "does this hold?"        -> {"type": "noul", "noul": 0.97}
    choice  "which of these?"        -> {"type": "choice", "choice": "billing",
                                         "probabilities": {...}, "confidence": 0.9}
    score   "where on this scale?"   -> {"type": "score", "score": 1.8,
                                         "probabilities": {"0": .., "1": ..},
                                         "legend": {...}, "confidence": 0.8}

Credentials: JEV_OPENROUTER_API_KEY, a dedicated OpenRouter key, and nothing
else. There is deliberately no fallback to OPENROUTER_API_KEY, the hub's chat
model or any other runtime, so decisions never run on a key meant for chat. The
key is read from the hub environment at call time, never passed as an argument
(so a workflow checkpoint never contains it), and scrubbed from error text.

Both the request and the answers are checked against the documented contract,
so a malformed or empty reply fails loudly instead of reaching the caller as a
usable decision. Rate limits, server errors and timeouts are retried once.
"""
from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

URL = "https://openrouter.ai/api/alpha/decisions"
KEY_ENV = "JEV_OPENROUTER_API_KEY"
DEFAULT_MODEL = "typesafe/jev-1.13"
TYPES = ("noul", "choice", "score")
QUESTION_FIELDS = frozenset({"type", "instructions", "criteria"})
TIMEOUT_S = 30.0
ATTEMPTS = 2          # one retry, for rate limits, server errors and timeouts
MAX_RETRY_WAIT_S = 10.0
_RETRYABLE = {408, 429, 500, 502, 503, 504, 524, 529}
_sleep = time.sleep   # a seam for tests


class JevError(RuntimeError):
    """A Jev call failed. `status` is the HTTP status when there was one."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class JevConfigError(JevError):
    """JEV_OPENROUTER_API_KEY is not set."""


class JevQuestionError(JevError, ValueError):
    """The questions (or state) do not follow the Decisions API contract."""


class JevResponseError(JevError):
    """The reply does not match the questions that were asked."""


def _guidance(value) -> bool:
    """Instructions and criteria entries: non-empty text, object or list."""
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, (dict, list)) and bool(value)


def validate_questions(questions) -> None:
    """Raise JevQuestionError unless `questions` follows the documented shapes.
    Each question has only `type`, `instructions` and `criteria`. A noul's
    criteria (optional) has exactly "true" and "false", a choice maps two or
    more labels to guidance, and a score lists two or more levels, lowest first."""
    if not isinstance(questions, dict) or not questions:
        raise JevQuestionError("questions must be a non-empty mapping of name -> question")
    for name, q in questions.items():
        if not isinstance(name, str) or not name.strip():
            raise JevQuestionError("every question needs a non-empty string name")
        if not isinstance(q, dict) or q.get("type") not in TYPES:
            raise JevQuestionError(f'question {name!r} needs type "noul", "choice" or "score"')
        unknown = sorted(set(q) - QUESTION_FIELDS)
        if unknown:
            raise JevQuestionError(f"question {name!r} has unknown field(s) {', '.join(unknown)}; "
                                   "a question has only type, instructions and criteria")
        if not _guidance(q.get("instructions")):
            raise JevQuestionError(f"question {name!r} needs instructions")
        crit = q.get("criteria")
        if q["type"] == "noul":
            if crit is None:
                continue
            if not isinstance(crit, dict) or set(crit) != {"true", "false"}:
                raise JevQuestionError(
                    f'noul question {name!r}: criteria must have exactly the keys "true" and "false"')
            entries = crit.values()
        elif q["type"] == "choice":
            if (not isinstance(crit, dict) or len(crit) < 2
                    or not all(isinstance(k, str) and k.strip() for k in crit)):
                raise JevQuestionError(
                    f"choice question {name!r}: criteria must map two or more choice labels to guidance")
            entries = crit.values()
        else:
            if not isinstance(crit, list) or len(crit) < 2:
                raise JevQuestionError(
                    f"score question {name!r}: criteria must be a list of two or more levels, lowest first")
            entries = crit
        if not all(map(_guidance, entries)):
            raise JevQuestionError(f"question {name!r}: every criteria entry needs guidance text")


def validate_state(state) -> None:
    if not _guidance(state):
        raise JevQuestionError("state must be non-empty text, an object or a list")


def _unit(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and -1e-9 <= value <= 1 + 1e-9)


def _check_probabilities(name: str, ans: dict, allowed: set[str]) -> None:
    probs = ans.get("probabilities")
    if probs is not None:
        if not isinstance(probs, dict) or not set(probs) <= allowed or not all(map(_unit, probs.values())):
            raise JevResponseError(f"answer {name!r}: probabilities are not a map of the "
                                   f"allowed values to numbers between 0 and 1")
    if ans.get("confidence") is not None and not _unit(ans["confidence"]):
        raise JevResponseError(f"answer {name!r}: confidence is not a number between 0 and 1")


def validate_answers(questions: dict, data) -> dict:
    """The answers for exactly the asked questions, each checked against its
    type: noul in [0, 1], choice one of the offered labels, score within the
    scale. Raises JevResponseError otherwise (an empty reply is an error)."""
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        raise JevResponseError("the reply has no answers")
    out = {}
    for name, q in questions.items():
        ans = answers.get(name)
        if not isinstance(ans, dict) or ans.get("type") != q["type"]:
            raise JevResponseError(f"no {q['type']} answer for question {name!r}")
        if q["type"] == "noul":
            if not _unit(ans.get("noul")):
                raise JevResponseError(f"answer {name!r}: noul is not a number between 0 and 1")
        elif q["type"] == "choice":
            labels = set(q["criteria"])
            if ans.get("choice") not in labels:
                raise JevResponseError(f"answer {name!r}: {ans.get('choice')!r} is not one of the choices")
            _check_probabilities(name, ans, labels)
        else:
            top = len(q["criteria"]) - 1
            levels = {str(i) for i in range(top + 1)}
            score = ans.get("score")
            if (not isinstance(score, (int, float)) or isinstance(score, bool)
                    or not math.isfinite(score) or not -1e-9 <= score <= top + 1e-9):
                raise JevResponseError(f"answer {name!r}: score is not a number between 0 and {top}")
            _check_probabilities(name, ans, levels)
            legend = ans.get("legend")
            if legend is not None and (not isinstance(legend, dict) or not set(legend) <= levels):
                raise JevResponseError(f"answer {name!r}: legend is not a map of the scale's levels")
        out[name] = ans
    return out


def _retry_after(resp) -> float | None:
    """Seconds from a Retry-After header: a number or an HTTP date."""
    raw = (resp.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        return max(0.0, parsedate_to_datetime(raw).timestamp() - time.time())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _http_error(resp, key: str) -> JevError:
    try:
        detail = resp.json()["error"]["message"]
    except Exception:  # noqa: BLE001 — any non-standard body: show its start
        detail = resp.text
    detail = str(detail).replace(key, "[redacted]")[:300]
    code = resp.status_code
    if code in (401, 403):
        what = f"OpenRouter rejected {KEY_ENV}"
    elif code == 402:
        what = f"the OpenRouter account behind {KEY_ENV} is out of credits"
    elif code == 429:
        what = "rate limited by OpenRouter"
    elif code >= 500 or code in _RETRYABLE:
        what = "OpenRouter or Jev had a server error"
    else:
        what = "Jev rejected the request"
    return JevError(f"{what} (HTTP {code}): {detail}", status=code,
                    retryable=code in _RETRYABLE or code >= 500)


def call(hub_dir, spec: dict, *, subject: str | None = None, surface: str = "workflow",
         chat_id: str | None = None) -> dict:
    """One decision request. `spec` holds `state`, `questions` and optionally
    `model` (default typesafe/jev-1.13). Returns the API's reply with its
    `answers` validated. Records one usage row (kind `jev`) per call, retries
    included, attributed to `subject` on `surface`."""
    import httpx

    from . import usage as usage_lib

    questions = spec.get("questions")
    validate_questions(questions)
    validate_state(spec.get("state"))
    model = spec.get("model") or DEFAULT_MODEL
    if not isinstance(model, str) or not model.strip():
        raise JevQuestionError("model must be a model id such as typesafe/jev-1.13")
    key = (os.environ.get(KEY_ENV) or "").strip()
    if not key:
        raise JevConfigError(
            f"Jev needs {KEY_ENV}: a dedicated OpenRouter key in the hub's .env. "
            "OPENROUTER_API_KEY and the chat model are never used for Jev.")
    body = {"model": model, "state": spec["state"], "questions": questions}
    started = time.monotonic()
    data: dict = {}
    status = "error"
    try:
        for attempt in range(1, ATTEMPTS + 1):
            err: JevError
            wait = float(attempt)
            try:
                resp = httpx.post(URL, json=body, timeout=TIMEOUT_S,
                                  headers={"Authorization": f"Bearer {key}"})
            except httpx.TimeoutException:
                err = JevError(f"Jev did not answer within {TIMEOUT_S:g}s", retryable=True)
            except httpx.HTTPError as exc:
                err = JevError(f"could not reach OpenRouter ({type(exc).__name__})", retryable=True)
            else:
                if resp.status_code < 400:
                    try:
                        data = resp.json()
                    except ValueError:
                        data = {}
                        raise JevResponseError(f"the reply is not JSON (HTTP {resp.status_code})") from None
                    if not isinstance(data, dict):
                        data = {}
                        raise JevResponseError("the reply is not a JSON object")
                    answers = validate_answers(questions, data)
                    status = "ok"
                    log.info("jev: %d answer(s) from %s in %d ms (%s)", len(answers),
                             data.get("model") or model, (time.monotonic() - started) * 1000,
                             data.get("id") or "no id")
                    return {**data, "answers": answers}
                err = _http_error(resp, key)
                asked = _retry_after(resp)
                if asked is not None:
                    wait = asked
            if err.retryable and attempt < ATTEMPTS and wait > MAX_RETRY_WAIT_S:
                err.args = (f"{err.args[0]}; OpenRouter asked to wait {wait:.0f}s, "
                            f"longer than Hubzoid waits ({MAX_RETRY_WAIT_S:g}s), so it was not retried",)
                raise err
            if not err.retryable or attempt == ATTEMPTS:
                if err.retryable:
                    err.args = (f"{err.args[0]}; gave up after {attempt} attempts",)
                raise err
            log.warning("jev: %s; retrying in %.1fs", err, wait)
            _sleep(wait)
        raise AssertionError("unreachable")
    finally:
        u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        usage_lib.record(
            hub_dir, hub=Path(hub_dir).name, surface=surface, kind="jev",
            subject=subject, chat_id=chat_id, model=data.get("model") or model,
            input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
            cost_usd=u.get("cost"), status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
