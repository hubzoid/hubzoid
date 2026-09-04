---
expect_tools: [runbook]
forbid_tools: [web_search, http_get]
timeout: 120
---
## Prompt
Show me the runbook for DiskFull on db-1.

## Criteria
Calls the runbook tool and reproduces the DiskFull steps from the file,
including the instruction not to delete files when the mount holds the
database and to page the database owner. Does not add steps of its own.
Adding a step that is not in the runbook file, or describing a generic
disk-cleanup procedure instead of the file's steps, is a failure.
