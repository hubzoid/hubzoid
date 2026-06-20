"""Auto-injected system-prompt addendum.

Hubzoid composes a small, deterministic block of text that is appended to the
user's `AGENTS.md` body before it is sent to the model as the system prompt.

Sections:
  * Environment      — cwd, hub name, model, date, backend
  * Knowledge        — `- name: description` list, omitted if hub has no
                       knowledge files
  * Skills           — `- name: description` list, omitted if hub has no
                       skills (after agents/ promotion in factory.py)
  * Tool guidance    — generic behavioural rules: prefer lookups, parallel
                       calls, surface tool-result links to the user, learn
                       from errors. NOT domain-specific.

Why this exists. Without an addendum the model only sees the user's AGENTS.md.
That works in Claude Code because the harness adds its own bias toward tool
use; through OpenRouter / Claude SDK without that harness the model frequently
answers from memory instead of calling `read_knowledge` / `load_skill`. The
addendum is the framework's contribution to making tool use the default — the
user does not have to write it.

Opt-out. The main agent's AGENTS.md frontmatter may set `auto_addendum: false`
to disable the addendum entirely. This is for power users who want full
control of the system prompt.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .factory import HubContext


def build(ctx: "HubContext", *, backend: str) -> str:
    """Return the addendum text. Always ends with a single trailing newline.

    `backend` is the runtime label ("openai-agents" or "claude-local") shown
    in the Environment block so operators can sanity-check which backend the
    model is actually running under.
    """
    parts: list[str] = []

    parts.append("---")
    parts.append("")
    parts.append("# Hubzoid runtime context (auto-injected)")
    parts.append("")

    parts.append(_env_section(ctx, backend=backend))

    knowledge_section = _knowledge_section(ctx)
    if knowledge_section:
        parts.append(knowledge_section)

    skills_section = _skills_section(ctx)
    if skills_section:
        parts.append(skills_section)

    raw_data_section = _raw_data_section(ctx)
    if raw_data_section:
        parts.append(raw_data_section)

    restricted_section = _restricted_section(ctx)
    if restricted_section:
        parts.append(restricted_section)

    parts.append(_uploads_section())
    parts.append(_tools_section())

    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def _env_section(ctx: "HubContext", *, backend: str) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    model = getattr(ctx.settings, "model", None) or "(unset)"
    lines = [
        "## Environment",
        "",
        f"- Hub directory: {ctx.hub_dir}",
        f"- Hub name: {ctx.hub_dir.name}",
        f"- Backend: {backend}",
        f"- Model: {model}",
        f"- Today's date (UTC): {today}",
        "",
    ]
    return "\n".join(lines)


def _knowledge_section(ctx: "HubContext") -> str:
    if not ctx.knowledge:
        return ""
    lines = [
        "## Knowledge available",
        "",
        "Call `read_knowledge(name)` to load any of these in full. Prefer to",
        "look something up rather than answer from memory when a relevant",
        "document is listed below:",
        "",
    ]
    for doc in ctx.knowledge:
        lines.append(f"- {doc.name}: {doc.description}")
    lines.append("")
    return "\n".join(lines)


def _skills_section(ctx: "HubContext") -> str:
    if not ctx.skills:
        return ""
    lines = [
        "## Skills available",
        "",
        "Call `load_skill(name)` to load a playbook when its description",
        "matches the user's request. Follow the skill body's instructions:",
        "",
    ]
    for skill in ctx.skills:
        lines.append(f"- {skill.spec.name}: {skill.spec.description}")
    lines.append("")
    return "\n".join(lines)


def _raw_data_section(ctx: "HubContext") -> str:
    """Emitted only when <hub>/raw_data/ exists.

    Tells the agent how to navigate a possibly-large unstructured corpus
    without drowning: orient first, scope every grep, read on demand.
    """
    from ._fs import resolve_bucket
    if resolve_bucket(ctx.hub_dir, "raw_data") is None:
        return ""
    return "\n".join([
        "## Searching raw_data/",
        "",
        "This hub has a `raw_data/` folder with unstructured source material.",
        "When the user asks about anything that could plausibly be in there:",
        "",
        "1. `list_files('raw_data/**')` to see every file (the `**` is",
        "   recursive — `raw_data/*` only shows the top level).",
        "2. `grep_data(pattern, path='raw_data/<scoped-folder>')` for any",
        "   value, symbol, or phrase. Scope `path` to one subfolder when",
        "   you can; unscoped greps across multiple repos waste tokens.",
        "3. `read_file(path, offset, limit)` for the specific file the",
        "   grep pointed at.",
        "",
        "Always grep before answering \"I cannot find X\" — list_files only",
        "tells you what files exist, not what is inside them.",
        "",
        "Stop once you have the answer. Two or three targeted greps and",
        "reads should be enough for most questions. If you reach a fourth,",
        "narrow the pattern or reconsider the question — do not re-read the",
        "same file or grep variants of the same term in a loop.",
        "",
        "If a tool result is truncated, the footer says exactly how to read",
        "the rest. Refine and continue.",
        "",
    ])


def _restricted_section(ctx: "HubContext") -> str:
    """Emitted only when <hub>/restricted/ exists.

    A reminder, not a control. The real enforcement is in code: the file tools
    refuse to read under restricted/, and access-controlled tools are gated by
    the runtime regardless of what the model is told here. This just removes the
    reflex to poke at the folder or to claim a tool that was filtered out.
    """
    from ._fs import resolve_bucket
    if resolve_bucket(ctx.hub_dir, "restricted") is None:
        return ""
    return (
        "## Restricted folder and tools\n"
        "\n"
        "This hub has a `restricted/` folder holding access-controlled tools\n"
        "and secrets.\n"
        "\n"
        "- Never try to read anything under `restricted/` (for example with\n"
        "  `read_file` or `grep_data`). Those calls are refused by the runtime,\n"
        "  and the credentials there are not yours to read.\n"
        "- Some tools are available only to certain users. Your tool list is\n"
        "  already filtered to what the current user may use. If a tool is not\n"
        "  in your list, treat it as nonexistent; do not claim it or hint at it.\n"
        "- If a tool returns an access-denied message, relay that plainly to the\n"
        "  user. Do not attempt to work around it.\n"
    )


def _uploads_section() -> str:
    """Guidance for files the user attached (Slack / Open WebUI / API uploads).

    Why this section exists. Without it, when `read_upload_full` returns a
    truncated preview of a large file, the agent panics, spawns a subagent,
    and tries to read the file via Bash / Read on hallucinated paths under
    `~/.claude/projects/`. None of those tools can reach the uploads dir —
    every call fails with permission-denied and the agent burns 15+ tool
    calls trying to escape. Telling the model the rule up front removes
    the escape reflex.
    """
    return (
        "## Reading user-uploaded files\n"
        "\n"
        "Files the user attached in this chat (Slack uploads, image drops,\n"
        "API attachments) live in a private per-chat directory that ONLY the\n"
        "`read_upload` and `read_upload_full` tools can reach.\n"
        "\n"
        "- The `Bash`, `Read`, and subagent (Agent / Task) tools CANNOT see\n"
        "  these files. Never try to read an upload via shell commands or by\n"
        "  spawning a subagent — the path is internal and any guess will be\n"
        "  wrong.\n"
        "- `read_upload(filename)` returns a bounded preview: first 200 lines\n"
        "  for text, structural summary + head for JSON, header + sample\n"
        "  rows for CSV, first pages for PDF, metadata only for images.\n"
        "  Bounds are intentional — they fit the prompt budget.\n"
        "- If a preview's footer says more is available, paginate with\n"
        "  `read_upload(filename, offset=N, limit=M)` — never escape to\n"
        "  another tool.\n"
        "- Use `read_upload_full(filename)` only when you specifically need\n"
        "  the entire text content. It is capped at 500,000 characters;\n"
        "  files larger than that come back with an explicit byte-count\n"
        "  footer telling you what is included.\n"
    )


def _tools_section() -> str:
    return (
        "## How to use your tools\n"
        "\n"
        "You have tools available to look things up, take actions, and produce\n"
        "output for the user.\n"
        "\n"
        "- Prefer to look things up rather than answer from memory whenever a\n"
        "  relevant source is listed above.\n"
        "- When you intend to call multiple tools and there are no dependencies\n"
        "  between them, call them in parallel.\n"
        "- When a tool saves a file for the user, its download link is added to\n"
        "  your reply automatically — just say what you made.\n"
        "- If a tool returns an error, read it and adjust; do not retry the same\n"
        "  call unchanged.\n"
    )


# ---------------------------------------------------------------------------
# Opt-out detection
# ---------------------------------------------------------------------------
def is_enabled(hub_dir: Path) -> bool:
    """Return False if the main agent's frontmatter sets `auto_addendum: false`.

    Defaults to True. We re-parse the file here (rather than threading the
    flag through every loader) because the addendum is a runtime concern.
    """
    from . import frontmatter

    path = hub_dir / "AGENTS.md"
    if not path.is_file():
        return True
    try:
        fm, _ = frontmatter.read(path)
    except Exception:
        return True
    value = fm.get("auto_addendum")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "no", "0", "off")
