from __future__ import annotations

import json
from typing import assert_never

from dokploy_wizard.dokploy.workspace_catalog_sync_models import JsonValue


def render_yaml_entry(key: str, value: JsonValue, indent: int) -> list[str]:
    prefix = " " * indent
    rendered_key = _render_key(key)
    match value:
        case dict() as mapping:
            lines = [f"{prefix}{rendered_key}:\n"]
            for child, child_value in mapping.items():
                lines.extend(render_yaml_entry(child, child_value, indent + 2))
            return lines
        case list() as values:
            lines = [f"{prefix}{rendered_key}:\n"]
            lines.extend(f"{prefix}  - {_render_scalar(item)}\n" for item in values)
            return lines
        case str() | int() | float() | bool() | None:
            return [f"{prefix}{rendered_key}: {_render_scalar(value)}\n"]
        case unreachable:
            assert_never(unreachable)


def _render_scalar(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _render_key(value: str) -> str:
    if value and all(
        character.isascii() and (character.isalnum() or character in "_-./") for character in value
    ):
        return value
    return json.dumps(value, ensure_ascii=False)
