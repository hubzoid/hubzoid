"""Reply splitting preserves content and provider limits, including fenced code."""
from __future__ import annotations

import re

import pytest
from hypothesis import given, strategies as st

from hubzoid.channels import replies


def content(text):
    text = re.sub(r"^\(\d+/\d+\)\n", "", text)
    return "".join(line for line in text.splitlines() if not line.startswith("```")).split()


@given(text=st.text(alphabet="abc XYZ\n😀é", max_size=5000),
       limit=st.integers(240, 600), max_parts=st.integers(1, 20))
def test_split_plain_content_limits_and_overflow(text, limit, max_parts):
    parts = replies.split(text, limit, max_parts)
    full = replies.split(text, limit, 100)
    assert len(parts) <= max_parts
    assert all(len(p) <= limit for p in parts)
    assert all(p.count("```") % 2 == 0 for p in parts)
    if len(full) <= max_parts:
        assert "".join("".join(content(p)) for p in parts) == "".join(text.split())
    else:
        assert parts[-1].endswith(replies.OVERFLOW_NOTE)


@given(lines=st.lists(st.text(alphabet="abc XYZ😀é", min_size=1, max_size=100),
                      min_size=1, max_size=30), limit=st.integers(240, 600))
def test_split_balanced_code_preserves_all_non_whitespace(lines, limit):
    text = "Before\n\n```python\n" + "\n".join(lines) + "\n```\n\nAfter"
    parts = replies.split(text, limit, 100)
    assert len(parts) <= 100
    assert all(len(p) <= limit and p.count("```") % 2 == 0 for p in parts)
    assert "".join("".join(content(p)) for p in parts) == "".join(content(text))


def test_empty_and_single_reply_and_delivery_states():
    assert replies.split(" \n ", 240, 3) == []
    assert replies.split("  hello\nworld  ", 240, 3) == ["hello\nworld"]
    assert not replies.delivered([])
    assert not replies.delivered([{"state": "succeeded"}, {"state": "ambiguous"}])
    assert replies.delivered([{"state": "succeeded"}])
