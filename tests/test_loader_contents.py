import json
from pathlib import Path

from hubzoid.loaders import contents


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_counts_what_the_folder_holds(tmp_path):
    hub = tmp_path / "hub"
    _write(hub / "AGENTS.md", "# Agent")
    _write(hub / "knowledge" / "policy.md")
    _write(hub / "knowledge" / "suppliers" / "harbor.md")
    _write(hub / "knowledge" / "export.csv")          # not knowledge the loader reads
    _write(hub / "Skills" / "reorder" / "SKILL.md")   # folder names are case-insensitive
    _write(hub / "Skills" / "returns.md")
    _write(hub / "Skills" / ".draft.md")              # hidden
    _write(hub / "agents" / "researcher.md")
    _write(hub / "tools_local" / "orders.py")
    _write(hub / "tools_local" / "stock.py")
    _write(hub / "tools_local" / "__init__.py")       # not a tool file
    _write(hub / "tools_local" / "_helpers.py")
    _write(hub / "connectors" / ".mcp.json", json.dumps({"mcpServers": {
        "docs": {"url": "https://docs.example.test/mcp"},
        "files": {"command": "npx", "args": ["server"]},
    }}))

    assert contents.counts(hub) == {
        "knowledge": 2, "skills": 2, "subagents": 1, "tool_files": 2, "connectors": 2,
    }


def test_an_empty_or_missing_hub_counts_nothing(tmp_path):
    empty = {"knowledge": 0, "skills": 0, "subagents": 0, "tool_files": 0, "connectors": 0}
    (tmp_path / "hub").mkdir()
    assert contents.counts(tmp_path / "hub") == empty
    assert contents.counts(tmp_path / "missing") == empty


def test_a_broken_connectors_file_counts_no_connectors(tmp_path):
    hub = tmp_path / "hub"
    _write(hub / "connectors" / ".mcp.json", "{not json")
    assert contents.counts(hub)["connectors"] == 0


def test_a_bucket_or_file_linked_from_outside_the_hub_is_not_counted(tmp_path):
    outside = tmp_path / "elsewhere"
    _write(outside / "secret-notes.md")
    _write(outside / "more" / "plans.md")
    hub = tmp_path / "hub"
    (hub / "skills").mkdir(parents=True)
    (hub / "knowledge").symlink_to(outside, target_is_directory=True)
    (hub / "skills" / "linked.md").symlink_to(outside / "secret-notes.md")
    _write(hub / "agents" / "inside.md")
    (hub / "agents" / "nested").symlink_to(outside / "more", target_is_directory=True)

    got = contents.counts(hub)
    assert got["knowledge"] == 0
    assert got["skills"] == 0
    assert got["subagents"] == 1
