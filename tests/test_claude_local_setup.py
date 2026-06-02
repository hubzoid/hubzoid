"""Guards for the claude-local-in-production story (issue #2).

These are doc/config guards: the runtime token path is exercised live in
the e2e suite, but the operator-facing guidance must not silently regress.
"""
from __future__ import annotations

from pathlib import Path

from hubzoid.cli import _STARTER_ENV

_REPO = Path(__file__).resolve().parents[1]
_DEPLOYING = (_REPO / "docs" / "DEPLOYING.md").read_text()


def test_starter_env_documents_subscription_token():
    assert "CLAUDE_CODE_OAUTH_TOKEN" in _STARTER_ENV
    assert "claude setup-token" in _STARTER_ENV
    # Make the "not an API key / not metered" point explicit for operators.
    assert "NOT an API key" in _STARTER_ENV


def test_deploying_has_claude_local_prod_section():
    assert "Running claude-local in production" in _DEPLOYING
    assert "claude setup-token" in _DEPLOYING
    assert "CLAUDE_CODE_OAUTH_TOKEN" in _DEPLOYING


def test_deploying_dropped_the_outdated_no_prod_claim():
    # The old line claimed claude-local "does not work in non-interactive prod".
    assert "does not work in\n  non-interactive prod" not in _DEPLOYING
    assert "it does not work in" not in _DEPLOYING


def test_systemd_unit_is_claude_local_ready():
    # Restart hardened to always; ProtectHome must not hide ~/.claude.
    assert "Restart=always" in _DEPLOYING
    assert "ProtectHome=true" not in _DEPLOYING
    # Home writable so claude can persist session/cache.
    assert "ReadWritePaths=/opt/hubzoid" in _DEPLOYING
