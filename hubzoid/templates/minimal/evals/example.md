---
# One markdown file per thing you want to check. Run them with
#   hubzoid eval run <hub>
# Docs: https://github.com/hubzoid/hubzoid/blob/main/docs/evals.md
#
# Everything below is OPTIONAL — a case can be just a "## Prompt" section.
contains: ["hubzoid"]        # substrings the reply must have (case-insensitive)
# expect_tools: [hello]      # tools that MUST be called
# forbid_tools: [http_get]   # tools that MUST NOT be called
# not_contains: ["as an AI"] # substrings the reply must not have
# timeout: 120               # hard bound in seconds
# threshold: 7               # judge pass mark out of 10
# tags: [canary]             # for `--tag canary`
# schedule: "0 6 * * 1"      # run itself weekly inside `hubzoid run`
---
## Prompt
In one sentence, what are you?

## Criteria
Describes itself as this hub's assistant. Does not claim to be a
general-purpose chatbot or a different product.

# A "## Criteria" section is what turns the model judge on — there is no
# separate flag. Delete it and this case runs only the free checks above.
# The judge grades against your AGENTS.md, so rules you wrote there are
# already enforced; you do not restate them here.
