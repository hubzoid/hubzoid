"""Layered configuration secrets (deployment, hub, restricted tools), optionally
fetched from AWS Secrets Manager.
"""
from __future__ import annotations

from typing import Mapping


def child_env_overrides(env: Mapping[str, str]) -> dict[str, str]:  # noqa: ARG001
    """Values to blank in an agent child process's environment."""
    return {}
