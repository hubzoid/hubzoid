"""Unit tests for hubzoid.tools._caps.truncate_with_overflow."""
from __future__ import annotations

from pathlib import Path

from hubzoid.tools._caps import truncate_with_overflow


def test_under_cap_returns_text_unchanged(tmp_path: Path):
    body, overflow = truncate_with_overflow(
        "hello", cap=100, overflow_dir=tmp_path / "out", label="read"
    )
    assert body == "hello"
    assert overflow is None
    # No spill dir created when nothing was written.
    assert not (tmp_path / "out").exists()


def test_at_cap_exactly_returns_text_unchanged(tmp_path: Path):
    body, overflow = truncate_with_overflow(
        "x" * 100, cap=100, overflow_dir=tmp_path / "out", label="read"
    )
    assert body == "x" * 100
    assert overflow is None


def test_over_cap_writes_overflow_and_returns_head(tmp_path: Path):
    big = "x" * 50_000
    body, overflow = truncate_with_overflow(
        big, cap=1000, overflow_dir=tmp_path / "out", label="read"
    )
    assert body.startswith("x" * 1000)
    assert "truncated at 1,000 chars" in body
    assert "50,000 total" in body
    assert overflow is not None
    assert overflow.is_file()
    assert overflow.read_text() == big


def test_overflow_path_is_hub_relative_when_hub_dir_given(tmp_path: Path):
    big = "x" * 5_000
    out_dir = tmp_path / "output" / "session-1"
    body, overflow = truncate_with_overflow(
        big,
        cap=100,
        overflow_dir=out_dir,
        label="grep",
        hub_dir=tmp_path,
    )
    # Hint shows path relative to hub_dir, not absolute.
    assert "output/session-1/grep-overflow-" in body
    assert str(tmp_path) not in body  # no leaked absolute path


def test_label_is_used_in_overflow_filename(tmp_path: Path):
    _, overflow = truncate_with_overflow(
        "y" * 500, cap=10, overflow_dir=tmp_path, label="grep"
    )
    assert overflow.name.startswith("grep-overflow-")
    assert overflow.suffix == ".txt"


def test_two_calls_do_not_collide(tmp_path: Path):
    _, a = truncate_with_overflow("a" * 500, cap=10, overflow_dir=tmp_path, label="read")
    _, b = truncate_with_overflow("b" * 500, cap=10, overflow_dir=tmp_path, label="read")
    assert a != b
    assert a.is_file() and b.is_file()
