# Providers

hubzoid routes model calls through [LiteLLM](https://docs.litellm.ai). The
`MODEL` string in `<hub>/.env` tells LiteLLM which provider to call.
v0.1 documents three providers. many more work but are undocumented.

## OpenRouter

One key, hundreds of models. Best default.

```bash
OPENROUTER_API_KEY=sk-or-v1-...
MODEL=openrouter/anthropic/claude-haiku-4.5
# Examples:
# MODEL=openrouter/openai/gpt-4o-mini
# MODEL=openrouter/meta-llama/llama-3.1-70b-instruct
# MODEL=openrouter/google/gemini-2.0-flash-001
```

Browse models at https://openrouter.ai/models. Prefix with `openrouter/`.

## OpenAI direct

```bash
OPENAI_API_KEY=sk-...
MODEL=openai/gpt-4o-mini
# Other examples:
# MODEL=openai/gpt-4o
# MODEL=openai/o3-mini
```

## Anthropic direct

```bash
ANTHROPIC_API_KEY=sk-ant-...
MODEL=anthropic/claude-haiku-4-5
# Other examples:
# MODEL=anthropic/claude-sonnet-4-6
# MODEL=anthropic/claude-opus-4-7
```

## Per-agent overrides

Any agent (main or sub) can override the default `MODEL` with `model:` in
its frontmatter. Useful for using a cheaper model for routing sub-agents:

```markdown
---
name: triage
description: Decide which deep agent to hand off to.
model: openrouter/openai/gpt-4o-mini
tools: []
---
```

## Other providers (undocumented in v0.1)

LiteLLM supports Azure OpenAI, AWS Bedrock, Google Vertex, Groq, Together,
Fireworks, Cohere, Mistral, Replicate, Ollama, and more. The `MODEL` prefix
plus the corresponding env vars (see LiteLLM docs) usually just work, but
the v0.1 test matrix only covers OpenRouter, OpenAI, and Anthropic.
