"""Search result snippets (hubzoid.chat.search)."""
from __future__ import annotations


def test_a_snippet_is_plain_text_that_starts_at_a_word():
    from types import SimpleNamespace

    from hubzoid.chat.search import SearchService

    body = "Some opening words here. " * 4 + "Registration is **stuck** on `confirm_seat`, see [docs](http://x)." + " more" * 80
    r = SimpleNamespace(body_text=body, kind="message", message_id="m", conversation_id="c", title="t",
                        updated_at=1, branch_id="b")
    out = SearchService._result(r, ["stuck"])
    snippet = out["snippet"]
    assert snippet.startswith("…") and snippet.endswith("…")
    assert "**" not in snippet and "`" not in snippet and "](" not in snippet and "confirm_seat" in snippet
    assert snippet[1:].split()[0] in body.split()  # a whole word, not part of one
    a, b = out["highlights"][0]
    assert snippet[a:b] == "stuck"
