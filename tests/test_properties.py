"""Property tests (Hypothesis): rules that must hold for any input, not just the
examples other tests picked. The `ci` profile (tests/conftest.py) runs a few
dozen derandomized examples per test; the nightly workflow runs a thousand
random ones (HYPOTHESIS_PROFILE=nightly)."""
from __future__ import annotations

from urllib.parse import quote

import yaml
from hypothesis import given
from hypothesis import strategies as st

from hubzoid import frontmatter, tool_events

# ---------------------------------------------------------------------------
# Frontmatter: what a hub author writes is what the loaders read back
# ---------------------------------------------------------------------------
_text = st.text(st.characters(exclude_categories=("Cs",)), max_size=40)
_keys = st.from_regex(r"[A-Za-z_][A-Za-z0-9_-]{0,20}", fullmatch=True)
_scalars = st.none() | st.booleans() | st.integers() | _text
_values = _scalars | st.lists(_scalars, max_size=5)


@given(meta=st.dictionaries(_keys, _values, max_size=8), body=_text)
def test_frontmatter_round_trips(meta, body):
    dumped = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) if meta else ""
    text = f"---\n{dumped}---\n{body}"
    parsed, parsed_body = frontmatter.split(text)
    # The same mapping YAML itself reads back. (PyYAML's own dump and load can
    # differ from `meta`: a lone U+0085 comes back as a space.)
    assert parsed == (yaml.safe_load(dumped) or {})
    assert parsed_body == body.strip()


# ---------------------------------------------------------------------------
# Download links: one link per file, the newest one, never one already shown
# ---------------------------------------------------------------------------
_NAMES = ["form.json", "form.json.encoded.txt", "report.pdf", "a b.csv"]


def _url(name: str, token: str, expires: int) -> str:
    return f"http://h/artifacts/chat-1/{quote(name, safe='')}?t={token}&e={expires}"


_writes = st.lists(
    st.tuples(st.sampled_from(_NAMES), st.from_regex(r"[0-9a-f]{8}", fullmatch=True),
              st.integers(1_700_000_000, 1_800_000_000)),
    max_size=12,
)


@given(writes=_writes, echoed=st.sets(st.sampled_from(_NAMES)),
       mangled=st.sets(st.sampled_from(_NAMES)))
def test_footer_has_one_latest_link_per_file(writes, echoed, mangled):
    artifacts = [{"name": n, "url": _url(n, t, e)} for n, t, e in writes]
    latest: dict[str, str] = {}  # last URL recorded per file, in first-seen order
    first: dict[str, str] = {}
    for art in artifacts:
        latest[art["name"]] = art["url"]
        first.setdefault(art["name"], art["url"])
    # The model echoed the first link issued for some files (an older token),
    # and for others wrote a link that may not open: a token we never issued,
    # or an issued link with characters run on.
    shown = " ".join(
        [f"[Download {n}]({first[n]})" for n in sorted(echoed) if n in first]
        + [f"[Download {n}]({_url(n, 'not-issued', 1)})" for n in sorted(mangled)]
        + [f"[Download {n}]({first[n]}9)" for n in sorted(mangled) if n in first]
    )

    footer = tool_events.format_artifact_footer(artifacts, shown_text=shown)
    lines = [ln for ln in footer.splitlines() if ln]
    assert lines == [f"[Download {n}]({u})" for n, u in latest.items() if n not in echoed]
