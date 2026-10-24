# Kestrel & Oak operations hub (an example)

An operations assistant for **Kestrel & Oak, a fictional online shop for
home goods**. Every person, supplier, product and number here is sample data.
It shows what Hubzoid does with a team's own knowledge and data: the agent
reads five short policy files and a stock export, and answers with the team's
numbers and rules instead of general advice.

## Try it

```bash
hubzoid run .
```

The web app opens in your browser. Pick one of the suggested prompts:

- What should we reorder today, and what could run out first?
- Harbor Ceramics will deliver 5 days late. What is at risk?
- A customer opened a blender 40 days ago and wants to return it. What can we offer?

## What is inside

| Path | What it is |
|---|---|
| `AGENTS.md` | The agent's role, sources and rules, and the suggested prompts. |
| `knowledge/` | The company, suppliers, reorder policy, returns policy and how to write to customers. |
| `raw_data/inventory.csv` | A stock export: 21 products with stock, sales per day and open orders. |
| `tools_local/inventory.py` | `stock_check`: days of cover, stockout risk and order quantities from the export. The arithmetic is code, so the same question gets the same numbers. |
| `skills/` | Two procedures: a reorder plan and a supplier delay. |
| `evals/` | Two checks of the agent's answers: `hubzoid eval run .` |
| `schedule/monday-reorder.md` | A weekly reorder plan, off until you enable it. |
| `connectors/.mcp.json` | Where real systems connect through MCP. Empty here. |
| `branding/` | The logo and the browser tab icon. |

## Make it yours

1. Replace `raw_data/inventory.csv` with your own stock export in the same
   columns, or change `_rows()` in `tools_local/inventory.py` to read your
   inventory system.
2. Rewrite the files in `knowledge/` with your suppliers and policies.
3. Edit `AGENTS.md`: the name, the suggested prompts and the rules.

Other starting points: `hubzoid init <name> --template minimal` (one small
example of each file type) and `--template demo` (a guided tour of Hubzoid).
