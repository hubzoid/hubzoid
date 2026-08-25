---
name: implement-proposal
description: Take an agreed proposals/ document, produce a plan, get it approved, then build and test it and open the implementation PR. Use when a proposal is merged and someone wants it built.
---

# implement-proposal

Read, plan, **gate**, build, test, PR. The gate is the only place you wait.

## 1. Is it real yet

A proposal is agreed only when it's merged to `main`. Check:

```
git fetch origin main
git cat-file -e origin/main:proposals/<file>
```

If it's not on `main`, stop and say which branch you found it on. Reviewing an
unmerged draft is `propose-change`, not this skill.

## 2. Read the ground rules

Read [`AGENTS.md`](../AGENTS.md) first — especially the "keep it thin" rule and
runtime neutrality. Then read the code nearest the change. Your value is that you
follow what the repo already documents instead of improvising.

## 3. Name the gaps

List every question the plan needs that the proposal doesn't answer, and who
would answer it. Don't resolve a gap by quietly assuming and building on it.

## 4. Write the plan

A file-by-file change list, a phased build order, a test plan (`pytest`, plus
`hubzoid doctor` if a hub is involved), the open questions, and the non-goals
carried over. State the blast radius so the approver knows how much scrutiny to
apply.

## 5. The gate — stop

Present the plan and **stop**. No branch, no files written, until a person says
yes. If the open questions are load-bearing, ask them here. Cheaper now than a
rebuild later.

## 6. Build and test

Work in small, verifiable steps. Match the conventions already in the files you
touch rather than importing new ones. Honour the thin rule: prefer deleting to
adding, and rent a standard before writing bespoke code.

- `pytest` green (run the affected tests as you go, the full suite before the PR).
- If a hub is involved, `hubzoid doctor <hub>` clean, and confirm every
  `read_knowledge` / `load_skill` named resolves to a real file.

## 7. Hand it over

```
git checkout -b implement/<slug>
git add <the work>
git commit -s -m "<area>: <what was built>

Proposal: proposals/<file>"
git push -u origin implement/<slug>
gh pr create --title "<area>: <what was built>" --body "..."
```

The PR body: what was built, a link to the proposal, the test result plainly
(including anything that failed or was left out). A human merges. You do not.

## Must refuse to

- Build before the gate.
- Plan from a proposal that isn't on `main`.
- Resolve an open question by silently assuming an answer.
- Report green when a test failed — say so in the PR body.
- Merge your own PR.
