---
name: briefing-format
description: The page layout, flag thresholds, and one-action rule for the morning briefing.
keywords: [briefing, format, thresholds, layout]
---

# Morning briefing format

One page. Five sections, always in this order. Every figure names its tool
and as-of time once, at the top of its section.

## 1. Headline

Two sentences at most. What kind of day yesterday was, and whether anything
below needs a decision today.

## 2. Sales (from `sales_snapshot`)

Yesterday's orders and revenue, then month to date against the monthly plan.
Name the three largest orders of the day with customer and amount. Flag when
any of these is true.

| Signal | Flag when |
|---|---|
| Revenue, month to date | Below 85 percent of the pro-rated monthly plan |
| Single order | Above 8 percent of the monthly plan on its own |
| Pipeline | Weighted pipeline below 1.5 times the remaining monthly plan |

## 3. Cash (from `cash_position`)

Bank balance, receivables due within 7 days, payables due within 7 days, and
weeks of runway. Flag when any of these is true.

| Signal | Flag when |
|---|---|
| Runway | Below 10 weeks |
| Overdue receivables | Any single customer above 30 days and above 250,000 |
| Payables | Payables due within 7 days exceed bank balance plus receivables due within 7 days |

## 4. Operations (from `ops_snapshot`)

Open orders, orders late against promise date, capacity used, incidents open.
Flag when any of these is true.

| Signal | Flag when |
|---|---|
| Late orders | More than 5 percent of open orders past promise date |
| Capacity | Above 92 percent or below 60 percent |
| Incidents | Any incident open longer than 24 hours |

## 5. One action

At most one recommended action, with the person who should own it. Pick the
flagged item with the largest money at stake. If nothing is flagged, write
"No action needed today" and stop.

## Style

Amounts in the company's reporting currency, rounded to the nearest thousand
above 10,000. Percentages to one decimal. No adjectives that a number could
replace.
