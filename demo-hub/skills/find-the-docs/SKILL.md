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
| Framework source, issues, PRs | `https://github.com/hubzoid/hubzoid` |
| Framework quickstart, CLI reference, README | the GitHub README |
| Consulting practice, customer case studies, pricing | `https://hubzoid.com` |
| Blog, working notes from inside deployments | `https://hubzoid.com/blog` |

## When to actually fetch

If the user asks something time-sensitive ("what is the latest version",
"any new blog posts", "what does the homepage say right now"), fetch.

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

## Hubzoid vs the consulting practice

If the user is confused about the relationship: the framework (this
Python package) is open source and free. The consulting practice
(hubzoid.com) is a paid service that deploys role-scoped hubs for
mid-enterprise organizations in six weeks. Built on the framework.
Separate offerings, same brand.

## Acceptance criteria

A successful run ends with the user holding either a URL to visit or a
fresh summary of what is on that URL. No invented links. No invented
roadmap items.
