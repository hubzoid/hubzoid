"""Model selection — translate a `MODEL` string into an Agents SDK Model object.

We use LiteLLM as the universal adapter. The model id determines provider:
  openrouter/<provider>/<model>   -> OpenRouter (OPENROUTER_API_KEY)
  openai/<model>                  -> OpenAI (OPENAI_API_KEY)
  anthropic/<model>               -> Anthropic (ANTHROPIC_API_KEY)
  azure/<deployment>              -> Azure OpenAI (AZURE_API_KEY +
                                     AZURE_API_BASE, optional AZURE_API_VERSION)
  <anything else>                 -> LiteLLM auto-detects from the prefix

LiteLLM supports many more providers; v1 documents the four above.

Azure note: the model id is the *deployment name* you created in the Azure
portal, not the underlying model — e.g. `azure/gpt-4o` where "gpt-4o" is your
deployment. AZURE_API_BASE is the resource endpoint
(https://<resource>.openai.azure.com), mirroring WaveAssist's `endpoint`, and
AZURE_API_KEY mirrors its `api_key`. AZURE_API_VERSION defaults if unset.
"""
from __future__ import annotations

import os

from agents.extensions.models.litellm_model import LitellmModel

from . import caching


class MissingProviderKey(RuntimeError):
    pass


def _provider_for(model_id: str) -> str:
    prefix = model_id.split("/", 1)[0].lower()
    return prefix


def _api_key_for(model_id: str) -> str | None:
    """Return the appropriate API key, or None if the env var isn't set."""
    provider = _provider_for(model_id)
    if provider == "openrouter":
        return os.environ.get("OPENROUTER_API_KEY")
    if provider == "openai":
        return os.environ.get("OPENAI_API_KEY")
    if provider == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY")
    if provider == "azure":
        return os.environ.get("AZURE_API_KEY")
    # Fallback: LiteLLM may pick up its own env vars.
    return None


def _base_url_for(model_id: str) -> str | None:
    provider = _provider_for(model_id)
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1"
    if provider == "azure":
        # Azure's resource endpoint, e.g. https://<resource>.openai.azure.com
        return os.environ.get("AZURE_API_BASE")
    return None


def build(model_id: str) -> LitellmModel:
    """Build a LitellmModel for the given id, surfacing missing-key errors clearly."""
    caching.install()
    key = _api_key_for(model_id)
    provider = _provider_for(model_id)
    if key is None and provider in {"openrouter", "openai", "anthropic", "azure"}:
        env_var = {
            "openrouter": "OPENROUTER_API_KEY",
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "azure": "AZURE_API_KEY",
        }[provider]
        raise MissingProviderKey(
            f"MODEL is set to {model_id!r} but {env_var} is not. "
            f"Add it to your hub's .env file."
        )
    base_url = _base_url_for(model_id)
    if provider == "azure" and not base_url:
        raise MissingProviderKey(
            f"MODEL is set to {model_id!r} but AZURE_API_BASE is not. "
            f"Add the Azure resource endpoint "
            f"(https://<resource>.openai.azure.com) to your hub's .env file."
        )
    return LitellmModel(model=model_id, base_url=base_url, api_key=key)
