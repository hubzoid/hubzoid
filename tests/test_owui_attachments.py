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
# owui_attachments — resolved / unresolved / query split
# ---------------------------------------------------------------------------
class TestOwuiAttachments:
    def test_none_for_non_owui_prompt(self, owui_uploads):
        from hubzoid.owui import owui_attachments
        assert owui_attachments("just a question", owui_uploads) is None

    def test_none_when_context_has_no_file_sources(self, owui_uploads):
        from hubzoid.owui import owui_attachments
        prompt = (
            "<context>\n"
            '<source id="1" name="kb" resource-type="collection" '
            'resource-id="c1">chunk</source>\n'
            "</context>\n\nask"
        )
        assert owui_attachments(prompt, owui_uploads) is None

    def test_splits_resolved_and_unresolved(self, owui_uploads):
        """A file present on disk is 'resolved'; one OWUI referenced but whose
        bytes are gone is 'unresolved' — surfaced, never silently dropped."""
        from hubzoid.owui import owui_attachments
        here = ("id-here", "here.json")
        (owui_uploads / f"{here[0]}_{here[1]}").write_text("{}")
        prompt = _make_owui_prompt(
            [(here[0], here[1], "chunk"), ("id-gone", "gone.csv", "chunk")],
            user_query="use these",
        )
        result = owui_attachments(prompt, owui_uploads)
        assert result is not None
        resolved, unresolved, query = result
        assert [n for n, _ in resolved] == ["here.json"]
        assert unresolved == ["gone.csv"]
        assert query == "use these"


# ---------------------------------------------------------------------------
# _normalize_owui_uploads — the bridge seam: ONE canonical store
#
# The whole point of the fix: an OWUI upload must land in the SAME per-chat
# uploads dir as a Slack/base64 upload, so read_upload / vision / ticket
# attachments all resolve it from one place. These are the regression guards.
# ---------------------------------------------------------------------------
class TestNormalizeOwuiUploads:
    @staticmethod
    def _canonical(hub_dir: Path, chat_id: str) -> Path:
        return hub_dir / ".hubzoid" / "chats" / chat_id / "uploads"

    def test_copies_file_into_canonical_store_and_marks_read_upload(self, tmp_path, owui_uploads):
        from hubzoid.server import _normalize_owui_uploads
        fid, name = "abc-1", "spec.json"
        (owui_uploads / f"{fid}_{name}").write_text('{"a": 1}')
        prompt = _make_owui_prompt([(fid, name, "chunk")], user_query="review this")

        out = _normalize_owui_uploads(tmp_path, "chat-1", prompt, owui_uploads)

        # 1) file now lives in THE canonical per-chat uploads store (read_upload's dir)
        copied = self._canonical(tmp_path, "chat-1") / name
        assert copied.is_file()
        assert copied.read_text() == '{"a": 1}'
        # 2) prompt carries the canonical read_upload marker; OWUI boilerplate gone
        assert f"read_upload('{name}')" in out
        assert "<source" not in out and "<context>" not in out and "### Task" not in out
        # 2b) the on-disk canonical path is advertised too, so path-accepting tools
        #     / shell scripts (e.g. the IRS test_template.py <file> flow) still work
        assert str(copied) in out
        # 3) user query preserved verbatim at the end
        assert out.endswith("review this")

    def test_image_gets_vision_marker_not_read_upload(self, tmp_path, owui_uploads):
        from hubzoid.server import _normalize_owui_uploads
        fid, name = "img-1", "screenshot.png"
        (owui_uploads / f"{fid}_{name}").write_bytes(b"\x89PNG\r\n\x1a\n")
        prompt = _make_owui_prompt([(fid, name, "chunk")], user_query="what is this")

        out = _normalize_owui_uploads(tmp_path, "chat-2", prompt, owui_uploads)

        assert self._canonical(tmp_path, "chat-2").joinpath(name).is_file()
        # vision_inject expands `[Image: name]` into a real image block at call time
        assert f"[Image: {name}]" in out
        assert "read_upload" not in out

    def test_unresolved_file_becomes_loud_note_not_silent_drop(self, tmp_path, owui_uploads):
        """The failure that burned IRS: a referenced file that isn't on disk must
        produce a visible note the agent relays, never a silent 'not found'."""
        from hubzoid.server import _normalize_owui_uploads
        prompt = _make_owui_prompt([("gone-id", "missing.json", "chunk")], user_query="read it")

        out = _normalize_owui_uploads(tmp_path, "chat-3", prompt, owui_uploads)

        assert out is not None
        assert "missing.json" in out
        assert "could not be read" in out.lower()
        assert out.endswith("read it")

    def test_passthrough_for_non_owui_prompt(self, tmp_path, owui_uploads):
        from hubzoid.server import _normalize_owui_uploads
        assert _normalize_owui_uploads(tmp_path, "chat-4", "plain question", owui_uploads) is None

    def test_image_and_document_same_turn_both_land(self, tmp_path, owui_uploads):
        from hubzoid.server import _normalize_owui_uploads
        (owui_uploads / "d1_report.csv").write_text("a,b\n1,2\n")
        (owui_uploads / "i1_chart.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        prompt = _make_owui_prompt(
            [("d1", "report.csv", "chunk"), ("i1", "chart.png", "chunk")],
            user_query="compare",
        )

        out = _normalize_owui_uploads(tmp_path, "chat-5", prompt, owui_uploads)

        canon = self._canonical(tmp_path, "chat-5")
        assert (canon / "report.csv").is_file() and (canon / "chart.png").is_file()
        assert "read_upload('report.csv')" in out
        assert "[Image: chart.png]" in out
