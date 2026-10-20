"""GitZoid's emails, in GitZoid's own layout: the /gitzoid chip, a title that
matches the subject, a stats bar, a summary box and labelled sections, then a
link to the full page. Inline styles only, because email clients drop
stylesheets. Model and contributor text is escaped here; Hubzoid sends the
result with body_format="html" and removes anything unsafe again.

An email stays short enough to read in the inbox. When the content is long,
lower sections lose items first, and the page always has everything."""
from __future__ import annotations

import re
from html import escape, unescape

from .render import hrs

INK, BODY, MUTED, FAINT, LINE, CARD = "#0B0B0C", "#2B2F36", "#5F6773", "#8A8F98", "#E7E6E2", "#F7F8F6"
GREEN, DARK_GREEN, RED, AMBER, SLATE = "#12C46A", "#0B7E43", "#C0392B", "#9A6B12", "#94A3B8"
MONO = "'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Helvetica,Arial,sans-serif"
TEXT_BUDGET = 4200   # visible characters; a Hubzoid html list email allows 5,000
SEVERITY = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low", "unknown": "Unknown"}
RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 1}

_PR = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b")
_CODE = re.compile(r"`([^`\n]{1,200})`")
_PR_NOTE = re.compile(r"^([^\s#]+#\d+): ")


# ------------------------------------------------------------------ text

def text(value, repos=(), limit: int = 600) -> str:
    """Model or contributor text as safe HTML: escaped, `code` shown as code,
    and owner/repo#123 linked when the repo is one of GitZoid's on GitHub."""
    value = " ".join(str(value or "").split())
    if len(value) > limit:
        value = value[:limit].rsplit(" ", 1)[0].rstrip(",;:") + " …"
    out = escape(value, quote=False)
    out = _CODE.sub(lambda m: f'<code style="font-family:{MONO};font-size:12.5px;background:{CARD};'
                              f'border:1px solid {LINE};border-radius:4px;padding:0 4px">{m.group(1)}</code>', out)
    if repos:
        out = _PR.sub(lambda m: (f'<a href="https://github.com/{m.group(1)}/pull/{m.group(2)}" '
                                 f'style="color:{DARK_GREEN};text-decoration:none">{m.group(0)}</a>')
                      if m.group(1) in repos else m.group(0), out)
    return out


def sentence(*parts) -> str:
    """Sentences joined with one full stop each, never two."""
    out = []
    for part in parts:
        part = " ".join(str(part or "").split())
        if part:
            out.append(part if part[-1] in ".!?…" else part + ".")
    return " ".join(out)


def visible(html: str) -> int:
    """About what Hubzoid counts against the limit: the text, plus each link's address."""
    hrefs = sum(len(h) + 3 for h in re.findall(r'href="([^"]*)"', html))
    return len(" ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())) + hrefs


def fit(render, levels) -> str:
    """The fullest version within the email text budget."""
    html = ""
    for level in levels:
        html = render(*level)
        if visible(html) <= TEXT_BUDGET:
            break
    return html


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ---------------------------------------------------------------- blocks

def h2(title: str, first: bool = False) -> str:
    return (f'<h2 style="margin:{6 if first else 30}px 0 0;padding:0 0 12px;border-bottom:1px solid {LINE};'
            f'font-family:{MONO};font-size:12px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;'
            f'color:{INK}">{escape(title)}</h2>')


def para(html: str, color: str = BODY, size: int = 14, weight: int = 400) -> str:
    return f'<p style="margin:10px 0;line-height:1.55;font-size:{size}px;font-weight:{weight};color:{color}">{html}</p>'


def bullets(items, repos=(), limit: int | None = None) -> str:
    items = [i for i in (items or []) if str(i or "").strip()][:limit]
    if not items:
        return para("None this week.", MUTED, 12)
    return (f'<ul style="margin:8px 0 12px 18px;padding:0">'
            + "".join(f'<li style="margin:5px 0;line-height:1.55;color:{BODY}">{text(i, repos)}</li>' for i in items)
            + "</ul>")


def summary_box(value, repos=()) -> str:
    return (f'<div style="padding:14px 16px;border-radius:10px;background:{CARD};border:1px solid {LINE};'
            f'border-left:3px solid {GREEN};margin:14px 0 20px"><p style="margin:0;line-height:1.55;color:{BODY}">'
            f'{text(value, repos, 900)}</p></div>')


def stats_bar(items) -> str:
    cells = "".join(
        f'<td style="width:{100 // len(items)}%;text-align:center;padding:18px 6px;vertical-align:top">'
        f'<div style="font-size:26px;font-weight:800;line-height:1.2;color:{GREEN}">{escape(str(value))}</div>'
        f'<div style="font-family:{MONO};font-size:10px;letter-spacing:1px;text-transform:uppercase;color:{MUTED}">'
        f'{escape(label)}</div></td>' for value, label in items)
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="width:100%;'
            f'border-collapse:collapse;margin-top:24px;border-top:1px solid {LINE};border-bottom:1px solid {LINE}">'
            f"<tr>{cells}</tr></table>")


def card(meta: str, title: str, lines: list[str], accent: str = GREEN) -> str:
    """meta and lines are HTML already; title is plain text."""
    return (f'<div style="padding:13px 15px;border-radius:10px;border:1px solid {LINE};border-left:3px solid {accent};'
            f'margin:10px 0">' + (f'<div style="font-size:12px;color:{MUTED}">{meta}</div>' if meta else "")
            + (f'<div style="font-weight:700;font-size:14px;color:{INK};margin-top:{5 if meta else 0}px">'
               f"{escape(title)}</div>" if title else "")
            + "".join(lines) + "</div>")


def line(html: str, color: str = "#374151", size: int = 13) -> str:
    return f'<div style="font-size:{size}px;line-height:1.5;color:{color};margin-top:5px">{html}</div>'


def label(title: str) -> str:
    return (f'<div style="font-family:{MONO};font-size:10px;font-weight:700;letter-spacing:1px;'
            f'text-transform:uppercase;color:{MUTED};margin:18px 0 8px">{escape(title)}</div>')


def chip() -> str:
    return (f'<span style="display:inline-block;background:#0B0E12;border-radius:8px;padding:9px 13px">'
            f'<span style="font-family:{MONO};font-size:17px;font-weight:700;letter-spacing:-0.5px;color:#ffffff">'
            f'<span style="color:{GREEN}">/</span>gitzoid</span></span>')


def shell(title: str, subtitle: str, bar: str, body: str, link: str, link_label: str, link_note: str,
          notes: list[str]) -> str:
    """The card the email is built in. Hubzoid puts it on the grey email
    background and adds the list footer below it."""
    cta = ""
    if str(link).startswith(("https://", "http://")):
        cta = (f'<p style="margin:26px 0 0"><a href="{escape(link)}" style="display:inline-block;background:#0B0E12;'
               f'color:#ffffff;text-decoration:none;font-weight:700;font-size:14px;padding:11px 18px;border-radius:8px">'
               f"{escape(link_label)} &rarr;</a></p>"
               f'<p style="margin:8px 0 0;font-size:12px;line-height:1.5;color:{MUTED}">{escape(link_note)}</p>')
    head = (f'<div style="padding:30px 34px 0">{chip()}'
            f'<h1 style="margin:20px 0 0;font-size:26px;font-weight:800;letter-spacing:-0.5px;line-height:1.15;'
            f'color:{INK}">{escape(title)}</h1>'
            + (f'<div style="color:{MUTED};font-size:12px;margin-top:8px">{escape(subtitle)}</div>' if subtitle else "")
            + "</div>")
    foot = (f'<div style="text-align:center;padding:26px 34px 30px;border-top:1px solid {LINE};margin-top:26px">'
            f'<span style="font-family:{MONO};font-size:12px;font-weight:700;color:{INK}">'
            f'<span style="color:{GREEN}">/</span>gitzoid</span>'
            + "".join(f'<p style="margin:8px 0 0;font-size:11px;line-height:1.5;color:{FAINT}">{escape(n)}</p>'
                      for n in notes) + "</div>")
    spacer = bar or '<div style="height:14px"></div>'
    return (f'<div style="max-width:640px;margin:0 auto;background:#ffffff;border:1px solid {LINE};border-radius:14px;'
            f'overflow:hidden;font-family:{SANS};font-size:14px;line-height:1.55;color:{INK}">'
            f'{head}{spacer}<div style="padding:6px 34px 0">{body}{cta}</div>{foot}</div>')


def coverage_note(coverage, what: str) -> str:
    """What the summary could not read, in plain words; the page lists each one."""
    notes = [str(c) for c in (coverage or []) if str(c).strip()]
    if not notes:
        return ""
    prs = {m.group(1) for c in notes if (m := _PR_NOTE.match(c))}
    other = [c for c in notes if not _PR_NOTE.match(c)]
    out = ""
    if prs:
        out += para(escape(f"The summary read only part of the code in {plural(len(prs), 'pull request')} "
                           f"(very large changes or files). The full {what} says which."), MUTED, 12)
    for c in other[:2]:   # totals that may be incomplete are a warning; the rest is context
        c = re.sub(r"\s*\((?:reports|review|audit|security|limits|digest)\.[a-z_]+\)", "", c)
        out += para(escape(sentence(c)), AMBER if "incomplete" in c else MUTED, 12)
    return out


def _title_case(name: str) -> str:
    name = (name or "").strip()
    return name[:1].upper() + name[1:]


# ---------------------------------------------------------------- digest

def digest_title(group: str, implicit: bool = False) -> str:
    """Shared by the subject and the heading, so the two always match."""
    if implicit or not (group or "").strip():
        return "GitZoid Digest"
    return f"GitZoid {_title_case(group)} Digest"


def security_rollup(security: dict | None, limit: int, repos=()) -> str:
    """The week's security state. Shown only once a check has run: before
    that, an empty list is not an all-clear."""
    if not security:
        return ""
    rows = sorted(security.get("open") or [], key=lambda r: (not r.get("kev"), RANK.get(r.get("severity"), 4)))
    cov = security.get("coverage") or []
    problems = [c for c in cov if c.get("last_error")]
    if not rows and not problems and not any(c.get("last_ok") for c in cov):
        return ""
    out = [h2("Security")]
    if rows:
        out.append(para(escape(f"{plural(len(rows), 'issue')} open."), MUTED, 12.5))
        for r in rows[:limit]:
            name = (f"{r.get('package') or ''} {r.get('version') or ''}".strip() if r.get("kind") == "dependency"
                    else (r.get("title") or r.get("advisory") or "Finding"))
            kev = f' <span style="color:{RED};font-weight:600">&middot; actively exploited</span>' if r.get("kev") else ""
            out.append(f'<div style="font-size:13px;line-height:1.5;color:{BODY};padding:7px 0;border-top:1px solid #EEEFEB">'
                       f'<span style="font-weight:700;color:{INK}">{text(name, (), 120)}</span> '
                       f'<span style="color:{MUTED}">&middot; {escape(r.get("repo") or "")}, '
                       f'{escape(SEVERITY.get(r.get("severity"), ""))}</span>{kev}</div>')
        if len(rows) > limit:
            out.append(para(escape(f"And {len(rows) - limit} more in the full digest."), MUTED, 12))
    elif problems:
        out.append(para("No open security issues in the checks that ran.", DARK_GREEN, 14, 500))
    else:
        out.append(para("No open security issues. GitZoid watched your dependencies and code and found nothing "
                        "that needs action.", DARK_GREEN, 14, 500))
    for c in problems[:3]:
        out.append(para(escape(sentence(f"Could not check {c['repo']} ({c['check_name']}): {c['last_error']}")), AMBER, 12))
    for repo in (security.get("audit_in_progress") or [])[:3]:
        out.append(para(escape(f"The deep code audit of {repo} is still in progress; findings so far are included."),
                        MUTED, 12))
    return "".join(out)


def digest_email(group: str, label: str, week_of: str, d: dict, numbers: dict, security: dict | None = None,
                 coverage=(), link: str = "", repos=(), implicit: bool = False) -> tuple[str, str]:
    title = digest_title(group, implicit)
    bar = stats_bar([(numbers.get("merged", 0), "PRs merged"), (numbers.get("contributors", 0), "Contributors"),
                     (numbers.get("open", 0), "Open PRs"), (numbers.get("stuck", 0), "Stuck 3+ days")])
    areas, tech, watch = d.get("areas") or [], d.get("technical") or [], d.get("watch") or []

    def render(n_lead, n_area, n_change, n_tech, n_watch, n_sec, poem):
        body = [h2("Summary", first=True), summary_box(d.get("headline"), repos),
                h2("Primary updates"), bullets(d.get("for_leadership"), repos, n_lead)]
        if areas[:n_area]:
            body.append(h2("Deep dive"))
            body += [card("", a.get("name") or "", [bullets(a.get("changes"), repos, n_change)])
                     for a in areas[:n_area]]
        if tech[:n_tech]:
            body += [h2("Engineering notes"), bullets(tech, repos, n_tech)]
        if watch[:n_watch]:
            body += [h2("Needs attention"), bullets(watch, repos, n_watch)]
        body.append(security_rollup(security, n_sec, repos))
        if poem and d.get("poem"):
            body.append(f'<div style="background:{CARD};padding:16px;border-radius:10px;border-left:3px solid {GREEN};'
                        f'margin:20px 0"><div style="font-weight:700;margin-bottom:6px">A small poem for this week</div>'
                        + "".join(f'<p style="margin:2px 0;color:#374151;font-style:italic">{escape(x)}</p>'
                                  for x in str(d["poem"]).splitlines() if x.strip()) + "</div>")
        body.append(coverage_note(coverage, "digest"))
        return shell(title, f"Report period: {label}", bar, "".join(body), link, "Open the full digest",
                     "Every merged pull request, numbers, security and evidence. Sign in to open it.",
                     ["Numbers are computed by code; the summary is written by a model from them.",
                      "Merged does not mean deployed."])

    levels = [(5, 4, 4, 6, 3, 6, True), (4, 3, 3, 4, 3, 4, True), (3, 2, 3, 3, 2, 3, False),
              (3, 2, 2, 2, 2, 3, False), (3, 0, 0, 0, 2, 2, False), (2, 0, 0, 0, 1, 1, False)]
    return f"{title}: Week of {week_of}", fit(render, levels)


# -------------------------------------------------------------- security

def _meta(*parts) -> str:
    return " &nbsp;|&nbsp; ".join(p for p in parts if p)


def _severity(sev: str | None) -> str:
    return f'<span style="color:{FAINT}">Severity: {escape(SEVERITY.get(sev or "", sev or ""))}</span>' if sev else ""


def _kev(on) -> str:
    return f'<span style="color:{RED};font-weight:600;white-space:nowrap">actively exploited</span>' if on else ""


def _escalated(on) -> str:
    return f'<span style="color:{AMBER};font-weight:600">More severe than before</span>' if on else ""


def _code_card(i: dict, escalated: bool, repos=()) -> str:
    where = i.get("path") or ""
    if where and i.get("line"):
        where += f":{i['line']}"
    lines = [line(text(i.get("impact") or "", repos, 400))] if i.get("impact") else []
    if i.get("fix"):
        lines.append(line("Fix: " + text(i["fix"], repos, 300), DARK_GREEN))
    if where:
        lines.append(line("Location: " + escape(where), "#9AA3AF", 11))
    meta = _meta(f'<b style="color:{INK}">{escape(i.get("repo") or "")}</b>', _severity(i.get("severity")),
                 _kev(i.get("kev")), _escalated(escalated))
    title = " ".join(str(i.get("title") or i.get("advisory") or "Finding").split())
    return card(meta, title[:160] + ("…" if len(title) > 160 else ""), lines, RED)


def _dep_card(group: list[dict], escalated: set, repos=()) -> str:
    """All advisories for one package version in one card: the worst severity,
    a count, the fix and every reference."""
    group = sorted(group, key=lambda g: (not g.get("kev"), RANK.get(g.get("severity"), 4)))
    worst = group[0]
    pkg = f"{worst.get('package') or ''} {worst.get('version') or ''}".strip()
    lines = [line(escape(plural(len(group), "known vulnerability", "known vulnerabilities")), MUTED, 12)] if len(group) > 1 else []
    impact = worst.get("impact") or worst.get("title")
    if impact:
        lines.append(line(text(impact, repos, 400)))
    fix = next((g.get("fix") for g in group if g.get("fix")), "")
    lines.append(line("Fix: " + text(fix, repos, 300), DARK_GREEN) if fix
                 else line("No fixed version is published yet.", AMBER))
    refs = []
    for g in group:
        ref = escape(g.get("advisory") or "")
        url = str(g.get("url") or "")
        if ref:
            refs.append(f'<a href="{escape(url)}" style="color:#9AA3AF">{ref}</a>' if url.startswith("https://") else ref)
    if refs:
        lines.append(line(("References: " if len(refs) > 1 else "Reference: ") + ", ".join(dict.fromkeys(refs)), "#9AA3AF", 11))
    if worst.get("path"):
        lines.append(line("Found in: " + escape(worst["path"]), "#9AA3AF", 11))
    meta = _meta(f'<b style="color:{INK}">{escape(worst.get("repo") or "")}</b>', _severity(worst.get("severity")),
                 _kev(any(g.get("kev") for g in group)),
                 _escalated(any(g.get("key") in escalated for g in group)))
    return card(meta, pkg, lines, SLATE)


def security_email(changes: dict, problems: list[dict], repos_checked: int, link: str = "", audit: bool = False,
                   repos=()) -> tuple[str, str]:
    """One email for what is new or worse since the last check. Code and access
    issues come first; routine package upgrades follow in a calmer colour."""
    items = (changes.get("new") or []) + (changes.get("escalated") or [])
    escalated = {i.get("key") for i in changes.get("escalated") or []}
    code = sorted([i for i in items if i.get("kind") != "dependency"],
                  key=lambda i: (not i.get("kev"), RANK.get(i.get("severity"), 4)))
    groups: dict[tuple, list] = {}
    for i in items:
        if i.get("kind") == "dependency":
            groups.setdefault((i.get("repo"), i.get("package"), i.get("version")), []).append(i)
    deps = sorted(groups.values(), key=lambda g: (not any(x.get("kev") for x in g),
                                                   min(RANK.get(x.get("severity"), 4) for x in g)))
    n = len(code) + len(deps)
    noun = "Security audit" if audit else "Security"
    checks = plural(len(problems), "check")
    subject = (f"GitZoid {noun}: {plural(n, 'issue')} found" + (f" · {checks} could not run" if problems else "")
               if n else f"GitZoid {noun}: {checks} could not run")
    intro = (f"{plural(n, 'issue')} found across {plural(repos_checked, 'repository', 'repositories')}."
             if n else "Nothing new was found, but some checks could not run.")
    if audit:
        intro += " Read from the code itself: missing permission checks, leaked secrets and backdoors."
    elif deps:
        intro += " Advisories GitZoid judged unreachable from your code are left out."
    resolved = len(changes.get("resolved") or [])

    def render(n_code, n_deps):
        body = [para(escape(intro), MUTED, 13)]
        if problems:
            body.append(label("Checks that could not run"))
            body += [card(_meta(f'<b style="color:{INK}">{escape(c.get("repo") or "")}</b>', escape(c.get("check_name") or "")),
                          "", [line(text(c.get("last_error"), (), 300)),
                               line("GitZoid tries again on its next run.", MUTED, 12)], AMBER) for c in problems[:3]]
        if code:
            body.append(label("Code and access issues"))
            body += [_code_card(i, i.get("key") in escalated, repos) for i in code[:n_code]]
            if len(code) > n_code:
                body.append(para(escape(f"And {len(code) - n_code} more in the full report."), MUTED, 12))
        if deps:
            body.append(label("Vulnerable dependencies"))
            body.append(para("Known vulnerabilities in third-party packages you depend on. The fix is to upgrade "
                             "the package.", MUTED, 12.5))
            body += [_dep_card(g, escalated, repos) for g in deps[:n_deps]]
            if len(deps) > n_deps:
                body.append(para(escape(f"And {len(deps) - n_deps} more in the full report."), MUTED, 12))
        if resolved:
            body.append(para(escape(f"{resolved} resolved since the last check."), DARK_GREEN, 13))
        return shell("Security audit" if audit else "Security review", "", "", "".join(body), link,
                     "Open the security report", "Every open finding, with impact, fix and coverage. Sign in to open it.",
                     ["GitZoid stays silent unless it finds something new, and does not alert you twice about the "
                      "same issue."])

    return subject, fit(render, [(8, 10), (5, 6), (3, 4), (2, 2), (1, 1)])


# --------------------------------------------------------------- reports

def report_title(kind: str, name: str = "") -> str:
    name = (name or "").strip()
    if kind == "people" or not name or "/" in name or "," in name:
        return f"GitZoid {kind.title()} Report"
    return f"GitZoid {_title_case(name)} {kind.title()} Report"


def report_stats(kind: str, n: dict) -> list[tuple]:
    if kind == "delivery":
        return [(n.get("merged", 0), "PRs merged"), (hrs(n.get("cycle_hours_median")), "Median cycle"),
                (hrs(n.get("first_review_hours_median")), "First review"), (n.get("unreviewed_merges", 0), "Unreviewed")]
    if kind == "people":
        return [(n.get("people", 0), "People"), (n.get("merged", 0), "PRs merged"),
                (n.get("reviews_given", 0), "Reviews given"), (n.get("open", 0), "Open now")]
    return [(n.get("merged", 0), "PRs merged"), (n.get("additions", 0), "Lines added"),
            (n.get("deletions", 0), "Lines removed"), (n.get("files_changed", 0), "Files changed")]


def report_email(kind: str, name: str, label: str, narrative: dict, numbers: dict | None = None, coverage=(),
                 link: str = "", repos=()) -> tuple[str, str]:
    title = report_title(kind, name)
    bar = stats_bar(report_stats(kind, numbers or {})) if numbers else ""
    notes = (["Counts only, for the groups you manage. No scores and no rankings."] if kind == "people" else
             ["Numbers are computed by code; the summary is written by a model from them.",
              "Merged does not mean deployed."])

    def render(n_high, n_watch):
        body = [h2("Summary", first=True), summary_box(narrative.get("headline"), repos)]
        if narrative.get("highlights"):
            body += [h2("Highlights"), bullets(narrative["highlights"], repos, n_high)]
        if narrative.get("watch") and n_watch:
            body += [h2("Needs attention"), bullets(narrative["watch"], repos, n_watch)]
        body.append(coverage_note(coverage, "report"))
        return shell(title, f"Report period: {label}", bar, "".join(body), link, "Open the full report",
                     "Tables, every merged pull request and the evidence behind the summary. Sign in to open it.", notes)

    return f"{title}: {label}", fit(render, [(5, 3), (4, 3), (3, 2), (2, 1), (1, 0)])
