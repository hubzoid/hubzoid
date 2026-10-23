---
# Run with: hubzoid eval run <hub>
# Docs: https://github.com/hubzoid/hubzoid/blob/main/docs/evals.md
expect_tools: [stock_check]
contains: ["Fig"]
tags: [demo]
timeout: 240
---
## Prompt
What should we reorder today, and what could run out first?

## Criteria
Uses the stock_check figures. Names the out-of-stock Fig candle and the
products that run out before their next delivery, with suggested quantities.
Groups orders by supplier and flags orders above EUR 2,000 for the operations
lead's approval. Does not invent products, suppliers or numbers.
