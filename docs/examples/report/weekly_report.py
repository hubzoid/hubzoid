"""Example workflow: a personal weekly report, published and emailed to its owner.

Copy this file and `report_template.html` into `<hub>/workflows/weekly_report/`
(as `main.py` and `report_template.html`). It reads the rows that belong to the
account the run acts as, renders the template, publishes the page as a private
report and emails that person a link.

The sample data is synthetic: `raw_data/orders.csv` with columns
owner,week,region,orders,revenue. Replace `load_rows` with your own source (for
data behind a person's own connection, ask the agent with `hub.call_agent`,
which acts as the run's account).

Try it locally without an SMTP server:

    HUBZOID_EMAIL_DELIVERY=preview hubzoid schedule run <hub> weekly_report
"""
import csv
import html
from datetime import datetime, timezone
from pathlib import Path
from string import Template

from hubzoid import hub, step, workflow

TEMPLATE = Path(__file__).with_name("report_template.html")
HUB_DIR = Path(__file__).resolve().parents[2]      # <hub>/workflows/weekly_report/main.py


def load_rows(owner: str, source: str) -> list[dict]:
    """Only this person's rows. Never read other people's data into their report."""
    with open(source, newline="") as f:
        return [r for r in csv.DictReader(f) if r["owner"].strip().lower() == owner]


@step
def build_report(owner: str, source: str, page: str) -> int:
    """Read, render and write in one step that returns only a count. A step's
    return value is kept in the run history, which the hub's managers can see,
    so personal data stays inside the step and the published report."""
    rows = load_rows(owner, source)
    Path(page).write_text(render(owner, rows))
    return len(rows)


def _money(v: float) -> str:
    return f"{v:,.0f}"


def _chart(weeks: list[tuple[str, float]]) -> str:
    """A small inline SVG bar chart: no script, prints as vector."""
    if not weeks:
        return "<p>No data this period.</p>"
    top = max(v for _, v in weeks) or 1
    bar, gap, height = 48, 16, 160
    width = len(weeks) * (bar + gap) + gap
    parts = [f'<svg viewBox="0 0 {width} {height + 40}" role="img" '
             'aria-label="Revenue by week">']
    for i, (label, value) in enumerate(weeks):
        h = round(value / top * height)
        x = gap + i * (bar + gap)
        parts.append(f'<rect x="{x}" y="{height - h}" width="{bar}" height="{h}" '
                     'rx="3" fill="#2f5d8a"/>')
        parts.append(f'<text x="{x + bar / 2}" y="{height + 16}" font-size="11" '
                     f'text-anchor="middle" fill="#5f5e59">{html.escape(label)}</text>')
        parts.append(f'<text x="{x + bar / 2}" y="{height - h - 4}" font-size="11" '
                     f'text-anchor="middle" fill="#1c1c1a">{_money(value)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def render(owner: str, rows: list[dict]) -> str:
    by_week: dict[str, float] = {}
    orders = 0
    for r in rows:
        by_week[r["week"]] = by_week.get(r["week"], 0.0) + float(r["revenue"])
        orders += int(r["orders"])
    revenue = sum(by_week.values())
    cards = "".join(
        f'<div class="card"><div class="label">{html.escape(k)}</div>'
        f'<div class="value">{html.escape(v)}</div></div>'
        for k, v in (("Revenue", _money(revenue)), ("Orders", str(orders)),
                     ("Weeks", str(len(by_week)))))
    body = "".join(
        f"<tr><td>{html.escape(r['week'])}</td><td>{html.escape(r['region'])}</td>"
        f"<td class=\"num\">{int(r['orders'])}</td>"
        f"<td class=\"num\">{_money(float(r['revenue']))}</td></tr>" for r in rows)
    table = ('<table><thead><tr><th>Week</th><th>Region</th><th class="num">Orders</th>'
             f'<th class="num">Revenue</th></tr></thead><tbody>{body}</tbody></table>')
    return Template(TEMPLATE.read_text()).substitute(
        title="Weekly sales report",
        subtitle=html.escape(f"Prepared for {owner}"),
        generated=datetime.now(timezone.utc).strftime("Generated %d %b %Y %H:%M UTC"),
        cards=cards, chart=_chart(sorted(by_week.items())), table=table,
        notes="Figures are synthetic sample data.")


@workflow(schedule="every monday at 8am", timezone="Asia/Kolkata")
def weekly_report():
    owner = hub.user.id
    source = HUB_DIR / hub.setting("orders_csv", "raw_data/orders.csv")
    page = hub.run_dir / "weekly-report.html"
    count = build_report(owner, str(source), str(page))
    report = hub.publish_artifact(page, title="Weekly sales report")
    mail = hub.send_email("Your weekly sales report",
                          "Your weekly report is ready. Sign in to open it.",
                          artifacts=[report])
    return {"report": report["id"], "rows": count, "email": mail["status"]}
