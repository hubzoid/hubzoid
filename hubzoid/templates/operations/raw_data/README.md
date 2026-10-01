# raw_data/

`inventory.csv` is a **fictional** stock export for Kestrel & Oak, taken on
Monday 28 September 2026. One row per product:

| Column | Meaning |
|---|---|
| `sku`, `product`, `category` | What the product is. |
| `supplier`, `lead_time_days`, `case_pack` | Who makes it, days from order to delivery, units per case. |
| `unit_cost_eur` | What one unit costs us. |
| `on_hand` | Units in the warehouse at the export. |
| `avg_daily_sales` | Units sold per day, averaged over the last 28 days. |
| `on_order`, `order_arrives_in_days` | An open order and how many days after the export it arrives. Empty when there is none. |

The `stock_check` tool in `tools_local/inventory.py` reads this file. To try
the hub on your own numbers, replace it with an export in the same columns.

Anything else you drop in this folder, the agent can search with `grep_data`
and read with `read_file`.
