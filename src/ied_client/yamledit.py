"""Edit one top-level YAML list (``entries:`` / ``quirks:``) as text, keeping every other byte.

The catalogue and quirks files are commented documentation as much as data, and PyYAML does not
keep comments, so they are never re-dumped. Items are located with :func:`yaml.compose` and spliced
in and out as whole lines: an item runs from the line of its ``-`` to its last line that is neither
blank nor a comment (comment lines between two items stay where they are). Only block-style lists
can be edited; a flow-style list (``entries: [...]``) is refused unless it is empty.

Used by ``local edit`` / ``local prune`` (FLD-1, FLD-6) and by ``tools/ingest_field_data.py`` (FLD-5).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import yaml


class YamlEditError(ValueError):
    """The file cannot be edited as text (not a mapping, flow-style list, …)."""


@dataclass(frozen=True)
class Item:
    """One item of the list: lines ``[start, end)`` (0-based), ``-`` at column ``indent``."""

    index: int
    start: int
    end: int
    indent: int
    value: Any


def _is_content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _list_node(text: str, key: str) -> tuple[yaml.Node | None, yaml.Node | None]:
    """(key node, value node) of ``key`` in the top-level mapping, or (None, None)."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise YamlEditError(f"invalid YAML: {exc}") from exc
    if root is None:
        return None, None
    if not isinstance(root, yaml.MappingNode):
        raise YamlEditError("the top level must be a mapping")
    for k, v in root.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return k, v
    return None, None


def items(text: str, key: str) -> list[Item]:
    """The items of the top-level list ``key`` (empty if the key is missing or null)."""
    _, node = _list_node(text, key)
    if node is None or (isinstance(node, yaml.ScalarNode) and node.tag.endswith(":null")):
        return []
    if not isinstance(node, yaml.SequenceNode):
        raise YamlEditError(f"`{key}` must be a list")
    if node.flow_style and node.value:
        raise YamlEditError(f"`{key}` is written in flow style ([...]); write it as a block list (`- ` items)")
    values = (yaml.safe_load(text) or {}).get(key) or []
    lines = text.splitlines(keepends=True)
    dashes: list[int] = []
    for child in node.value:
        line = child.start_mark.line
        while line > 0 and not lines[line].lstrip().startswith("-"):
            line -= 1
        dashes.append(line)
    seq_end = node.end_mark.line if node.end_mark.column == 0 else node.end_mark.line + 1
    seq_end = min(seq_end, len(lines))
    out: list[Item] = []
    for i, start in enumerate(dashes):
        boundary = dashes[i + 1] if i + 1 < len(dashes) else seq_end
        end = start + 1
        for n in range(start, boundary):
            if _is_content(lines[n]):
                end = n + 1
        indent = len(lines[start]) - len(lines[start].lstrip(" "))
        out.append(Item(i, start, end, indent, values[i] if i < len(values) else None))
    return out


def dump_item(value: Any) -> str:
    """One item as block YAML at column 0 (``- key: value`` …), ending with a newline."""
    return yaml.safe_dump([value], sort_keys=False, allow_unicode=True, default_flow_style=False, width=100)


def item_text(text: str, item: Item) -> str:
    """The item's own lines, moved to column 0."""
    lines = text.splitlines(keepends=True)[item.start : item.end]
    out = []
    for ln in lines:
        body = ln.rstrip("\n")
        cut = min(item.indent, len(body) - len(body.lstrip(" ")))
        out.append(body[cut:] + "\n")
    return "".join(out)


def _indent(block: str, indent: int) -> list[str]:
    pad = " " * indent
    out = []
    for ln in block.splitlines():
        out.append((pad + ln if ln.strip() else "") + "\n")
    return out


def _ensure_newline(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


def remove(text: str, key: str, indices: Iterable[int]) -> str:
    """``text`` without the listed items (and without a blank line doubled by the removal)."""
    wanted = set(indices)
    lines = _ensure_newline(text).splitlines(keepends=True)
    for item in sorted((i for i in items(text, key) if i.index in wanted), key=lambda i: i.start, reverse=True):
        del lines[item.start : item.end]
        if (
            item.start < len(lines)
            and not lines[item.start].strip()
            and (item.start == 0 or not lines[item.start - 1].strip())
        ):
            del lines[item.start]
    return "".join(lines)


def replace(text: str, key: str, index: int, new_item: str) -> str:
    """Replace item ``index`` by ``new_item`` (block YAML at column 0, as from :func:`dump_item`)."""
    found = [i for i in items(text, key) if i.index == index]
    if not found:
        raise YamlEditError(f"`{key}` has no item #{index + 1}")
    item = found[0]
    lines = _ensure_newline(text).splitlines(keepends=True)
    lines[item.start : item.end] = _indent(new_item, item.indent)
    return "".join(lines)


def append(text: str, key: str, new_items: Sequence[str], *, default_indent: int = 0, blank_line: bool = True) -> str:
    """Append items (block YAML at column 0) at the end of the list ``key``, creating it if needed.

    New items take the indentation of the existing ones (``default_indent`` for an empty list) and
    are separated by a blank line when the existing items are (or ``blank_line`` for an empty list).
    """
    if not new_items:
        return text
    text = _ensure_newline(text)
    lines = text.splitlines(keepends=True)
    current = items(text, key)
    if current:
        last = current[-1]
        indent = last.indent
        sep = not lines[last.start - 1].strip() if len(current) > 1 else blank_line
        at = last.end
    else:
        key_node, value = _list_node(text, key)
        indent = default_indent
        sep = blank_line
        if key_node is None:
            if lines and lines[-1].strip() and blank_line:
                lines.append("\n")
            lines.append(f"{key}:\n")
            at = len(lines)
        else:
            kline = key_node.start_mark.line
            if isinstance(value, yaml.SequenceNode) and value.flow_style:  # `key: []`
                lines[kline] = lines[kline][: key_node.start_mark.column] + f"{key}:\n"
            at = kline + 1
    block: list[str] = []
    for n, new in enumerate(new_items):
        if sep and (current or n > 0):
            block.append("\n")
        block.extend(_indent(new, indent))
    lines[at:at] = block
    return "".join(lines)
