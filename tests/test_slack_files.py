"""Slack adapter: file attachments downloaded and forwarded to the bridge.

When a Slack message has a `files` array, the adapter must:
  1. Fetch each file via `url_private_download` with the bot token.
  2. Upload it to the bridge's internal `POST /v1/files`
     (`inbound.uploads.push_upload`) and keep the stored file id.
  3. Put a file part with that id on the message, which the bridge attaches
     to the thread's chat.

Files larger than the bridge's cap, or whose download fails, are skipped
with a warning rather than crashing the turn.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from hubzoid.slack import files as slack_files
from hubzoid.slack.conversion import messages_from_thread


# ---------------------------------------------------------------------------
# download_message_files: the new pure-ish helper
# ---------------------------------------------------------------------------
def _slack_history_with_file(file_id: str = "F123", filename: str = "spec.pdf") -> list[dict]:
    return [
        {
            "user": "U1",
            "ts": "1700000001.000100",
            "text": "have a look",
            "files": [{"id": file_id, "name": filename}],
        },
    ]


def _info_response(file_id: str = "F123", filename: str = "spec.pdf", mimetype: str = "application/pdf") -> dict:
    return {
        "file": {
            "id": file_id,
            "name": filename,
            "mimetype": mimetype,
            "url_private_download": f"https://files.slack.com/{file_id}/download/{filename}",
            "size": 12,
        }
    }


def test_download_uploads_each_file_to_bridge():
    """Single file in history -> one Slack GET + one bridge POST."""
    payload = b"%PDF-1.7 fake"
    slack_client = MagicMock()
    slack_client.files_info.return_value = _info_response()

    http = MagicMock()
    slack_get = MagicMock(status_code=200, content=payload, headers={"content-type": "application/pdf"})
    bridge_post = MagicMock(status_code=201)
    bridge_post.json.return_value = {"file_id": "stored-spec"}
    http.get.return_value = slack_get
    http.post.return_value = bridge_post

    result = slack_files.download_message_files(
        history=_slack_history_with_file(),
        slack_client=slack_client,
        http=http,
        bridge_url="http://127.0.0.1:8000/v1",
        api_key="dev",
        chat_id="slack-1700000001.000100",
        bot_token="xoxb-fake",
        max_upload_bytes=10 * 1024 * 1024,
    )

    # GET went to the private download URL with the bot token.
    slack_call = http.get.call_args
    assert "files.slack.com" in slack_call.args[0]
    assert slack_call.kwargs["headers"]["Authorization"] == "Bearer xoxb-fake"

    # The upload went to the one upload route with the bridge api key.
    bridge_call = http.post.call_args
    assert bridge_call.args[0] == "http://127.0.0.1:8000/v1/files"
    assert bridge_call.kwargs["headers"]["Authorization"] == "Bearer dev"
    assert bridge_call.kwargs["files"]["file"] == ("spec.pdf", payload, "application/pdf")

    # Result maps the message's ts to the stored file ids.
    assert result == {"1700000001.000100": ["stored-spec"]}


def test_download_skips_files_over_size_cap():
    """Slack file `size` exceeds cap -> skipped before download."""
    slack_client = MagicMock()
    slack_client.files_info.return_value = {
        "file": {
            "id": "F999",
            "name": "huge.bin",
            "mimetype": "application/octet-stream",
            "url_private_download": "https://files.slack.com/F999/download/huge.bin",
            "size": 50 * 1024 * 1024,  # 50 MB
        }
    }
    http = MagicMock()
    result = slack_files.download_message_files(
        history=[{"ts": "t1", "files": [{"id": "F999", "name": "huge.bin"}]}],
        slack_client=slack_client,
        http=http,
        bridge_url="http://x/v1",
        api_key="dev",
        chat_id="slack-t1",
        bot_token="xoxb",
        max_upload_bytes=10 * 1024 * 1024,
    )
    http.get.assert_not_called()
    assert result == {}


def test_download_skips_when_slack_get_fails():
    slack_client = MagicMock()
    slack_client.files_info.return_value = _info_response()
    http = MagicMock()
    http.get.return_value = MagicMock(status_code=403, content=b"", headers={})
    result = slack_files.download_message_files(
        history=_slack_history_with_file(),
        slack_client=slack_client,
        http=http,
        bridge_url="http://x/v1",
        api_key="dev",
        chat_id="slack-t1",
        bot_token="xoxb",
        max_upload_bytes=10 * 1024 * 1024,
    )
    http.post.assert_not_called()
    assert result == {}


def test_download_skips_when_bridge_rejects():
    """Bridge 413 (cap exceeded after download) -> skip, don't abort the turn."""
    slack_client = MagicMock()
    slack_client.files_info.return_value = _info_response()
    http = MagicMock()
    http.get.return_value = MagicMock(status_code=200, content=b"data", headers={})
    http.post.return_value = MagicMock(status_code=413)
    result = slack_files.download_message_files(
        history=_slack_history_with_file(),
        slack_client=slack_client,
        http=http,
        bridge_url="http://x/v1",
        api_key="dev",
        chat_id="slack-t1",
        bot_token="xoxb",
        max_upload_bytes=10 * 1024 * 1024,
    )
    assert result == {}


def test_download_multiple_files_in_one_message():
    slack_client = MagicMock()
    slack_client.files_info.side_effect = [
        _info_response(file_id="F1", filename="a.txt", mimetype="text/plain"),
        _info_response(file_id="F2", filename="b.json", mimetype="application/json"),
    ]
    http = MagicMock()
    http.get.return_value = MagicMock(status_code=200, content=b"x", headers={})
    http.post.side_effect = [MagicMock(status_code=201, json=MagicMock(return_value={"file_id": "fa"})),
                             MagicMock(status_code=201, json=MagicMock(return_value={"file_id": "fb"}))]
    history = [
        {
            "ts": "t1",
            "text": "two files",
            "files": [{"id": "F1", "name": "a.txt"}, {"id": "F2", "name": "b.json"}],
        }
    ]
    result = slack_files.download_message_files(
        history=history,
        slack_client=slack_client,
        http=http,
        bridge_url="http://x/v1",
        api_key="dev",
        chat_id="slack-t1",
        bot_token="xoxb",
        max_upload_bytes=10 * 1024 * 1024,
    )
    assert result == {"t1": ["fa", "fb"]}


# ---------------------------------------------------------------------------
# messages_from_thread: attaches notes to the right message
# ---------------------------------------------------------------------------
def test_messages_from_thread_puts_file_parts_on_the_matching_ts():
    history = [
        {"user": "U1", "ts": "t1", "text": "look at this"},
        {"user": "U1", "ts": "t2", "text": "and this one too"},
        {"user": "U2", "ts": "t3", "text": ""},
    ]
    out = messages_from_thread(
        history,
        bot_user_id="UBOT",
        attached_files_by_ts={"t1": ["fa"], "t2": ["fb", "fc"], "t3": ["fd"]},
    )
    assert out[0]["content"] == [{"type": "text", "text": "look at this"},
                                 {"type": "file", "file": {"file_id": "fa"}}]
    assert [p.get("file", {}).get("file_id") for p in out[1]["content"]] == [None, "fb", "fc"]
    assert out[2]["content"] == [{"type": "file", "file": {"file_id": "fd"}}]   # a file with no words


def test_messages_from_thread_without_files_map_unchanged():
    """Default call (no attached_files_by_ts) keeps the existing behaviour."""
    history = [{"user": "U1", "ts": "t1", "text": "hello"}]
    out = messages_from_thread(history, bot_user_id="UBOT")
    assert out == [{"role": "user", "content": "hello"}]
