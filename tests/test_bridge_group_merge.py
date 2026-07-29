"""The bridge now MERGES group sources (OWUI ∪ resolver/header) instead of one
overriding the other. Non-breaking: OWUI-only and header-only paths are
unchanged; only the both-present path (the new WhatsApp/Telegram case) changes."""
from hubzoid.server import merge_group_sources


def test_owui_only_unchanged():
    assert sorted(merge_group_sources({"coordinator"}, None)) == ["coordinator"]


def test_header_only_unchanged():
    assert sorted(merge_group_sources(set(), "coordinator,admin")) == ["admin", "coordinator"]


def test_union_when_both_present():
    assert sorted(merge_group_sources({"staff"}, "coordinator")) == ["coordinator", "staff"]


def test_duplicate_across_sources_collapses():
    assert merge_group_sources({"coordinator"}, "coordinator") == ["coordinator"]


def test_empty_both_is_empty():
    assert merge_group_sources(set(), None) == []


def test_blank_header_entries_ignored():
    assert sorted(merge_group_sources(set(), "a,, ,b")) == ["a", "b"]
