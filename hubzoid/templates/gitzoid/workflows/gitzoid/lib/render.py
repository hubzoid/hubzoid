"""Printable GitZoid reports: the pages its emails link to (the emails are in
mail.py). Contributor/model text is escaped at each rendering boundary."""
from __future__ import annotations

from html import escape

CSS = """
:root{--ink:#0b0b0c;--muted:#5f6773;--line:#e7e6e2;--card:#f7f8f6;--bg:#f2f3f1;--paper:#fff;--accent:#087f46;--bad:#b42318;--ok:#067647;--warn:#b54708}
@media (prefers-color-scheme:dark){:root{--ink:#e6e9ec;--muted:#9aa5b1;--line:#2c333a;--card:#161b21;--bg:#0e1116;--paper:#161b21;--accent:#6ee7a8;--bad:#ff8a80;--ok:#6ee7a8;--warn:#ffc46b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:920px;margin:24px auto;padding:32px;background:var(--paper);border:1px solid var(--line);border-radius:14px}h1{font-size:24px;margin:0 0 4px}h2{font:700 13px/1.5 ui-monospace,Menlo,monospace;letter-spacing:1px;text-transform:uppercase;margin:28px 0 10px;padding-top:14px;border-top:1px solid var(--line)}
.meta{color:var(--muted);font-size:13px;margin:0 0 18px}.lead{font-size:17px;margin:18px 0 24px;padding:16px;background:var(--card);border-left:3px solid #12c46a;border-radius:8px}.brand{display:inline-block;background:#0b0e12;color:#fff;padding:8px 12px;border-radius:8px;font:700 17px ui-monospace,Menlo,monospace;margin-bottom:20px}.brand span{color:#12c46a}summary{cursor:pointer}li{margin:6px 0}.tile b{color:var(--accent)}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:12px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}.tile b{display:block;font-size:20px}.tile span{color:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin:8px 0}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600;white-space:nowrap}td{min-width:90px;overflow-wrap:break-word}
.bad{color:var(--bad)}.ok{color:var(--ok)}.warn{color:var(--warn)}code{font:12.5px ui-monospace,Menlo,monospace}a{color:var(--accent)}
.table-scroll{overflow-x:auto;max-width:100%;border:1px solid var(--line);border-radius:8px}
.table-scroll:focus{outline:2px solid var(--accent)}.table-scroll table{margin:0;min-width:520px}
main{overflow-wrap:anywhere}.security-item{padding:14px;border:1px solid var(--line);border-radius:8px;margin:12px 0}
@media(max-width:600px){main{padding:20px 14px;margin:0;border:0;border-radius:0}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.tile b{font-size:18px}}
ul{padding-left:20px}footer{margin-top:32px;color:var(--muted);font-size:12px}
@media print{body{background:#fff;color:#000}main{padding:0;margin:0;border:0;background:#fff}a{color:#000}.table-scroll{overflow:visible}.table-scroll table{min-width:0}details{break-inside:avoid}}
"""


def esc(x) -> str:
    return escape(str(x if x is not None else ""))


def page(title: str, meta: str, body: str, footer: str = "") -> str:
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{esc(title)}</title>"
            f"<style>{CSS}</style></head><body><main><div class=\"brand\"><span>/</span>gitzoid</div><h1>{esc(title)}</h1><p class=\"meta\">{esc(meta)}</p>"
            f"{body}<footer>{esc(footer or 'Made by GitZoid on Hubzoid. Numbers are computed by code; summaries are written by a model from them.')}</footer>"
            "</main></body></html>")


def tiles(items: list[tuple[str, object, str]]) -> str:
    return "<div class=\"grid\">" + "".join(
        f"<div class=\"tile\"><b>{esc(v if v is not None else '-')}</b><span>{esc(label)}"
        f"{(' · ' + esc(delta)) if delta else ''}</span></div>" for label, v, delta in items) + "</div>"


def table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "<p class=\"meta\">None.</p>"
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c if isinstance(c, Raw) else esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="table-scroll" role="region" aria-label="Report data; scroll horizontally" tabindex="0"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def bullets(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul>" if items else "<p class=\"meta\">None.</p>"


class Raw(str):
    """Already-safe HTML for a table cell."""


def link(url: str, text: str) -> Raw:
    if not str(url).startswith(("https://", "http://")):
        return Raw(esc(text))
    return Raw(f"<a href=\"{esc(url)}\">{esc(text)}</a>") if url else Raw(esc(text))


def hrs(h) -> str:
    if h is None:
        return "-"
    return f"{h:.0f} h" if h < 48 else f"{h / 24:.1f} days"


# ------------------------------------------------------------------- pages

def digest_page(group: str, label: str, d: dict, now_d: dict, prev_d: dict, now_c: dict, merged: list[dict],
                security: dict, card: dict, health: list[dict], coverage: list[str] | None = None) -> str:
    from .metrics import change
    body = [f"<p class=\"lead\">{esc(d.get('headline', ''))}</p>", coverage_notice(coverage or [])]
    body.append("<h2>Primary updates</h2>" + bullets(d.get("for_leadership") or []))
    if d.get("technical"):
        body.append("<h2>Engineering notes</h2>" + bullets(d["technical"]))
    for a in d.get("areas") or []:
        body.append(f"<h2>{esc(a['name'])}</h2>" + bullets(a.get("changes") or []))
    if d.get("watch"):
        body.append("<h2>Needs attention</h2>" + bullets(d["watch"]))
    body.append("<h2>By the numbers</h2>")
    body.append(tiles([("pull requests merged", now_d["merged"], change(now_d["merged"], prev_d["merged"])),
                       ("median cycle time", hrs(now_d["cycle_hours_median"]), ""),
                       ("median first human review", hrs(now_d["first_review_hours_median"]), ""),
                       ("open now", now_d["open"], ""), ("stuck 3+ days", now_d["stuck"], ""),
                       ("lines added / removed", f"{now_c['additions']} / {now_c['deletions']}", "")]))
    body.append("<h2>Merged this week</h2>" + table(["Pull request", "Repo", "Author", "Size"], [
        [link(p.get("url", ""), f"#{p['number']} {p['title']}"), p["repo"], p["author"],
         f"+{p.get('additions', 0)} -{p.get('deletions', 0)}"] for p in merged]))
    if now_d["stuck_list"]:
        body.append("<h2>Stuck</h2>" + table(["Pull request", "Repo", "Days without update"], [
            [link(p["url"], f"#{p['number']} {p['title']}"), p["repo"], p["days"]] for p in now_d["stuck_list"]]))
    body.append("<h2>Security</h2>" + security_summary_html(security))
    body.append("<h2>GitZoid's report card</h2>" + tiles([
        ("reviews", card["reviews"], ""), ("findings", card["findings"], ""), ("fixed", card["fixed"], ""),
        ("dismissed", card["dismissed"], ""), ("fix rate", card["fix_rate"], "")]))
    if health:
        body.append("<h2>Rule health</h2>" + table(["Rule", "Fired", "Fixed", "Dismissed", "Health"], [
            [h["rule"], h["fired"], h["fixed"], h["dismissed"], h["health"]] for h in health]))
    if d.get("poem"):
        body.append("<h2>The week in verse</h2><p>" + esc(d["poem"]).replace("\n", "<br>") + "</p>")
    body.append(evidence_section(merged, coverage or []))
    return page(f"Weekly digest · {group}", label, "".join(body))


def security_summary_html(s: dict) -> str:
    rows = [[r["repo"], r["kind"], r.get("package") or r.get("path") or "", r.get("advisory") or "",
             r["severity"] + (" · exploited now" if r.get("kev") else "")] for r in s.get("open", [])]
    cov = [f"{c['repo']}: {c['check_name']} could not run ({c['last_error']})" for c in s.get("coverage", []) if c.get("last_error")]
    out = f"<p>{len(rows)} open item(s).</p>" + (table(["Repo", "Kind", "Where", "Advisory", "Severity"], rows) if rows else "")
    if cov:
        out += "<p class=\"warn\">Checks that could not run:</p>" + bullets(cov)
    return out


def report_page(kind: str, name: str, label: str, narrative: dict, numbers: dict, sources: list | None = None, coverage: list | None = None) -> str:
    body = [f"<p class=\"lead\">{esc(narrative.get('headline', ''))}</p>", coverage_notice(coverage or [])]
    if narrative.get("highlights"):
        body.append("<h2>Highlights</h2>" + bullets(narrative["highlights"]))
    if narrative.get("watch"):
        body.append("<h2>Needs attention</h2>" + bullets(narrative["watch"]))
    body.append("<h2>By the numbers</h2>")
    if kind == "delivery":
        d = numbers
        body.append(tiles([("merged", d["merged"], d.get("merged_change", "")),
                           ("median cycle time", hrs(d["cycle_hours_median"]), ""),
                           ("75th percentile cycle", hrs(d["cycle_hours_p75"]), ""),
                           ("median first human review", hrs(d["first_review_hours_median"]), ""),
                           ("merged without human review", d["unreviewed_merges"], ""),
                           ("stuck now", d["stuck"], "")]))
        body.append("<h2>Merged per week</h2>" + table(["Week of", "Merged"], [[k, v] for k, v in d["by_week"].items()]))
        body.append("<h2>By repo</h2>" + table(["Repo", "Merged"], [[k, v] for k, v in d["by_repo"].items()]))
        if d["stuck_list"]:
            body.append("<h2>Stuck now</h2>" + table(["Pull request", "Repo", "Days without update"], [
                [link(p["url"], f"#{p['number']} {p['title']}"), p["repo"], p["days"]] for p in d["stuck_list"]]))
    else:
        c = numbers
        body.append(tiles([("merged", c["merged"], c.get("merged_change", "")), ("lines added", c["additions"], ""),
                           ("lines removed", c["deletions"], ""), ("files changed", c["files_changed"], ""),
                           ("PRs changing test files", c["with_tests"], ""), ("reverts", c["reverts"], "")]))
        body.append("<h2>Pull request sizes</h2>" + table(["Size", "Count"], [[k, c["sizes"].get(k, 0)] for k in ("S", "M", "L", "XL")]))
        body.append("<h2>Most changed areas</h2>" + table(["Area", "Files changed"], [list(a) for a in c["top_areas"]]))
        g = c["gitzoid"]
        body.append("<h2>GitZoid findings</h2>" + tiles([("opened", g["opened"], ""), ("fixed", g["fixed"], ""),
                                                         ("dismissed", g["dismissed"], ""), ("deferred", g["deferred"], "")]))
    body.append(evidence_section(sources or [], coverage or []))
    return page(f"{kind.title()} report · {name}", label, "".join(body))


def coverage_notice(coverage: list[str]) -> str:
    if not coverage:
        return ""
    return '<p class="warn"><strong>Coverage is limited.</strong> See Evidence and scope for ' + str(len(coverage)) + ' coverage note(s).</p>'


def evidence_section(sources: list[dict], coverage: list[str]) -> str:
    out = ["<h2>Evidence and scope</h2><p>Merged changes in the selected period; deployment and test execution "
           "are not measured. Open PRs show the current backlog. Test-file counts are not test coverage. "
           "Review counts are unique commit/configuration analyses recorded since this GitZoid upgrade.</p>"]
    if coverage:
        out.append("<details><summary>Coverage limits</summary>" + bullets(coverage) + "</details>")
    for p in sources:
        e = p.get("evidence") or {}
        out.append('<details class="security-item"><summary>' + str(link(p.get("url", ""),
                   f"{p['repo']}#{p['number']} · {p['title']}")) + '</summary>')
        if e:
            out += ["<p><strong>Change:</strong> " + esc(e.get("change")) + "</p>",
                    ("<p><strong>Risks:</strong></p>" + bullets(e["risks"]) if e.get("risks") else ""),
                    "<p><strong>Test evidence:</strong> " + esc(e.get("tests")) + "</p>",
                    ("<p><strong>Follow-up:</strong></p>" + bullets(e["follow_ups"]) if e.get("follow_ups") else "")]
        else:
            out.append("<p>Metadata only; no code-level conclusion.</p>")
        out.append(bullets(p.get("coverage_notes", [])) + "</details>")
    return "".join(out)


def people_page(name: str, label: str, rows: list[dict]) -> str:
    body = ("<p class=\"lead\">Counts only, for the groups you manage. No scores and no rankings.</p>"
            + table(["Person", "Merged", "Median cycle", "Reviews given", "Reviews waiting on them", "Open now"],
                    [[r["person"], r["merged"], hrs(r["cycle_hours_median"]), r["reviews_given"], r["waiting_on"],
                      r["open"]] for r in rows]))
    return page(f"People report · {name}", label, body, "Private to you. Made by GitZoid on Hubzoid.")


def security_page(group: str, stamp: str, changes: dict, open_rows: list[dict], coverage: list[dict]) -> str:
    def rows(items):
        return [[i["repo"], i.get("package") or i.get("path") or "", i.get("version") or (f"line {i['line']}" if i.get("line") else ""),
                 link(i.get("url", ""), i.get("advisory") or ""), i["severity"] + (" · exploited now" if i.get("kev") else ""),
                 i.get("impact") or i.get("title") or "", i.get("fix") or ""] for i in items]
    head = ["Repo", "Package or file", "Version", "Advisory", "Severity", "Impact", "Fix"]
    body = [tiles([("new", len(changes.get("new", [])), ""),
                   ("escalated", len(changes.get("escalated", [])), ""),
                   ("resolved", len(changes.get("resolved", [])), ""),
                   ("open", len(open_rows), "")])]
    if changes.get("new") or changes.get("escalated"):
        body.append("<h2>New</h2>" + table(head, rows(changes.get("new", []))))
        if changes.get("escalated"):
            body.append("<h2>Escalated</h2>" + table(head, rows(changes["escalated"])))
    if changes.get("resolved"):
        body.append("<h2>Resolved since the last check</h2>" + table(["Repo", "Package or file", "Advisory"], [
            [r["repo"], r.get("package") or r.get("path") or "", r.get("advisory") or ""] for r in changes["resolved"]]))
    problems = [c for c in coverage if c.get("last_error")]
    if problems:
        body.append("<h2 class=\"warn\">Checks that could not run</h2>" + table(["Repo", "Check", "Problem", "Since"], [
            [c["repo"], c["check_name"], c["last_error"], c["error_at"]] for c in problems]))
    body.append("<h2>Everything open</h2>")
    for item in open_rows:
        where = item.get("package") or item.get("path") or ""
        body.append('<article class="security-item"><h3>' + esc(item["severity"].title()) + " · " +
                    esc(where) + '</h3><p class="meta">' + esc(item["repo"]) + " · " +
                    str(link(item.get("url", ""), item.get("advisory") or "")) + " · " +
                    esc(item.get("version") or "") + '</p><p><strong>Impact:</strong> ' +
                    esc(item.get("impact") or item.get("title") or "Needs assessment") +
                    '</p><p><strong>Next action:</strong> ' + esc(item.get("fix") or "Review the cited code and confirm remediation") +
                    '</p></article>')
    if not open_rows:
        body.append("<p>No open findings in the completed checks. Review coverage above.</p>")
    return page(f"Security · {group}", f"Checked {stamp}", "".join(body))
