"""Strict YAML for deployment files.

People and agents edit these files by hand, so a mistake must fail with the
file, the line and the key, never half-apply. Rules:

* no tags (``!!python/...`` or any other), anchors or aliases,
* no duplicate keys,
* the document is a mapping.

``load`` returns the data plus a map from JSON Pointer to the 1-based line it
came from, so a validation error on ``/presentation/appearance/default_mode``
can name its line.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


class YamlError(ValueError):
    def __init__(self, message: str, *, path: str | None = None, line: int | None = None):
        super().__init__(message)
        self.message = message
        self.path = path
        self.line = line

    def where(self) -> str:
        bits = [b for b in (self.path, f"line {self.line}" if self.line else None) if b]
        return ", ".join(bits)


@dataclass
class Loaded:
    data: dict
    lines: dict[str, int]
    path: str | None = None

    def line_of(self, pointer: str) -> int | None:
        """The line of ``pointer``, or of its nearest parent that has one."""
        while True:
            if pointer in self.lines:
                return self.lines[pointer]
            if not pointer or "/" not in pointer:
                return self.lines.get("")
            pointer = pointer.rsplit("/", 1)[0]


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


_PLAIN_TAGS = {
    "tag:yaml.org,2002:str", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:bool", "tag:yaml.org,2002:null", "tag:yaml.org,2002:map",
    "tag:yaml.org,2002:seq",
}


def _convert(node, pointer: str, lines: dict[str, int], where: str | None) -> Any:
    lines.setdefault(pointer, node.start_mark.line + 1)
    if node.tag not in _PLAIN_TAGS:
        raise YamlError(f"YAML tags are not allowed ({node.tag}).", path=where,
                        line=node.start_mark.line + 1)
    if isinstance(node, MappingNode):
        out: dict = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode):
                raise YamlError("Keys must be plain text.", path=where,
                                line=key_node.start_mark.line + 1)
            key = yaml.SafeLoader.construct_scalar(_LOADER, key_node)
            key = _resolve_scalar(key_node, key)
            if not isinstance(key, str):
                key = str(key)
            if key in out:
                raise YamlError(f"The key {key!r} appears twice.", path=where,
                                line=key_node.start_mark.line + 1)
            child = f"{pointer}/{_escape(key)}"
            lines[child] = key_node.start_mark.line + 1
            out[key] = _convert(value_node, child, lines, where)
        return out
    if isinstance(node, SequenceNode):
        return [_convert(item, f"{pointer}/{i}", lines, where) for i, item in enumerate(node.value)]
    value = yaml.SafeLoader.construct_scalar(_LOADER, node)
    return _resolve_scalar(node, value)


class _Loader(yaml.SafeLoader):
    pass


_LOADER = _Loader("")


def _resolve_scalar(node: ScalarNode, raw: str) -> Any:
    tag = node.tag
    if tag == "tag:yaml.org,2002:str":
        return raw
    if tag == "tag:yaml.org,2002:null":
        return None
    if tag == "tag:yaml.org,2002:bool":
        return _LOADER.construct_yaml_bool(node)
    if tag == "tag:yaml.org,2002:int":
        return _LOADER.construct_yaml_int(node)
    if tag == "tag:yaml.org,2002:float":
        return _LOADER.construct_yaml_float(node)
    return raw


def loads(text: str, *, path: str | None = None) -> Loaded:
    """Parse ``text`` strictly. Raises ``YamlError``."""
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                mark = event.start_mark
                raise YamlError("YAML anchors and aliases are not allowed.", path=path,
                                line=mark.line + 1)
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except YamlError:
        raise
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or str(exc)
        raise YamlError(f"This is not valid YAML: {problem}.", path=path,
                        line=(mark.line + 1) if mark else None) from exc
    if node is None:
        return Loaded({}, {}, path)
    lines: dict[str, int] = {}
    data = _convert(node, "", lines, path)
    if not isinstance(data, dict):
        raise YamlError("The file must be a mapping of keys to values.", path=path, line=1)
    return Loaded(data, lines, path)


def load(path: Path) -> Loaded:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise YamlError("The file does not exist.", path=str(path)) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise YamlError(f"The file can't be read: {exc}.", path=str(path)) from exc
    return loads(text, path=str(path))


def dumps(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
