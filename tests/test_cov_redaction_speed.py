"""Scrubbing credentials from free text (hubzoid.redaction.scrub_text) in
linear time. The `key: value` rule (and the URL credential rule) used to
rescan the rest of a word from every position, so 50 KB of one long word (a base64 blob, a hex dump,
minified code in a command's output) took about 100 s and stalled the tool
call. It must still redact every key form it did, and give the same result as
before (a differential property against the old pattern, kept here only as
the oracle)."""
from __future__ import annotations

import re
import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hubzoid import redaction
from hubzoid.redaction import REDACTED

OLD_KEY_VALUE = re.compile(
    r"""(?ix)
    (["']?[\w-]*(?:token|password|passwd|secret|api[_-]?key|authorization)[\w-]*["']?\s*[:=]\s*)
    ("[^"]*"|'[^']*'|[^\s,;}&]+)
    """)


@pytest.mark.parametrize("raw,want", [
    ("password=hunter2", f"password={REDACTED}"),
    ('{"api_key": "abc123"}', f'{{"api_key": "{REDACTED}"}}'),
    ("X-Api-Key: abc123", f"X-Api-Key: {REDACTED}"),
    ("db.secret = s3cr3t; next", f"db.secret = {REDACTED}; next"),
    ('x"token": "v"', f'x"token": {REDACTED}'),
    ("GITHUB_PERSONAL_ACCESS_TOKEN='p'", f"GITHUB_PERSONAL_ACCESS_TOKEN={REDACTED}"),
    ("nothing secret here", "nothing secret here"),
    ("clone https://ana:pw@git.example.com/repo", f"clone https://{REDACTED}@git.example.com/repo"),
    ("see git+ssh://deploy@host:22/x", f"see git+ssh://{REDACTED}@host:22/x"),
    # Found by the implementation review: after punctuation, and keys of any length.
    ("+https://alice:hunter2@host/repo", f"+https://{REDACTED}@host/repo"),
    ("x" * 101 + "token=opaque-value", "x" * 101 + f"token={REDACTED}"),
    ("token" + "x" * 101 + "=opaque", "token" + "x" * 101 + f"={REDACTED}"),
    ("a=password=secret", f"a=password={REDACTED}"),
])
def test_every_key_form_is_still_redacted(raw, want):
    assert redaction.scrub_text(raw) == want


@pytest.mark.parametrize("text", [
    "x" * 20_000,                                    # one long word
    ("QUJDRA" * 4000),                               # base64-like
    "token" * 4000,                                  # the keyword over and over
    "a-" * 10_000 + "password",
])
def test_long_words_are_scrubbed_in_linear_time(text):
    started = time.perf_counter()
    redaction.scrub_text(text)
    assert time.perf_counter() - started < 2.0       # was about 16 s for 20 KB


fragment = st.sampled_from(["token", "secret", "api_key", "api-key", "password", "auth", "my", "_", "-", "x",
                            "Bearer", '"', "'", ":", "=", " ", ",", ";", "}", "\n", "abc", "12", "."])


@pytest.mark.hypothesis
@given(st.lists(fragment, max_size=30).map("".join))
def test_same_result_as_before(text):
    old = OLD_KEY_VALUE.sub(
        lambda m: m.group(0) if m.group(2).strip("\"'") == REDACTED or m.group(2).lower() in ("bearer", "basic")
        else m.group(1) + REDACTED, text)
    assert redaction._SECRET_TEXT_RE.sub(
        lambda m: m.group(0) if m.group(2).strip("\"'") == REDACTED or m.group(2).lower() in ("bearer", "basic")
        else m.group(1) + REDACTED, text) == old
