from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

from dokploy_wizard.dokploy.workspace_catalog_sync_io import sha256
from dokploy_wizard.dokploy.workspace_catalog_sync_models import (
    JsonValue,
    WorkspaceCatalogSyncError,
)


@dataclass(frozen=True, slots=True)
class JsonPointerPatch:
    content: bytes
    pre_sha256: str | None
    post_sha256: str


@dataclass(frozen=True, slots=True)
class _Member:
    key: str
    value_start: int
    value_end: int


_DECODER: Final = json.JSONDecoder(object_pairs_hook=lambda pairs: _unique_object(pairs))


def compact_json(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def json_value_sha(value: JsonValue) -> str:
    return sha256(compact_json(value))


def parse_json_pointer(pointer: str) -> tuple[str, ...]:
    if not pointer.startswith("/"):
        raise WorkspaceCatalogSyncError("workspace JSON pointer is invalid")
    segments: list[str] = []
    for raw in pointer[1:].split("/"):
        segment = ""
        index = 0
        while index < len(raw):
            if raw[index] != "~":
                segment += raw[index]
                index += 1
                continue
            if index + 1 == len(raw) or raw[index + 1] not in "01":
                raise WorkspaceCatalogSyncError("workspace JSON pointer is invalid")
            segment += "~" if raw[index + 1] == "0" else "/"
            index += 2
        if not segment:
            raise WorkspaceCatalogSyncError("workspace JSON pointer is invalid")
        segments.append(segment)
    return tuple(segments)


def patch_json_pointer(
    content: bytes, pointer: tuple[str, ...], value: JsonValue
) -> JsonPointerPatch:
    text, root_start = _document(content)
    if not pointer:
        raise WorkspaceCatalogSyncError("workspace JSON pointer is empty")
    current_start = root_start
    for index, segment in enumerate(pointer):
        members, close = _members(text, current_start)
        member = next((candidate for candidate in members if candidate.key == segment), None)
        if member is None:
            nested = value
            for child in reversed(pointer[index + 1 :]):
                nested = {child: nested}
            insertion = (
                json.dumps(segment, ensure_ascii=False) + ":" + compact_json(nested).decode()
            )
            if members:
                insertion = "," + insertion
            rendered = (text[:close] + insertion + text[close:]).encode()
            return JsonPointerPatch(
                content=rendered,
                pre_sha256=None,
                post_sha256=json_value_sha(value),
            )
        if index == len(pointer) - 1:
            previous = _decode(text, member.value_start)[0]
            replacement = compact_json(value).decode()
            rendered = (
                text[: member.value_start] + replacement + text[member.value_end :]
            ).encode()
            return JsonPointerPatch(
                content=rendered,
                pre_sha256=json_value_sha(previous),
                post_sha256=json_value_sha(value),
            )
        if text[member.value_start] != "{":
            raise WorkspaceCatalogSyncError("workspace JSON pointer parent must be an object")
        current_start = member.value_start
    raise WorkspaceCatalogSyncError("workspace JSON pointer is invalid")


def json_value_at(content: bytes, pointer: str) -> JsonValue:
    text, current_start = _document(content)
    for segment in parse_json_pointer(pointer):
        members, _ = _members(text, current_start)
        member = next((candidate for candidate in members if candidate.key == segment), None)
        if member is None:
            raise WorkspaceCatalogSyncError("workspace JSON pointer is absent")
        value, _ = _decode(text, member.value_start)
        current_start = member.value_start
    return value


def json_pointer_sha(content: bytes, pointer: str) -> str:
    return json_value_sha(json_value_at(content, pointer))


def json_document_sha(content: bytes) -> str:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkspaceCatalogSyncError("workspace JSON target is invalid") from error
    root_start = _whitespace(text, 0)
    value, end = _decode(text, root_start)
    if _whitespace(text, end) != len(text):
        raise WorkspaceCatalogSyncError("workspace JSON target is invalid")
    return json_value_sha(value)


def _document(content: bytes) -> tuple[str, int]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkspaceCatalogSyncError("workspace JSON target is invalid") from error
    start = _whitespace(text, 0)
    value, end = _decode(text, start)
    if not isinstance(value, dict) or _whitespace(text, end) != len(text):
        raise WorkspaceCatalogSyncError("workspace JSON target must be one object")
    _validate_unique_members(text, start)
    return text, start


def _decode(text: str, start: int) -> tuple[JsonValue, int]:
    try:
        value, end = _DECODER.raw_decode(text, start)
    except json.JSONDecodeError as error:
        raise WorkspaceCatalogSyncError("workspace JSON target is invalid") from error
    return value, end


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> JsonValue:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise WorkspaceCatalogSyncError("workspace JSON object keys are not unique")
    return dict(pairs)


def _members(text: str, start: int) -> tuple[tuple[_Member, ...], int]:
    if start >= len(text) or text[start] != "{":
        raise WorkspaceCatalogSyncError("workspace JSON pointer parent must be an object")
    position = _whitespace(text, start + 1)
    members: list[_Member] = []
    if position < len(text) and text[position] == "}":
        return (), position
    while position < len(text):
        key, key_end = _decode(text, position)
        if not isinstance(key, str):
            raise WorkspaceCatalogSyncError("workspace JSON object key is invalid")
        position = _whitespace(text, key_end)
        if position >= len(text) or text[position] != ":":
            raise WorkspaceCatalogSyncError("workspace JSON target is invalid")
        value_start = _whitespace(text, position + 1)
        _, value_end = _decode(text, value_start)
        members.append(_Member(key=key, value_start=value_start, value_end=value_end))
        position = _whitespace(text, value_end)
        if position < len(text) and text[position] == "}":
            return tuple(members), position
        if position >= len(text) or text[position] != ",":
            raise WorkspaceCatalogSyncError("workspace JSON target is invalid")
        position = _whitespace(text, position + 1)
    raise WorkspaceCatalogSyncError("workspace JSON target is invalid")


def _validate_unique_members(text: str, start: int) -> None:
    members, _ = _members(text, start)
    keys = [member.key for member in members]
    if len(keys) != len(set(keys)):
        raise WorkspaceCatalogSyncError("workspace JSON object keys are not unique")
    for member in members:
        if text[member.value_start] == "{":
            _validate_unique_members(text, member.value_start)


def _whitespace(text: str, start: int) -> int:
    position = start
    while position < len(text) and text[position] in " \t\r\n":
        position += 1
    return position
