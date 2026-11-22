"""What an audit record keeps of a tool call (hubzoid.audit_log): the
arguments under HUBZOID_AUDIT_ARGS (property tests: a secret-named argument
never keeps its value; the redacted text masks addresses and card numbers and
is capped) and a result's size."""
from __future__ import annotations

import json
from types import SimpleNamespace

from hypothesis import given
from hypothesis import strategies as st

from hubzoid import audit_log
from hubzoid.redaction import is_secret_key

_names = st.sampled_from(["query", "order_id", "api_key", "password", "client_secret", "access_token",
                          "max_tokens", "note", "bad name", "Authorization"])
_values = st.one_of(st.text(max_size=40), st.integers(), st.floats(allow_nan=False), st.lists(st.integers()),
                    st.none())


@given(args=st.dictionaries(_names, _values, max_size=8))
def test_a_secret_named_argument_never_keeps_its_value(args):
    out = audit_log.summarize_args(args, env={"HUBZOID_AUDIT_ARGS": "names"})
    assert set(out) == {str(k) for k in args}
    for name, value in args.items():
        if is_secret_key(name) or " " in name or not isinstance(value, (str, int, float)):
            assert out[name] in ("…", "[redacted]")          # kept by name only
        if is_secret_key(name) and isinstance(value, str) and len(value) > 3:
            assert value not in json.dumps(out)


@given(local=st.from_regex(r"[a-z]{1,10}", fullmatch=True), digits=st.from_regex(r"\d{16}", fullmatch=True),
       filler=st.text(st.characters(categories=("L",)), max_size=2000))
def test_redacted_arguments_mask_addresses_and_cards_and_are_capped(local, digits, filler):
    email = f"{local}@example.org"
    text = audit_log.summarize_args({"note": f"{filler} write to {email} card {digits}"},
                                    env={"HUBZOID_AUDIT_ARGS": "redacted"})
    assert isinstance(text, str) and len(text) <= 1001
    assert email not in text and digits not in text


def test_argument_shapes():
    env = {"HUBZOID_AUDIT_ARGS": "names"}
    # names: identifier-like arguments keep their value (ids, paths, addresses), others only their name.
    assert audit_log.summarize_args('{"query": "refunds", "order_id": "7"}', env=env) == {"query": "…",
                                                                                         "order_id": "7"}
    assert audit_log.summarize_args("", env=env) == {}
    assert audit_log.summarize_args("plain words", env=env) == {"input": "…"}
    assert audit_log.summarize_args(["a"], env=env) == {"input": "…"}
    assert audit_log.summarize_args(None, env=env) is None
    url = audit_log.summarize_args({"page_url": "https://h.example/p?sig=abc"}, env=env)["page_url"]
    assert url.startswith("https://h.example/p") and "sig=abc" not in url


def test_result_size():
    assert audit_log.size_of(None) is None
    assert audit_log.size_of(b"abc") == 3 and audit_log.size_of("é") == 2
    assert audit_log.size_of({"a": 1}) == len(json.dumps({"a": 1}))
    parts = SimpleNamespace(content=[SimpleNamespace(text="hi"), SimpleNamespace(text="there")])
    assert audit_log.size_of(parts) == len(json.dumps(["hi", "there"]))

    class Unprintable:
        def __str__(self):
            raise RuntimeError("no")

    assert audit_log.size_of(Unprintable()) is None
    assert audit_log.refused_text("[access denied: tool]") and not audit_log.refused_text("fine")
