from __future__ import annotations

import json
from dataclasses import dataclass

from dokploy_wizard.dokploy.workspace_catalog_sync_json import json_value_sha
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    JsonValue,
    WorkspaceCatalogSyncError,
)
from dokploy_wizard.dokploy.workspace_catalog_sync_yaml_render import render_yaml_entry


@dataclass(frozen=True, slots=True)
class YamlPointerPatch:
    content: bytes
    pre_sha256: str | None
    post_sha256: str


@dataclass(frozen=True, slots=True)
class _Node:
    key: str
    start: int
    end: int
    indent: int
    scalar: str


def patch_yaml_pointer(
    content: bytes, pointer: tuple[str, ...], value: JsonValue
) -> YamlPointerPatch:
    lines = _document_lines(content)
    if not pointer:
        raise WorkspaceCatalogSyncError("workspace Hermes YAML pointer is empty")
    start, end, indent = 0, len(lines), 0
    for index, segment in enumerate(pointer):
        nodes = _nodes(lines, start, end, indent)
        node = next((candidate for candidate in nodes if candidate.key == segment), None)
        if node is None:
            nested = value
            for child in reversed(pointer[index + 1 :]):
                nested = {child: nested}
            rendered = _insert(lines, end, render_yaml_entry(segment, nested, indent))
            return YamlPointerPatch(
                content=rendered,
                pre_sha256=None,
                post_sha256=json_value_sha(value),
            )
        if index == len(pointer) - 1:
            previous = _node_value(lines, node)
            replacement = render_yaml_entry(segment, value, indent)
            rendered = "".join(lines[: node.start] + replacement + lines[node.end :]).encode()
            return YamlPointerPatch(
                content=rendered,
                pre_sha256=json_value_sha(previous),
                post_sha256=json_value_sha(value),
            )
        if node.scalar:
            raise WorkspaceCatalogSyncError("workspace Hermes YAML pointer parent is not a mapping")
        start, end, indent = node.start + 1, node.end, indent + 2
    raise WorkspaceCatalogSyncError("workspace Hermes YAML pointer is invalid")


def yaml_pointer_exists(content: bytes, pointer: tuple[str, ...]) -> bool:
    lines = _document_lines(content)
    start, end, indent = 0, len(lines), 0
    for index, segment in enumerate(pointer):
        node = next(
            (
                candidate
                for candidate in _nodes(lines, start, end, indent)
                if candidate.key == segment
            ),
            None,
        )
        if node is None:
            return False
        if index != len(pointer) - 1:
            if node.scalar:
                raise WorkspaceCatalogSyncError(
                    "workspace Hermes YAML pointer parent is not a mapping"
                )
            start, end, indent = node.start + 1, node.end, indent + 2
    return True


def yaml_value_at(content: bytes, pointer: tuple[str, ...]) -> JsonValue:
    lines = _document_lines(content)
    start, end, indent = 0, len(lines), 0
    for index, segment in enumerate(pointer):
        node = next(
            (
                candidate
                for candidate in _nodes(lines, start, end, indent)
                if candidate.key == segment
            ),
            None,
        )
        if node is None:
            raise WorkspaceCatalogSyncError("workspace Hermes YAML pointer is absent")
        if index == len(pointer) - 1:
            return _node_value(lines, node)
        if node.scalar:
            raise WorkspaceCatalogSyncError("workspace Hermes YAML pointer parent is not a mapping")
        start, end, indent = node.start + 1, node.end, indent + 2
    raise WorkspaceCatalogSyncError("workspace Hermes YAML pointer is empty")


def yaml_pointer_sha(content: bytes, pointer: tuple[str, ...]) -> str:
    return json_value_sha(yaml_value_at(content, pointer))


def _document_lines(content: bytes) -> list[str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkspaceCatalogSyncError("workspace Hermes YAML is invalid") from error
    lines = text.splitlines(keepends=True)
    if text and not lines:
        lines = [text]
    _validate_mapping(lines, 0, len(lines), 0)
    return lines


def _validate_mapping(lines: list[str], start: int, end: int, indent: int) -> None:
    for node in _nodes(lines, start, end, indent):
        if node.scalar or node.start + 1 == node.end:
            continue
        child_line = _next_content(lines, node.start + 1, node.end)
        if child_line is None:
            continue
        child_indent, child = _line(lines[child_line])
        if child_indent != indent + 2:
            raise WorkspaceCatalogSyncError("workspace Hermes YAML indentation is invalid")
        if child.startswith("- "):
            _validate_sequence(lines, node.start + 1, node.end, indent + 2)
        else:
            _validate_mapping(lines, node.start + 1, node.end, indent + 2)


def _validate_sequence(lines: list[str], start: int, end: int, indent: int) -> None:
    for index in range(start, end):
        if _ignorable(lines[index]):
            continue
        actual, body = _line(lines[index])
        if actual != indent or not body.startswith("- ") or not body[2:].strip():
            raise WorkspaceCatalogSyncError("workspace Hermes YAML sequence is invalid")


def _nodes(lines: list[str], start: int, end: int, indent: int) -> tuple[_Node, ...]:
    nodes: list[_Node] = []
    index = start
    while index < end:
        if _ignorable(lines[index]):
            index += 1
            continue
        actual, body = _line(lines[index])
        if actual < indent:
            break
        if actual != indent or body.startswith("- "):
            raise WorkspaceCatalogSyncError("workspace Hermes YAML structure is invalid")
        key, scalar = _mapping_parts(body)
        block_end = _block_end(lines, index, end, indent)
        if scalar.strip() and _next_content(lines, index + 1, block_end) is not None:
            raise WorkspaceCatalogSyncError("workspace Hermes YAML scalar has children")
        nodes.append(
            _Node(
                key=key,
                start=index,
                end=block_end,
                indent=indent,
                scalar=scalar.strip(),
            )
        )
        index = block_end
    keys = [node.key for node in nodes]
    if len(keys) != len(set(keys)):
        raise WorkspaceCatalogSyncError("workspace Hermes YAML mapping keys are not unique")
    return tuple(nodes)


def _node_value(lines: list[str], node: _Node) -> JsonValue:
    if node.scalar:
        return _scalar(node.scalar)
    child_line = _next_content(lines, node.start + 1, node.end)
    if child_line is None:
        return {}
    _, child = _line(lines[child_line])
    if child.startswith("- "):
        values: list[JsonValue] = []
        for index in range(node.start + 1, node.end):
            if _ignorable(lines[index]):
                continue
            _, body = _line(lines[index])
            values.append(_scalar(body[2:].strip()))
        return values
    return {
        child_node.key: _node_value(lines, child_node)
        for child_node in _nodes(lines, node.start + 1, node.end, node.indent + 2)
    }


def _scalar(value: str) -> JsonValue:
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "null":
        return None
    if value.startswith('"'):
        try:
            parsed: JsonValue = json.loads(value)
        except json.JSONDecodeError as error:
            raise WorkspaceCatalogSyncError("workspace Hermes YAML scalar is invalid") from error
        return parsed
    return value


def _mapping_parts(body: str) -> tuple[str, str]:
    if body.startswith('"'):
        try:
            key, end = json.JSONDecoder().raw_decode(body)
        except json.JSONDecodeError as error:
            raise WorkspaceCatalogSyncError(
                "workspace Hermes YAML mapping key is invalid"
            ) from error
        if not isinstance(key, str) or end >= len(body) or body[end] != ":":
            raise WorkspaceCatalogSyncError("workspace Hermes YAML mapping key is invalid")
        return key, body[end + 1 :].strip()
    key, separator, scalar = body.partition(":")
    if not separator or not key or key != key.strip():
        raise WorkspaceCatalogSyncError("workspace Hermes YAML mapping key is invalid")
    return key, scalar.strip()


def _insert(lines: list[str], index: int, rendered: list[str]) -> bytes:
    prefix: list[str] = []
    if index and not lines[index - 1].endswith(("\n", "\r")):
        prefix.append("\n")
    return "".join(lines[:index] + prefix + rendered + lines[index:]).encode()


def _block_end(lines: list[str], start: int, end: int, indent: int) -> int:
    for index in range(start + 1, end):
        if _ignorable(lines[index]):
            continue
        actual, _ = _line(lines[index])
        if actual <= indent:
            return index
    return end


def _next_content(lines: list[str], start: int, end: int) -> int | None:
    return next((index for index in range(start, end) if not _ignorable(lines[index])), None)


def _line(line: str) -> tuple[int, str]:
    body = line.rstrip("\r\n")
    leading = body[: len(body) - len(body.lstrip(" \t"))]
    if "\t" in leading or len(leading) % 2:
        raise WorkspaceCatalogSyncError("workspace Hermes YAML indentation is invalid")
    return len(leading), body[len(leading) :]


def _ignorable(line: str) -> bool:
    return not line.strip() or line.lstrip().startswith("#")
