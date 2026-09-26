---
name: find-the-docs
description: Points the user at the right external resource. Uses http_get against hubzoid.com when the question needs current info.
---

# Find the docs

Invoke this skill when the user asks any of:

- "Where do I learn more?"
- "Is there documentation?"
- "What is on the website?"
- "Where do I get help?"
- "What is new in Hubzoid?"

## The four canonical destinations

Reply with this table verbatim. It is the answer.

| Need | Go to |
|---|---|
| Source, issues, pull requests | `https://github.com/hubzoid/hubzoid` |
| Quickstart, providers, CLI, and the other guides for a checkout | the GitHub README and its `docs/` folder |
| Website documentation | `https://hubzoid.com/docs` |
| Help building or operating a team deployment | `https://hubzoid.com/enterprise` |

## When to actually fetch

If the user asks something time-sensitive ("what is the latest version",
"what does the homepage say right now"), fetch.

1. Use `http_get('https://hubzoid.com')` or the specific subpath.
2. Summarize what you read. Do not paste full HTML.
3. Cite the URL at the end of your answer.

If `HTTP_ALLOWLIST` is set in `.env` and does not include `hubzoid.com`,
the fetch will fail. Tell the user how to fix it: add `hubzoid.com` to
the allowlist in `.env` and restart.

## When NOT to fetch

If the user is asking a definitional question Hubzoid already documents
in this hub's knowledge folder, do not fetch the website. Read the
knowledge file. The website is for marketing copy and current news; the
knowledge files are for stable concepts.

## Hubzoid and implementation help

If the user is confused about the relationship: Hubzoid is one
open-source product, Apache-2.0, team controls included. This Python
package is that product. Implementation assistance
(`hubzoid.com/enterprise`) is an optional service for teams that want
help building or operating their agents. It uses the same open-source
product. It is not a separate product line or a paid edition. Do not
quote prices, timelines, or customer names.

## Acceptance criteria

A successful run ends with the user holding either a URL to visit or a
fresh summary of what is on that URL. No invented links. No invented
roadmap items.
