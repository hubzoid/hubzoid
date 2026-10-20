# Ship the docs and the builder skill in the wheel: `hubzoid docs <topic>`

Status: proposed, waiting for a founder decision. Nothing is built yet.

## Problem

The builder skill (`skills/builder/SKILL.md`) links to pages in `docs/`
instead of repeating them, so the docs stay in one place. A coding agent
that installed Hubzoid from PyPI has neither: the wheel ships no `docs/` and
no `skills/`. Today the agent has to fetch the pages from GitHub, at the tag
that matches `hubzoid --version`. That fails offline, and for an unreleased
install it means reading `main`, which may describe a different version than
the one installed.

The builder journey of 2026-10-20 (HubzoidAgent `factory/builder-skill/proof.html`)
read the pages from an SDK checkout named in the brief. A real builder in a
blank folder would not have one.

## Why now

2.0 makes Claude Code and Codex the main authoring path, and HUB-110 asks
for docs and skills that match the installed release. eve ships its docs in
the npm package for the same reason (`packages/eve/package.json` `files`,
`scripts/copy-docs.mjs`), and its coding-agent skill only says "read the
bundled docs".

## What should happen

- The wheel carries `docs/*.md` and `skills/builder/SKILL.md` as package data,
  from the same files in the repo. Nothing is copied by hand.
- `hubzoid docs list` prints the page ids and titles.
- `hubzoid docs <topic>` prints one page as Markdown (for example
  `hubzoid docs evals`). An unknown topic lists the closest ids.
- `hubzoid docs skill builder --to .claude/skills/hubzoid-builder` writes the
  skill for a project and refuses to overwrite a changed copy.
- The skill then says "read `hubzoid docs <topic>`" instead of a GitHub URL.
- The website renders or links the repo's `docs/`, never its own copy. Today it
  still keeps its own pages, so that is separate website work.

## Scope and non-goals

- No docs server, search index, MCP docs server or website change.
- No manifest hashing, HTTP routes or Help pages in the web app. HUB-110
  describes those. This is its smallest first step, and they can follow.
- `docs/` pages that are internal records (verification records, design
  history) can be left out of the list by a short allowlist.

## Open questions

- Ship every page, or only the journey pages listed in `docs/README.md`?
- The size added to the wheel (about 560 KB of Markdown today) is acceptable?
- Should `hubzoid init` print the `hubzoid docs skill builder` line?

## Definition of done

From a fresh venv with only the built wheel and no network,
`hubzoid docs evals` prints the same text as `docs/evals.md` at that commit,
and `hubzoid docs skill builder --to <dir>` writes the same bytes as
`skills/builder/SKILL.md`. A packaging test checks both.
