"""Slack chat surface for HubZoid hubs.

`hubzoid slack <hub>` runs a slack-bolt Socket Mode adapter that forwards
Slack events to the hub's existing OpenAI-compatible bridge. Same hub folder,
same `.env`, same auth — Slack just becomes another client of `/v1/chat/completions`.

See docs/slack.md for the operator walkthrough.
"""
from __future__ import annotations
