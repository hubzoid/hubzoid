"""`download_message_files` skips files we've already uploaded this thread.

Slack's `conversations.replies` returns the full thread on every turn,
so without dedup we re-fetch every attachment on every message — wastes
Slack rate budget AND re-pays the bridge's per-upload overhead.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from hubzoid.slack import files as slack_files


def _info(file_id: str, name: str = "doc.txt") -> dict:
    return {
        "file": {
            "id": file_id,
            "name": name,
            "mimetype": "text/plain",
            "url_private_download": f"https://files.slack.com/{file_id}/x",
            "size": 12,
        }
    }


def _ok_http():
    http = MagicMock()
    http.get.return_value = MagicMock(status_code=200, content=b"abc", headers={})
    http.post.return_value = MagicMock(status_code=200)
    return http


def test_already_seen_file_ids_are_skipped():
    """File id already in `already_seen` -> no slack call, no bridge call."""
    slack_client = MagicMock()
    slack_client.files_info.return_value = _info("F1", "doc.txt")
    http = _ok_http()
    seen = {"F1"}
    result = slack_files.download_message_files(
        history=[{"ts": "t1", "files": [{"id": "F1"}]}],
        slack_client=slack_client,
        http=http,
        bridge_url="http://x/v1",
        api_key="k",
        chat_id="slack-t1",
        bot_token="xoxb",
        max_upload_bytes=1024 * 1024,
        already_seen=seen,
    )
    slack_client.files_info.assert_not_called()
    http.get.assert_not_called()
    http.post.assert_not_called()
    assert result == {}


def test_new_file_id_is_processed_and_added_to_seen_set():
    """A file not in `already_seen` is uploaded and the set is mutated."""
    slack_client = MagicMock()
    slack_client.files_info.return_value = _info("F2", "new.txt")
    http = _ok_http()
    seen: set[str] = set()
    result = slack_files.download_message_files(
        history=[{"ts": "t1", "files": [{"id": "F2"}]}],
        slack_client=slack_client,
        http=http,
        bridge_url="http://x/v1",
        api_key="k",
        chat_id="slack-t1",
        bot_token="xoxb",
        max_upload_bytes=1024 * 1024,
        already_seen=seen,
    )
    assert result == {"t1": ["new.txt"]}
    # The caller's set is mutated so the next turn skips this file.
    assert "F2" in seen


def test_failed_upload_is_not_added_to_seen_set():
    """If bridge POST fails, we leave the id out of `seen` so the next
    turn retries — otherwise a transient failure would permanently lose
    the file."""
    slack_client = MagicMock()
    slack_client.files_info.return_value = _info("F3", "x.txt")
    http = MagicMock()
    http.get.return_value = MagicMock(status_code=200, content=b"abc", headers={})
    http.post.return_value = MagicMock(status_code=413)
    seen: set[str] = set()
    slack_files.download_message_files(
        history=[{"ts": "t1", "files": [{"id": "F3"}]}],
        slack_client=slack_client,
        http=http,
        bridge_url="http://x/v1",
        api_key="k",
        chat_id="slack-t1",
        bot_token="xoxb",
        max_upload_bytes=1024 * 1024,
        already_seen=seen,
    )
    assert "F3" not in seen


def test_already_seen_default_none_preserves_old_behavior():
    """Backward compat: callers that don't pass `already_seen` keep the
    original re-download-every-turn behavior (the dedup is opt-in via
    the keyword arg)."""
    slack_client = MagicMock()
    slack_client.files_info.return_value = _info("F4", "z.txt")
    http = _ok_http()
    # Call twice with no `already_seen` argument.
    for _ in range(2):
        result = slack_files.download_message_files(
            history=[{"ts": "t1", "files": [{"id": "F4"}]}],
            slack_client=slack_client,
            http=http,
            bridge_url="http://x/v1",
            api_key="k",
            chat_id="slack-t1",
            bot_token="xoxb",
            max_upload_bytes=1024 * 1024,
        )
        assert result == {"t1": ["z.txt"]}
    assert slack_client.files_info.call_count == 2
