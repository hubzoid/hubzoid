"""`@gitzoid` commands in pull request comments. Parsed by code, never by the
model, and kept to the ones that always behave the same:

    @gitzoid review             review the latest commits now (even if paused)
    @gitzoid pause / resume     stop or restart automatic reviews on this PR
    @gitzoid dismiss <reason>   on a GitZoid inline comment: dismiss that finding
    @gitzoid defer              on a GitZoid inline comment: defer it
    @gitzoid help               the commands, and the rule files used here

Only people with write, maintain or admin permission on the repo can command
(GitHub's permission endpoint decides). Each comment runs once, keyed by repo,
comment id and edit time, so a redelivered webhook or an edited comment that
was already handled does nothing. Replies carry a hidden marker, so a retried
step never replies twice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import lifecycle

VERBS = ("review", "pause", "resume", "dismiss", "defer", "help")
WRITERS = ("admin", "maintain", "write")
MENTION = re.compile(r"^\s*@gitzoid\b[:,]?\s*(.*)$", re.I)
FINDING = re.compile(r"<!-- gitzoid:finding:([0-9a-f]{16}) -->")
HELP = """**GitZoid commands** (people with write access):

- `@gitzoid review`: review the latest commits now
- `@gitzoid pause` / `@gitzoid resume`: stop or restart automatic reviews on this pull request
- On one of GitZoid's inline comments: `@gitzoid dismiss <reason>` or `@gitzoid defer`
- `@gitzoid help`: this list"""


@dataclass
class Command:
    verb: str
    arg: str = ""


def parse(body: str) -> Command | None:
    """The first `@gitzoid` line outside quotes and code blocks, or None. An
    unknown verb becomes `help` so a typo gets an answer instead of silence."""
    fenced = False
    for raw in (body or "").splitlines():
        if raw.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or raw.lstrip().startswith(">"):
            continue
        m = MENTION.match(raw)
        if not m:
            continue
        words = m.group(1).strip().split(maxsplit=1)
        verb = words[0].lower().rstrip(".!") if words else "help"
        arg = words[1].strip() if len(words) > 1 else ""
        return Command(verb if verb in VERBS else "help", arg if verb in VERBS else "")
    return None


def key(repo: str, comment: dict) -> str:
    return f"{repo}:{comment['id']}:{comment.get('updated_at') or ''}"


def reply_marker(comment: dict) -> str:
    return f"<!-- gitzoid:reply:{comment['id']}:{comment.get('updated_at') or ''} -->"


def handle(gh, store, settings, repo: str, comment: dict, *, own_login: str, hour: str,
           rule_files: list[str]) -> dict:
    """Run one comment's command. Returns {"outcome", "review_now", "refresh"}:
    review_now asks the caller to review the PR now; refresh asks it to
    update the summary and status because a finding's state changed."""
    result = {"outcome": "ignored", "review_now": False, "refresh": False}
    if not comment["author"] or comment.get("author_type") == "Bot":
        return result
    if FINDING.search(comment.get("body") or "") or "<!-- gitzoid:" in (comment.get("body") or ""):
        return result  # GitZoid's own words quoted back
    cmd = parse(comment.get("body") or "")
    if cmd is None or not settings.commands_for(repo):
        return result
    k = key(repo, comment)
    if store.claim_command(k, repo=repo, number=comment["number"], comment_id=comment["id"],
                           author=comment["author"], command=cmd.verb) == "done":
        return {**result, "outcome": "already handled"}
    # "new" or "pending" (an earlier attempt stopped part-way): every effect
    # below is safe to repeat, so the command simply runs again.
    number = comment["number"]

    def answer(text: str) -> None:
        marker = reply_marker(comment)
        if gh.replied(repo, number, marker):
            return
        if not store.take(f"replies:{comment['author']}", hour, settings.limits.replies_per_person_per_hour):
            return
        gh.reply(repo, number, comment, f"{text}\n{marker}")

    def done(outcome: str, **extra) -> dict:
        store.finish_command(k, outcome)
        return {**result, "outcome": outcome, **extra}

    if gh.permission(repo, comment["author"]) not in WRITERS:
        answer(f"@{comment['author']} only people with write access to this repo can run GitZoid commands.")
        return done("refused: no write access")

    if cmd.verb == "help":
        files = "\n".join(f"- `{f}`" for f in rule_files) or "- none"
        answer(f"{HELP}\n\nRules used for this pull request:\n{files}")
        return done("help")
    if cmd.verb == "review":
        gh.react(repo, comment, "eyes")
        return done("review requested", review_now=True)
    if cmd.verb in ("pause", "resume"):
        paused = cmd.verb == "pause"
        store.save_review(repo, number, paused=int(paused),
                          pause_reason=f"paused by @{comment['author']}" if paused else None,
                          pushes=0, state="paused" if paused else "waiting")
        gh.react(repo, comment, "+1")
        return done(cmd.verb, refresh=True, review_if_new=not paused)

    # dismiss / defer: only in the thread of one of GitZoid's inline comments
    finding = None
    root_id = comment.get("in_reply_to")
    if comment["kind"] == "review" and root_id:
        finding = store.finding_by_comment(repo, root_id)
        if finding is None:
            root = gh.review_comment(repo, root_id)
            m = FINDING.search((root or {}).get("body") or "")
            finding = store.finding(m.group(1)) if m else None
    if finding is not None and int(finding["number"]) != int(number):
        finding = None
    if finding is None:
        answer("Reply to one of GitZoid's inline comments to dismiss or defer that finding.")
        return done("no finding in this thread")
    target = "dismissed" if cmd.verb == "dismiss" else "deferred"
    if finding["state"] == target and finding.get("by_user") == comment["author"]:
        return done(target, refresh=True)  # this command already did it before stopping
    if finding["state"] != "open":
        answer(f"This finding is already {finding['state']}.")
        return done(f"already {finding['state']}")
    pr = store.review(repo, number) or {}
    if (cmd.verb in ("dismiss", "defer") and settings.status_for(repo) == "gate" and lifecycle.blocking(finding)
            and comment["author"] == pr.get("author")):
        answer("The merge gate is on, so someone other than the pull request's author must dismiss or defer a blocking finding.")
        return done(f"refused: author cannot {cmd.verb} a blocking finding")
    state = "dismissed" if cmd.verb == "dismiss" else "deferred"
    reason = cmd.arg or ("no reason given" if state == "dismissed" else "later")
    store.save_finding({**finding, "state": state, "reason": reason[:300], "by_user": comment["author"]},
                       event=state, sha=finding.get("last_sha"), by_user=comment["author"], reason=reason[:300])
    gh.react(repo, comment, "+1")
    return done(state, refresh=True)
