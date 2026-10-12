"""Untrusted text in prompts. Repo files, pull request text, comments,
advisories and diffs go inside named blocks (<diff>, <pull_request>, ...).
`fence` defangs anything in that text that looks like one of those block
tags, so a contributor cannot close a block early or forge a trusted one.
Code is otherwise left exactly as written (a `<` in code stays a `<`)."""
from __future__ import annotations

import re

BLOCKS = re.compile(
    r"<(\s*/?\s*(?:pull_request|pull_requests|diff|team_rules|team_context|repo_[a-z_]+|existing_discussion|"
    r"automated_checks|already_decided|previous_findings|linked_issue|ci_status|readme|team_numbers|"
    r"review_guide|untrusted)\b)", re.I)


def fence(text) -> str:
    return BLOCKS.sub(r"‹\1", str(text or ""))
