"""Tests for parsing Open WebUI's RAG-template prompts into clean
user-query + file-path notes.

OWUI persists every upload to `<hub>/.openwebui-data/uploads/<file_id>_<name>`
and embeds the `file_id` and `name` directly in the prompt as
`<source resource-type="file" resource-id="..." name="...">`. We extract
those, resolve to disk paths, and rewrite the prompt so the agent gets
the user's verbatim question plus exact file paths — no glob, no DB
lookup, no correlation heuristic.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def owui_uploads(tmp_path: Path) -> Path:
    d = tmp_path / ".openwebui-data" / "uploads"
    d.mkdir(parents=True)
    return d


def _make_owui_prompt(file_refs: list[tuple[str, str, str]], user_query: str) -> str:
    """Build a realistic OWUI RAG-wrapped prompt.

    file_refs: list of (file_id, name, chunk_content)
    """
    sources = "\n".join(
        f'<source id="{i+1}" name="{name}" resource-type="file" '
        f'resource-id="{file_id}">{chunk}</source>'
        for i, (file_id, name, chunk) in enumerate(file_refs)
    )
    return (
        "### Task:\n"
        "Respond to the user query using the provided context, incorporating "
        "inline citations in the format [id] **only when the <source> tag "
        "includes an explicit id attribute** (e.g., <source id=\"1\">).\n\n"
        "### Guidelines:\n"
        "- If you don't know the answer, clearly state that.\n\n"
        "### Output:\n"
        "Provide a clear and direct response to the user's query, including "
        "inline citations in the format [id] only when the <source> tag with "
        "id attribute is present in the context.\n\n"
        "<context>\n"
        f"{sources}\n"
        "</context>\n\n"
        f"{user_query}"
    )


# ---------------------------------------------------------------------------
# parse_owui_attachment_prompt
# ---------------------------------------------------------------------------
class TestParseOwuiPrompt:
    def test_returns_none_for_non_owui_prompt(self, owui_uploads):
        from hubzoid.owui import parse_owui_attachment_prompt
        # Plain prompt — no <context>, no <source>
        assert parse_owui_attachment_prompt("hello, how are you?", owui_uploads) is None

    def test_returns_none_when_no_source_tags(self, owui_uploads):
        from hubzoid.owui import parse_owui_attachment_prompt
        prompt = "### Task:\nDo something.\n<context>\n</context>\n\nthe query"
        assert parse_owui_attachment_prompt(prompt, owui_uploads) is None

    def test_extracts_single_file_path_and_user_query(self, owui_uploads):
        from hubzoid.owui import parse_owui_attachment_prompt
        # Stage the file on disk at the path OWUI uses.
        file_id = "1e861a57-7a17-4f65-a539-140d7c03a836"
        name = "program-test.json"
        (owui_uploads / f"{file_id}_{name}").write_text("{}")
        prompt = _make_owui_prompt(
            [(file_id, name, '{"key":"chunk content"}')],
            user_query="Review this template. program.",
        )
        result = parse_owui_attachment_prompt(prompt, owui_uploads)
        assert result is not None
        paths, user_query = result
        assert len(paths) == 1
        fname, fpath = paths[0]
        assert fname == name
        assert fpath == owui_uploads / f"{file_id}_{name}"
        assert user_query == "Review this template. program."

    def test_deduplicates_multiple_source_tags_for_same_file(self, owui_uploads):
        """OWUI emits a <source> tag per retrieved chunk — for one file
        across many chunks we should still get one path."""
        from hubzoid.owui import parse_owui_attachment_prompt
        file_id = "abc-123"
        name = "doc.json"
        (owui_uploads / f"{file_id}_{name}").write_text("{}")
        # Three <source> tags, same file
        prompt = _make_owui_prompt(
            [
                (file_id, name, "chunk a"),
                (file_id, name, "chunk b"),
                (file_id, name, "chunk c"),
            ],
            user_query="summarise",
        )
        result = parse_owui_attachment_prompt(prompt, owui_uploads)
        assert result is not None
        paths, _ = result
        assert len(paths) == 1

    def test_multiple_distinct_files(self, owui_uploads):
        from hubzoid.owui import parse_owui_attachment_prompt
        f1, f2 = ("id-a", "a.json"), ("id-b", "b.csv")
        for fid, name in (f1, f2):
            (owui_uploads / f"{fid}_{name}").write_text("x")
        prompt = _make_owui_prompt(
            [(f1[0], f1[1], "chunk a"), (f2[0], f2[1], "chunk b")],
            user_query="compare these",
        )
        result = parse_owui_attachment_prompt(prompt, owui_uploads)
        assert result is not None
        paths, query = result
        names = sorted(n for n, _ in paths)
        assert names == ["a.json", "b.csv"]
        assert query == "compare these"

    def test_skips_files_not_on_disk(self, owui_uploads):
        """If OWUI references a file we can't find (e.g. cleaned up),
        we still parse but drop the missing ones."""
        from hubzoid.owui import parse_owui_attachment_prompt
        # Reference a file that doesn't exist on disk.
        prompt = _make_owui_prompt(
            [("missing-id", "ghost.json", "phantom")],
            user_query="hi",
        )
        result = parse_owui_attachment_prompt(prompt, owui_uploads)
        # All referenced files missing -> None (no attachments to surface).
        assert result is None

    def test_ignores_non_file_resource_types(self, owui_uploads):
        """OWUI also uses <source> tags for knowledge collections; those
        are not files we can read directly."""
        from hubzoid.owui import parse_owui_attachment_prompt
        prompt = (
            "<context>\n"
            '<source id="1" name="my-kb" resource-type="collection" '
            'resource-id="coll-1">vector chunk</source>\n'
            "</context>\n\nask me anything"
        )
        assert parse_owui_attachment_prompt(prompt, owui_uploads) is None

    def test_user_query_is_verbatim_with_no_post_processing(self, owui_uploads):
        from hubzoid.owui import parse_owui_attachment_prompt
        fid, name = "u1", "x.txt"
        (owui_uploads / f"{fid}_{name}").write_text("x")
        # Query has punctuation, multi-line, leading/trailing whitespace.
        prompt = _make_owui_prompt(
            [(fid, name, "c")],
            user_query="Line one.\nLine two.\nLine three.",
        )
        _, query = parse_owui_attachment_prompt(prompt, owui_uploads)
        assert query == "Line one.\nLine two.\nLine three."

    def test_returns_none_when_owui_uploads_dir_missing(self, tmp_path):
        from hubzoid.owui import parse_owui_attachment_prompt
        missing = tmp_path / "nonexistent" / "uploads"
        prompt = _make_owui_prompt([("x", "y.json", "z")], user_query="q")
        # No dir -> can't resolve paths -> None
        assert parse_owui_attachment_prompt(prompt, missing) is None


# ---------------------------------------------------------------------------
# rewrite_prompt_with_attachment_notes — bridge integration helper
# ---------------------------------------------------------------------------
class TestRewritePrompt:
    def test_rewrite_replaces_owui_wrapper_with_clean_notes(self, owui_uploads):
        from hubzoid.owui import rewrite_owui_prompt
        fid, name = "u1", "doc.json"
        (owui_uploads / f"{fid}_{name}").write_text("x")
        wrapped = _make_owui_prompt([(fid, name, "chunk")], user_query="summarise")
        result = rewrite_owui_prompt(wrapped, owui_uploads)
        assert result is not None
        # OWUI's RAG wrapper boilerplate is gone.
        assert "### Task:" not in result
        assert "<context>" not in result
        assert "<source" not in result
        # User query is present at the end, verbatim.
        assert result.endswith("summarise")
        # Attachment note is at the top.
        assert "[User attached file: doc.json" in result
        assert str(owui_uploads / f"{fid}_{name}") in result

    def test_rewrite_returns_none_for_non_owui_prompt(self, owui_uploads):
        from hubzoid.owui import rewrite_owui_prompt
        assert rewrite_owui_prompt("plain question", owui_uploads) is None
