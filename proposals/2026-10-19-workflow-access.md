# Workflow access: who may run, schedule, change and see each workflow

## Problem
Today two broad grants cover every workflow of an agent: one to see them and
one to run and control them. A team cannot let an analyst run one report
without also letting them run and cancel everything else, and there is no
way to say who may change a workflow or review a change to it.

## Why now
Workflows from chat and reviewed hub changes both need to know who may run,
schedule, edit and approve a given workflow. Teams that share an agent need
the same control to give a teammate exactly one workflow.

## What should happen
- Ten separate actions per workflow: run, schedule (needs run), edit,
  create, approve (review definition changes only, never a person's approval
  of an action), see definition, see runs, see results, pause and cancel, and
  manage access. No action implies another.
- Grants go to a person, or to everyone in the agent, for one workflow or for
  all of them, including new ones.
- One policy, three ways in: Settings, Workflow access; the CLI
  (`hubzoid workflow-access export|validate|diff|apply|explain|resources`); and
  the API (`/api/workflow-access`). The file `workflows/access.yaml` is the
  same policy as a document. Editing the file alone grants nothing; applying
  it is audited, takes the revision it read, and fails on a concurrent change.
- "Check someone's access" explains why a person may or may not do something.
- Every place a workflow runs or is scheduled checks the same rule: chat
  workflow tools, a report's own "run for me" tool, the Console's run now,
  personal schedules when saved and again when they fire.
- Grants made before this keep working, read through rather than copied, so
  nobody loses access on upgrade and nobody gains another person's results.

## Out of scope
Groups (they arrive with the access work that brings groups back), and
showing other people's results through a shared audience.

## Compatibility
Additive. Two operational tables. Existing grants are honoured as before.
