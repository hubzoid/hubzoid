---
name: dashboard
description: Build a polished single-file HTML dashboard (charts, KPIs, tables) from data you have gathered, and save it with write_artifact so the user gets a clickable link. Use when the user asks for a chart, graph, dashboard, report, or any visual summary of numbers.
---

# Build a hosted HTML dashboard

You turn data you already have into ONE HTML file and save it with
`write_artifact("<name>.html", <html>)`. The bridge serves it inline as a web
page (sandboxed) and returns a download link — pass that link straight to the
user.

Follow this exactly. The output must look considered, not like a default
template.

## 0. Safety — never inject raw data into the page

The dashboard is HTML you assemble from values that may come from users or
uploaded files. Untrusted text dropped into markup or a `<script>` can break
the page or run as code. So:

- Put chart data in a `<script type="application/json" id="data">…</script>`
  block built with proper JSON, and read it with
  `JSON.parse(document.getElementById('data').textContent)`. Do NOT hand-splice
  values into JS array literals.
- For any text you place in HTML (titles, table cells, labels), escape
  `& < >` (and quotes in attributes). Numbers are safe as-is.
- Never build a `<script>` body by string-concatenating a data value.

## 1. Get the data first

Gather the numbers before you draw anything — from `read_knowledge`,
`read_upload`, `grep_data`, a tool result, or the conversation. Never invent
data points. If a value is unknown, leave it out and say so; do not fill gaps
with plausible-looking numbers. If there is essentially nothing to chart, say
so in prose instead of making an empty dashboard.

## 2. Pick the right chart for the question

| The question is about… | Use |
|---|---|
| Comparing categories | horizontal or vertical **bar** |
| Change over time | **line** (or area for one series) |
| Parts of a whole (≤5 parts) | **doughnut** — never a pie with many slices |
| One headline number | a **KPI stat tile**, not a chart |
| Many rows of exact values | an HTML **table**, not a chart |

Prefer the simplest form that answers the question. One clear chart beats five
noisy ones. Do not use 3D, gradients-as-data, or dual y-axes.

## 3. Compose the HTML

Write ONE file. Requirements:

- **Chart.js via CDN**, pinned: load
  `https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js`. Do not
  rely on any other external asset — no web fonts, no CSS frameworks, no
  images. Everything else is inline. (The page is served standalone; a viewer
  with internet gets the chart. If the deployment is air-gapped, tell the user
  the chart needs the CDN.)
- **Layout**: a title, an optional one-line subtitle with the data's as-of
  date, a row of KPI tiles for headline numbers, then the chart(s), then any
  detail table. Use CSS grid; keep generous whitespace.
- **Theme**: support light AND dark via
  `@media (prefers-color-scheme: dark)`. Never hard-code a single background.
- **Type**: system font stack
  (`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`), one
  bold weight for headings, comfortable line-height, tabular numbers for
  figures (`font-variant-numeric: tabular-nums`).
- **Responsive**: `max-width` container centered; charts inside a wrapper with
  a fixed height (e.g. 320px) and `maintainAspectRatio: false` so they don't
  collapse.

### Color — a small, accessible palette

Use at most 5 series colors, in this order, and reuse them consistently:

```
#2563eb  #059669  #d97706  #7c3aed  #dc2626
(blue)   (green)  (amber)  (violet) (red)
```

One series → just the blue. Sequential magnitude → shades of one hue, not a
rainbow. Never encode meaning in color alone: keep axis labels and a legend so
the chart reads without color too. Ensure text has real contrast in both
themes (light text on dark bg, dark on light).

### Chart.js defaults to set every time

- `responsive: true`, `maintainAspectRatio: false`.
- Turn OFF chartjs gridlines you don't need; keep the axis a hairline.
- Show a legend only when there is more than one series.
- Format numbers in ticks/tooltips (thousands separators, units, `%`).
- Give every axis a title when the unit isn't obvious.

## 4. Save it and hand over the link

```
write_artifact("sales-overview.html", "<the full HTML>")
```

Use a short, descriptive, kebab-case filename ending in `.html`. The tool
returns a markdown download link — give that link to the user and say in one
line what the dashboard shows. Do not paste the HTML into the chat.

## Skeleton to adapt (do not ship verbatim — fill with real data + real title)

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root { --bg:#f8fafc; --card:#fff; --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0b1220; --card:#111a2e; --ink:#e5edff; --muted:#93a4c3; --line:#22304d; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  .wrap { max-width:960px; margin:0 auto; padding:32px 20px; }
  h1 { font-size:1.5rem; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 24px; font-size:.9rem; }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:16px; margin-bottom:24px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }
  .kpi .n { font-size:1.8rem; font-weight:700; font-variant-numeric:tabular-nums; }
  .kpi .l { color:var(--muted); font-size:.8rem; text-transform:uppercase; letter-spacing:.04em; }
  .chart { height:320px; }
  table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); }
  td.num, th.num { text-align:right; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Title of the dashboard</h1>
  <p class="sub">As of 2026-07-10 · source: …</p>
  <div class="kpis">
    <div class="card kpi"><div class="n">1,284</div><div class="l">Total</div></div>
    <!-- more KPI tiles -->
  </div>
  <div class="card"><div class="chart"><canvas id="c1"></canvas></div></div>
</div>
<!-- Data goes here as JSON — never spliced into the script below. -->
<script type="application/json" id="data">{"labels":["A","B","C"],"values":[12,19,7]}</script>
<script>
  const D = JSON.parse(document.getElementById('data').textContent);
  const ink = getComputedStyle(document.documentElement).getPropertyValue('--ink').trim();
  Chart.defaults.color = ink;
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  new Chart(document.getElementById('c1'), {
    type: 'bar',
    data: {
      labels: D.labels,
      datasets: [{ label:'Count', data:D.values, backgroundColor:'#2563eb', borderRadius:6 }]
    },
    options: {
      responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{ display:false } },
      scales:{ y:{ beginAtZero:true, ticks:{ callback:v=>v.toLocaleString() } } }
    }
  });
</script>
</body>
</html>
```

Keep it honest, legible, and easy to scan. When in doubt, remove decoration.
