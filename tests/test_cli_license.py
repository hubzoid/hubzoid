"""CLI tests for `hubzoid license` (status / keygen / issue / verify)."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from hubzoid import cli, licensing

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LICENSE_KEY", raising=False)
    monkeypatch.delenv("HUBZOID_LICENSE_PUBKEY", raising=False)


def test_keygen_prints_private_and_public_keys():
    res = runner.invoke(cli.app, ["license", "keygen"])
    assert res.exit_code == 0
    assert "PRIVATE" in res.output.upper()
    assert "PUBLIC" in res.output.upper()


def test_issue_then_status_shows_enterprise_features():
    priv, pub = licensing.generate_keypair()
    issued = runner.invoke(
        cli.app,
        [
            "license", "issue",
            "--customer", "Samarth",
            "-f", "scheduling",
            "-f", "access-control",
            "--private-key", priv,
        ],
    )
    assert issued.exit_code == 0, issued.output
    token = issued.output.strip().splitlines()[-1].strip()

    status = runner.invoke(cli.app, ["license", "--key", token, "--pubkey", pub])
    assert status.exit_code == 0, status.output
    out = status.output
    assert "Samarth" in out
    assert "enterprise" in out
    assert "scheduling" in out


def test_status_without_key_reports_community():
    res = runner.invoke(cli.app, ["license"])
    assert res.exit_code == 0
    assert "community" in res.output.lower()


def test_verify_flags_tampered_token():
    priv, pub = licensing.generate_keypair()
    issued = runner.invoke(
        cli.app, ["license", "issue", "--customer", "X", "--private-key", priv]
    )
    token = issued.output.strip().splitlines()[-1].strip()
    payload_b64, sig = token.split(".", 1)
    tampered = "x" + payload_b64[1:] + "." + sig

    res = runner.invoke(
        cli.app, ["license", "verify", "--key", tampered, "--pubkey", pub]
    )
    assert res.exit_code != 0
    assert "invalid" in res.output.lower()
