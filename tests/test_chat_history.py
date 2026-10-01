"""The model prompt for a web app turn: the branch, flattened exactly as the
OpenAI-compatible bridge flattens a chat (server._flatten_messages), with the
bridge's own attachment notes."""
from __future__ import annotations

from hubzoid import memory, server, uploads
from hubzoid.chat.history import build_prompt


def _upload(hub, conv, name, payload, mime):
    folder = memory.chat_upload_dir(hub, conv)
    uploads.write_with_meta(folder, name, payload, mime=mime)
    return folder / name


def test_same_flattening_as_the_openai_compatible_bridge(tmp_path):
    branch = [
        {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
        {"role": "assistant", "content": [
            {"type": "reasoning", "text": "secret thoughts"},
            {"type": "tool-call", "toolCallId": "c", "toolName": "read_knowledge", "args": {}},
            {"type": "text", "text": "Hello! "},
            {"type": "text", "text": "How can I help?"}]},
        {"role": "user", "content": [{"type": "text", "text": "Summarise."}]},
    ]
    prompt = build_prompt(tmp_path, "c_hist00001", branch)
    expected = server._flatten_messages([
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello! How can I help?"},
        {"role": "user", "content": "Summarise."},
    ])
    assert prompt == expected == "[user]\nHi\n\n[assistant]\nHello! How can I help?\n\n[user]\nSummarise."
    assert "secret thoughts" not in prompt and "read_knowledge" not in prompt


def test_empty_assistant_replies_are_skipped(tmp_path):
    branch = [{"role": "user", "content": [{"type": "text", "text": "a"}]},
              {"role": "assistant", "content": []},
              {"role": "user", "content": [{"type": "text", "text": "b"}]}]
    assert build_prompt(tmp_path, "c_hist00001", branch) == "[user]\na\n\n[user]\nb"


def test_attachments_use_the_bridge_notes(tmp_path):
    hub = tmp_path
    png = _upload(hub, "c_hist00002", "chart.png", b"\x89PNG\r\n\x1a\nfake", "image/png")
    pdf = _upload(hub, "c_hist00002", "spec.pdf", b"%PDF-1.4 fake", "application/pdf")
    branch = [{"role": "user", "content": [
        {"type": "text", "text": "Look at these"},
        {"type": "image", "file_id": "chart.png", "name": "chart.png", "mime": "image/png", "size": 12},
        {"type": "file", "file_id": "spec.pdf", "name": "spec.pdf", "mime": "application/pdf", "size": 13},
    ]}]
    prompt = build_prompt(hub, "c_hist00002", branch)
    assert prompt == (
        "[user]\n"
        + server._attachment_note("chart.png", 12, "image/png", png) + "\n\n"
        + server._attachment_note("spec.pdf", 13, "application/pdf", pdf) + "\n\n"
        + "Look at these")
    assert "[Image: chart.png]" in prompt   # what vision_inject expands


def test_images_reach_vision_inject_from_the_conversation_folder(tmp_path):
    from hubzoid import vision_inject

    _upload(tmp_path, "c_hist00003", "cat.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")
    branch = [{"role": "user", "content": [
        {"type": "image", "file_id": "cat.png", "name": "cat.png", "mime": "image/png", "size": 40}]}]
    prompt = build_prompt(tmp_path, "c_hist00003", branch)
    data = vision_inject.openai_input(prompt, tmp_path, "c_hist00003", enabled=True,
                                      max_edge=1568, max_images=4)
    assert isinstance(data, list)
    assert [b["type"] for b in data[0]["content"]] == ["input_text", "input_image"]


def test_non_raster_images_are_offered_as_files(tmp_path):
    svg = _upload(tmp_path, "c_hist00004", "logo.svg", b"<svg/>", "image/svg+xml")
    branch = [{"role": "user", "content": [
        {"type": "file", "file_id": "logo.svg", "name": "logo.svg", "mime": "image/svg+xml", "size": 6}]}]
    prompt = build_prompt(tmp_path, "c_hist00004", branch)
    assert "[Image:" not in prompt
    assert prompt == "[user]\n" + (
        f"[User attached file: logo.svg (6 bytes, image/svg+xml). Read it with "
        f"read_upload('logo.svg'), or pass its on-disk path to a path-accepting tool or "
        f"script: {svg}]")


def test_missing_attachment_is_reported_not_guessed(tmp_path):
    branch = [{"role": "user", "content": [
        {"type": "file", "file_id": "gone.txt", "name": "gone.txt", "mime": "text/plain", "size": 3},
        {"type": "text", "text": "read it"}]}]
    prompt = build_prompt(tmp_path, "c_hist00005", branch)
    assert "[Attachment unreadable: the user attached gone.txt" in prompt
    assert prompt.endswith("read it")
