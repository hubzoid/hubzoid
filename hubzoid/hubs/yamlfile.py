"""Strict YAML for hub files: one document, no duplicate keys, no aliases or
custom tags, at most 1 MiB. Errors name the file and line."""
from __future__ import annotations

from pathlib import Path

import yaml

MAX_BYTES = 1024 * 1024


class HubFileError(ValueError):
    """A hub file that cannot be used. The message names the file and line."""


class _StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):  # noqa: D401 - PyYAML hook
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise HubFileError(f"line {event.start_mark.line + 1}: aliases are not allowed")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise HubFileError(f"line {key_node.start_mark.line + 1}: duplicate key {key!r}")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load(path: Path) -> dict:
    """The file as a mapping. Raises HubFileError with the path and line."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HubFileError(f"{path}: cannot be read ({type(exc).__name__})") from None
    if len(raw) > MAX_BYTES:
        raise HubFileError(f"{path}: larger than 1 MiB")
    try:
        data = yaml.load(raw.decode("utf-8"), Loader=_StrictLoader)  # noqa: S506 - strict SafeLoader
    except HubFileError as exc:
        raise HubFileError(f"{path}: {exc}") from None
    except UnicodeDecodeError:
        raise HubFileError(f"{path}: not UTF-8 text") from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}: " if mark else ""
        raise HubFileError(f"{path}: {where}{getattr(exc, 'problem', None) or 'invalid YAML'}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise HubFileError(f"{path}: the top level must be a mapping")
    return data


def line_of(path: Path, key: str) -> int | None:
    """The first line where `key:` appears, for error messages."""
    try:
        for number, line in enumerate(Path(path).read_text().splitlines(), start=1):
            if line.strip().startswith((f"{key}:", f"- name: {key}", f"name: {key}")):
                return number
    except OSError:
        return None
    return None
