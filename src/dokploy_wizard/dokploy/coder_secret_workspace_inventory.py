from __future__ import annotations

import json
import re
from typing import Final
from uuid import UUID

from dokploy_wizard.dokploy.coder_migration_types import JsonValue
from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError
from dokploy_wizard.dokploy.coder_secret_workspace_contract import (
    WorkspaceHashError,
    WorkspaceRecord,
    WorkspaceTemplate,
)

_PRIMARY_TEMPLATE: Final = "ubuntu-vscode-opencode-pi"
_LEGACY_PRIMARY_TEMPLATE: Final = "ubuntu-vscode"
_ENV_NAME: Final = re.compile(r"[A-Z_][A-Z0-9_]*\Z")


def verification_template(output: str) -> WorkspaceTemplate:
    try:
        value: JsonValue = json.loads(output)
    except json.JSONDecodeError as error:
        raise CoderSecretClientError(
            "Coder verification template payload is malformed",
            kind="client_workspace_template_payload",
        ) from error
    match value:
        case list() as templates:
            candidates = tuple(_template_candidate(template) for template in templates)
            primary_candidates = tuple(
                candidate
                for candidate in candidates
                if candidate.get("name") == _PRIMARY_TEMPLATE
            )
        case _:
            raise CoderSecretClientError(
                "Coder verification template payload is not a list",
                kind="client_workspace_template_root",
            )
    match primary_candidates:
        case (primary_candidate,):
            return _verification_template_record(primary_candidate)
        case ():
            legacy_candidates = tuple(
                candidate
                for candidate in candidates
                if candidate.get("name") == _LEGACY_PRIMARY_TEMPLATE
            )
            match legacy_candidates:
                case (legacy_candidate,):
                    return _verification_template_record(legacy_candidate)
                case ():
                    raise CoderSecretClientError(
                        "Coder verification template is absent",
                        kind="client_workspace_template_primary_absent",
                    )
                case _:
                    raise CoderSecretClientError(
                        "Coder legacy verification template is ambiguous",
                        kind="client_workspace_template_legacy_ambiguous",
                    )
        case _:
            raise CoderSecretClientError(
                "Coder primary verification template is ambiguous",
                kind="client_workspace_template_primary_ambiguous",
            )


def _verification_template_record(value: dict[str, JsonValue]) -> WorkspaceTemplate:
    try:
        return _template_record(value)
    except CoderSecretClientError as error:
        raise CoderSecretClientError(
            "Coder verification template record is invalid",
            kind="client_workspace_template_record_invalid",
        ) from error


def _template_candidate(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise CoderSecretClientError(
            "Coder verification template record is invalid",
            kind="client_workspace_template_record_invalid",
        )
    if "Template" not in value:
        return value
    inner = value["Template"]
    if not isinstance(inner, dict):
        raise CoderSecretClientError(
            "Coder verification template record is invalid",
            kind="client_workspace_template_record_invalid",
        )
    return inner


def workspace_records(output: str) -> tuple[WorkspaceRecord, ...]:
    value = _json(output, "workspace inventory")
    match value:
        case list() as workspaces:
            return tuple(_workspace(workspace) for workspace in workspaces)
        case _:
            raise CoderSecretClientError(
                "Coder workspace inventory is invalid", kind="client_workspace_inventory"
            )


def environment_name(value: str) -> str:
    if _ENV_NAME.fullmatch(value) is None:
        raise CoderSecretClientError(
            "Coder verification environment name is invalid",
            kind="client_workspace_intent",
        )
    return value


def workspace_hash_command(env_name: str) -> str:
    return f'printf %s "${{{env_name}}}" | sha256sum | cut -d " " -f1'


def sha256_output(output: str) -> str:
    lines = output.splitlines()
    if (
        len(lines) != 1
        or len(lines[0]) != 64
        or any(character not in "0123456789abcdef" for character in lines[0])
    ):
        raise WorkspaceHashError("Coder verification workspace hash is invalid")
    return lines[0]


def _template_record(value: dict[str, JsonValue]) -> WorkspaceTemplate:
    return WorkspaceTemplate(
        _uuid(value.get("id"), "template ID"),
        _text(value.get("name"), "template name"),
    )


def _workspace(value: JsonValue) -> WorkspaceRecord:
    match value:
        case dict() as mapping:
            latest_build = mapping.get("latest_build")
            match latest_build:
                case dict() as build:
                    return WorkspaceRecord(
                        _uuid(mapping.get("id"), "workspace ID"),
                        _text(mapping.get("name"), "workspace name"),
                        _uuid(mapping.get("owner_id"), "workspace owner ID"),
                        _text(mapping.get("owner_name"), "workspace owner name"),
                        _uuid(mapping.get("template_id"), "workspace template ID"),
                        _text(mapping.get("template_name"), "workspace template name"),
                        _text(build.get("status"), "workspace status"),
                    )
                case _:
                    raise CoderSecretClientError(
                        "Coder workspace latest build is invalid",
                        kind="client_workspace_inventory",
                    )
        case _:
            raise CoderSecretClientError(
                "Coder workspace inventory is invalid", kind="client_workspace_inventory"
            )


def _json(output: str, label: str) -> JsonValue:
    try:
        value: JsonValue = json.loads(output)
    except json.JSONDecodeError as error:
        raise CoderSecretClientError(
            f"Coder {label} is malformed",
            kind=(
                "client_workspace_template"
                if label == "templates"
                else "client_workspace_inventory"
            ),
        ) from error
    return value


def _uuid(value: JsonValue | None, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise CoderSecretClientError(
            "Coder workspace identity is invalid", kind="client_workspace_identity"
        ) from error
    if str(parsed) != text:
        raise CoderSecretClientError(
            "Coder workspace identity is invalid", kind="client_workspace_identity"
        )
    return text


def _text(value: JsonValue | None, label: str) -> str:
    match value:
        case str() as text if text:
            return text
        case _:
            raise CoderSecretClientError(
                f"Coder {label} is invalid", kind="client_workspace_inventory"
            )
