---
tags: [canary]
expect_tools: [read_knowledge]
forbid_tools: [metric, web_search]
contains: ["30 days"]
timeout: 120
---
## Prompt
How long do I have to submit an expense claim?

## Criteria
States 30 days from the expense date, with the original receipt, and
mentions that older claims need the finance head's approval. Cites the
policies source. Does not invent a form, a portal, or a step the policy
does not describe.
