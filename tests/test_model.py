"""Provider/key resolution for `hubzoid.model.build`.

These exercise the env-var wiring without making any network calls — a
LitellmModel is constructed lazily, so building one is cheap and offline.
"""
from __future__ import annotations

import pytest

from hubzoid import model as modellib


def test_provider_detection():
    assert modellib._provider_for("azure/gpt-4o") == "azure"
    assert modellib._provider_for("openai/gpt-4o-mini") == "openai"
    assert modellib._provider_for("openrouter/anthropic/claude-haiku-4.5") == "openrouter"


def test_azure_requires_api_key(monkeypatch):
    monkeypatch.delenv("AZURE_API_KEY", raising=False)
    monkeypatch.setenv("AZURE_API_BASE", "https://r.openai.azure.com")
    with pytest.raises(modellib.MissingProviderKey, match="AZURE_API_KEY"):
        modellib.build("azure/gpt-4o")


def test_azure_requires_api_base(monkeypatch):
    monkeypatch.setenv("AZURE_API_KEY", "k")
    monkeypatch.delenv("AZURE_API_BASE", raising=False)
    with pytest.raises(modellib.MissingProviderKey, match="AZURE_API_BASE"):
        modellib.build("azure/gpt-4o")


def test_azure_builds_with_endpoint_and_key(monkeypatch):
    monkeypatch.setenv("AZURE_API_KEY", "secret-key")
    monkeypatch.setenv("AZURE_API_BASE", "https://r.openai.azure.com/")
    m = modellib.build("azure/gpt-4o")
    assert m.model == "azure/gpt-4o"
    # base_url flows through to LiteLLM so it hits the Azure resource endpoint.
    assert m.base_url == "https://r.openai.azure.com/"
    assert m.api_key == "secret-key"


def test_openai_still_builds(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    m = modellib.build("openai/gpt-4o-mini")
    assert m.model == "openai/gpt-4o-mini"
    assert m.base_url is None
