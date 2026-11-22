"""Patterns that read untrusted text run in linear time, with the same
results as before for realistic input: email masking in audit records
(hubzoid.audit_log), credential URLs in parity settings
(hubzoid.parity.validate) and table cell attributes in migrated Open WebUI
messages (hubzoid.migrate_openwebui). Each used to rescan the rest of a long
word from every position: 8,000 characters took about a second, a 1 MiB tool
argument hours. The old patterns are kept here only as the oracle."""
from __future__ import annotations

import re
import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hubzoid import audit_log, migrate_openwebui
from hubzoid.parity import validate

CASES = {
    "email": (audit_log._EMAIL, re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "sub"),
    "credential_url": (validate._CREDENTIAL_URL, re.compile(r"[a-z][a-z0-9+.-]*://[^/\s@]*:[^/\s@]*@", re.I),
                       "search"),
    "attr": (migrate_openwebui._ATTR_RE, re.compile(r'([\w-]+)="([^"]*)"'), "findall"),
}


def run(pattern, how, text):
    if how == "sub":
        return pattern.sub("[x]", text)
    if how == "search":
        return pattern.search(text) is not None
    return pattern.findall(text)


@pytest.mark.parametrize("name", sorted(CASES))
@pytest.mark.parametrize("text", ["x" * 30_000, "a-" * 15_000, "a." * 15_000, "1" * 30_000])
def test_long_words_take_linear_time(name, text):
    new, _old, how = CASES[name]
    started = time.perf_counter()
    run(new, how, text)
    assert time.perf_counter() - started < 1.0


def test_known_values():
    assert audit_log._mask_personal("mail ana.b+x@example.co.uk now") == "mail [email] now"
    assert validate._CREDENTIAL_URL.search("postgres://user:pw@db:5432/x")
    assert validate._CREDENTIAL_URL.search("9https://user:pw@host")
    assert not validate._CREDENTIAL_URL.search("https://host/x?a=1")
    assert migrate_openwebui._ATTR_RE.findall(' colspan="2" data-x="a b"') == [("colspan", "2"), ("data-x", "a b")]


piece = st.sampled_from(["a", "b", "1", ".", "+", "-", "_", "@", ":", "/", "//", "://", '"', "=", " ", "\n",
                         "http", "x.org", "user", "pw", "td"])


@pytest.mark.hypothesis
@pytest.mark.parametrize("name", sorted(CASES))
@given(text=st.lists(piece, max_size=25).map("".join))
def test_same_results_as_before(name, text):
    new, old, how = CASES[name]
    assert run(new, how, text) == run(old, how, text)
