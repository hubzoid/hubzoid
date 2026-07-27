"""`hubzoid test --file` — stage local files as chat attachments.

`hubzoid test` runs in-process with no bridge, so it never had a chat in
scope and `read_upload` refused every call. `--file` stages the given
files through the *same* `server._persist_attachments` the bridge uses,
under a stable `cli-test` chat id, so the CLI sees byte-identical notes,
sidecars and size caps to real Open WebUI / Slack traffic.

The scope is entered only when `--file` is passed. Without it the command
must behave exactly as before — see `test_no_file_leaves_run_unscoped`,
which guards a real regression: `write_artifact` branches on chat scope
and would start emitting signed bridge URLs pointing at a bridge that
isn't running.
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi import HTTPException
from typer.testing import CliRunner

from hubzoid import cli

runner = CliRunner()

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _uploads(hub: Path, chat_id: str = "cli-test") -> Path:
    return hub / ".hubzoid" / "chats" / chat_id / "uploads"


@pytest.fixture
def hub(tmp_path: Path) -> Path:
    """A bare hub dir. `hubzoid test` needs no scaffolding beyond a path."""
    return tmp_path


# ---------------------------------------------------------------------------
# Staging: files land in the chat's uploads dir with sidecars
# ---------------------------------------------------------------------------
def test_text_file_is_staged_with_sidecar(hub: Path, tmp_path: Path):
    src = tmp_path / "data.csv"
    src.write_text("name,qty\nbolt,4\n", encoding="utf-8")

    cli._stage_test_attachments(
        hub, "cli-test", [src], "how many rows?", max_upload_bytes=1_000_000
    )

    staged = _uploads(hub) / "data.csv"
    assert staged.read_text(encoding="utf-8") == "name,qty\nbolt,4\n"
    assert (_uploads(hub) / "data.csv.hubzoid.json").is_file()


def test_text_file_note_points_the_agent_at_read_upload(hub: Path, tmp_path: Path):
    src = tmp_path / "data.csv"
    src.write_text("name,qty\nbolt,4\n", encoding="utf-8")

    prompt = cli._stage_test_attachments(
        hub, "cli-test", [src], "how many rows?", max_upload_bytes=1_000_000
    )

    assert "read_upload('data.csv')" in prompt
    # The user's own question survives, after the notes.
    assert prompt.endswith("how many rows?")


def test_image_file_note_uses_the_vision_marker(hub: Path, tmp_path: Path):
    src = tmp_path / "chart.png"
    src.write_bytes(PNG_BYTES)

    prompt = cli._stage_test_attachments(
        hub, "cli-test", [src], "what is this?", max_upload_bytes=1_000_000
    )

    # `vision_inject` keys off this exact marker to expand the image into a
    # multimodal block — a read_upload note here would silently skip vision.
    assert "[Image: chart.png]" in prompt
    assert "read_upload" not in prompt


def test_multiple_files_are_all_staged(hub: Path, tmp_path: Path):
    csv = tmp_path / "data.csv"
    csv.write_text("a,b\n1,2\n", encoding="utf-8")
    png = tmp_path / "chart.png"
    png.write_bytes(PNG_BYTES)

    prompt = cli._stage_test_attachments(
        hub, "cli-test", [csv, png], "compare these", max_upload_bytes=1_000_000
    )

    assert (_uploads(hub) / "data.csv").is_file()
    assert (_uploads(hub) / "chart.png").is_file()
    assert "read_upload('data.csv')" in prompt
    assert "[Image: chart.png]" in prompt


def test_no_files_leaves_the_prompt_untouched(hub: Path):
    prompt = cli._stage_test_attachments(
        hub, "cli-test", [], "just a question", max_upload_bytes=1_000_000
    )

    assert prompt == "just a question"
    assert not (hub / ".hubzoid").exists()


# ---------------------------------------------------------------------------
# Size cap: refuse the whole turn rather than stage a partial set
# ---------------------------------------------------------------------------
def test_oversize_attachment_is_refused_and_nothing_is_written(hub: Path, tmp_path: Path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 4096, encoding="utf-8")

    with pytest.raises(HTTPException) as exc:
        cli._stage_test_attachments(
            hub, "cli-test", [big], "read it", max_upload_bytes=1024
        )

    assert exc.value.status_code == 413
    assert "max_upload_bytes" in str(exc.value.detail)
    assert not _uploads(hub).exists()


def test_oversize_file_blocks_its_small_sibling_too(hub: Path, tmp_path: Path):
    small = tmp_path / "small.txt"
    small.write_text("fine", encoding="utf-8")
    big = tmp_path / "big.txt"
    big.write_text("x" * 4096, encoding="utf-8")

    with pytest.raises(HTTPException):
        cli._stage_test_attachments(
            hub, "cli-test", [small, big], "read them", max_upload_bytes=1024
        )

    # All-or-nothing: a half-staged set would let the agent reason over a
    # chat that is missing a file the user believes they attached.
    assert not _uploads(hub).exists()


# ---------------------------------------------------------------------------
# Chat scope is entered ONLY when --file is passed
# ---------------------------------------------------------------------------
class _RecordingRuntime:
    """Stands in for a real backend; records the chat id seen during run()."""

    def __init__(self) -> None:
        self.seen_chat_id: str | None = "<never ran>"
        self.seen_prompt: str = ""

    async def aopen(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def run(self, prompt: str) -> str:
        from hubzoid import _request_ctx

        self.seen_chat_id = _request_ctx.get_chat_id()
        self.seen_prompt = prompt
        return "ok"


@pytest.fixture
def fake_runtime(monkeypatch) -> _RecordingRuntime:
    from hubzoid import runtime as runtime_lib

    rt = _RecordingRuntime()
    monkeypatch.setattr(runtime_lib, "build", lambda *a, **kw: rt)
    return rt


def test_no_file_leaves_run_unscoped(hub: Path, fake_runtime: _RecordingRuntime):
    result = runner.invoke(cli.app, ["test", str(hub), "--prompt", "ping"])

    assert result.exit_code == 0, result.output
    # None means write_artifact keeps using the session output dir and keeps
    # printing a local path instead of a dead bridge URL.
    assert fake_runtime.seen_chat_id is None
    assert fake_runtime.seen_prompt == "ping"


def test_file_scopes_the_run_to_the_cli_test_chat(
    hub: Path, tmp_path: Path, fake_runtime: _RecordingRuntime
):
    src = tmp_path / "notes.txt"
    src.write_text("hello", encoding="utf-8")

    result = runner.invoke(
        cli.app, ["test", str(hub), "--prompt", "summarise", "--file", str(src)]
    )

    assert result.exit_code == 0, result.output
    assert fake_runtime.seen_chat_id == "cli-test"
    assert "read_upload('notes.txt')" in fake_runtime.seen_prompt
    assert (_uploads(hub) / "notes.txt").is_file()


def test_missing_file_is_rejected_before_any_model_call(
    hub: Path, tmp_path: Path, fake_runtime: _RecordingRuntime
):
    missing = tmp_path / "nope.txt"
    result = runner.invoke(
        cli.app,
        ["test", str(hub), "--prompt", "hi", "--file", str(missing)],
    )

    assert result.exit_code != 0
    # Specifically a path-does-not-exist rejection from the option parser —
    # not an "unknown option" or a failure deep inside the run.
    assert "does not exist" in result.output
    assert fake_runtime.seen_chat_id == "<never ran>"


def test_attachments_are_staged_as_data_urls(hub: Path, tmp_path: Path):
    """The CLI must hand `_persist_attachments` a data URL it can parse.

    Guards the seam between the CLI and the bridge helper: a malformed
    data URL is silently skipped by `_DATA_URL_RE`, which would produce a
    run with no notes and no staged file rather than an error.
    """
    src = tmp_path / "data.csv"
    payload = b"a,b\n1,2\n"
    src.write_bytes(payload)

    blocks = cli._attachment_blocks([src])

    assert len(blocks) == 1
    url = blocks[0]["data"]
    assert url.startswith("data:text/csv;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == payload
