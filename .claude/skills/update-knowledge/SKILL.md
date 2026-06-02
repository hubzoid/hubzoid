---
name: update-knowledge
description: Use when refreshing a hub's knowledge/ docs from recent source-code commits — folds new commits in a hub's raw_data/ repos into its knowledge/*.md. Triggered manually or by `hubzoid knowledge refresh`.
---

# update-knowledge

Keep `<hub>/knowledge/*.md` accurate as the source code under `<hub>/raw_data/`
changes. You work through a **pending worklist** of commits, updating the
affected knowledge docs, until nothing is pending. A `/goal` keeps you going
until the worklist is clear.

## Procedure

1. **See what's pending.**
   ```bash
   hubzoid knowledge pending <hub> --format json
   ```
   Each entry is `{repo, sha, subject}`. Empty output means you're done.

2. **For each pending commit:**
   - Read its diff in the owning repo:
     ```bash
     git -C <hub>/raw_data/<repo> show <sha>
     ```
   - Decide which `<hub>/knowledge/*.md` files it affects. Map by topic:
     an auth change touches the auth doc; a new module may need a new doc or
     a line in the module map. A commit that changes nothing user-facing
     (formatting, internal refactor) may need **no** knowledge change — that's
     fine.
   - Edit the affected docs so they stay accurate. **Preserve each doc's
     frontmatter.** Keep the writing tight and at the same altitude as the
     existing docs (orientation + where to look, not a line-by-line changelog).

3. **Mark the commit handled** (whether or not it needed a doc change):
   ```bash
   hubzoid knowledge mark-done <hub> <sha>
   ```

4. **Repeat** until `hubzoid knowledge pending <hub>` is empty.

## Rules

- Only edit files under `<hub>/knowledge/`. Never touch `raw_data/` or code.
- Do **not** `git commit`. A human reviews your diff and commits, then
  restarts the hub so the new knowledge loads.
- If a large batch, handle commits in small groups and `mark-done` as you go —
  if your session ends early, a fresh run resumes from the remaining pending
  commits (progress is on disk, not in your context).
- When in doubt about which doc a commit affects, prefer updating the module
  map / overview doc over inventing a new file.
