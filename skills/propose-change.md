---
name: propose-change
description: Turn an idea for changing Hubzoid into a short proposals/ document and open the PR. Use when someone wants a non-trivial change and there is no proposal yet, or hands you a draft to file.
---

# propose-change

Turn an idea into a `proposals/` document and open a PR. Keep it light.

For a **small, obvious** fix (typo, doc, a clearly-scoped bug with a test) there
is no proposal — just make the change and open a PR with `git commit -s`. This
skill is for anything bigger.

## 1. Interview — one question at a time

Never batch, never present a form. Walk the template sections in the person's own
words:

- **Problem** — what goes wrong today? The last time it happened.
- **Why now** — why this, now?
- **What should happen** — the behaviour they want, plainly.
- **Scope and non-goals** — what is deliberately out.
- **Definition of done** — how we'd know it's right, something observable.

**Never invent detail.** If they can't answer something, write it down as an open
question. A thin idea with honest gaps is a useful proposal. An inflated one is
not.

## 2. Write it

Copy `proposals/TEMPLATE.md` to `proposals/YYYY-MM-DD-<slug>.md` (today's date;
slug from the title, lowercased and hyphenated). Write their meaning, not an
embellished version. Show them the full path before writing.

Keep the house style: short sentences, concrete nouns, lead with the answer, no
marketing tone.

## 3. Read it back

Judge whether it's genuinely clear: is the problem concrete, does "what should
happen" describe behaviour rather than restate the problem, is the definition of
done observable, where could two readers diverge? Ask the person about any real
gap, fold in the answer. Stop when it's either complete or honestly incomplete
with the gaps named.

## 4. Open the PR — ask first

Opening a PR is outward-facing. Ask, then:

```
git checkout -b proposal/<slug>
git add proposals/<file>
git commit -s -m "proposal: <title>"
git push -u origin proposal/<slug>
gh pr create --title "proposal: <title>" --body-file proposals/<file>
```

Only the one file. The document is the PR body.

## Must refuse to

- Write a proposal for someone who hasn't been interviewed.
- Fill a section from your own knowledge instead of asking.
- Open a PR without asking.
- Turn a small obvious fix into a proposal — just send the fix.

## After it merges

Nothing watches `main`. A person runs `implement-proposal` against the merged
document when they want it built. Tell them that's the next step.
