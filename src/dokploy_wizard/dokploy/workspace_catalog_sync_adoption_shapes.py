from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from dokploy_wizard.dokploy.workspace_catalog_sync_io import sha256
from dokploy_wizard.dokploy.workspace_catalog_sync_models import JsonValue, TransactionBlockedError


@dataclass(frozen=True, slots=True)
class LegacyShapeBinding:
    base_url: str
    credential_value_sha256: str


ShapeValidator = Callable[[Mapping[str, JsonValue], LegacyShapeBinding], None]


def validate_json_shape(value: JsonValue, pointer: str, binding: LegacyShapeBinding) -> None:
    mapping = _mapping(value, "workspace legacy JSON pointer")
    validators: Mapping[str, ShapeValidator] = {
        "/provider/litellm": _validate_opencode,
        "/providers/litellm": _validate_pi,
        "/github.copilot.chat.customOAIModels": _validate_copilot,
    }
    try:
        validator = validators[pointer]
    except KeyError as error:
        raise TransactionBlockedError("workspace legacy JSON pointer is unsupported") from error
    validator(mapping, binding)


def validate_yaml_shape(value: JsonValue, pointer: str, binding: LegacyShapeBinding) -> None:
    mapping = _mapping(value, "workspace legacy Hermes pointer")
    validators: Mapping[str, ShapeValidator] = {
        "/providers/openai": _validate_hermes_provider,
        "/platforms/api_server/extra/model_routes": _validate_hermes_routes,
    }
    try:
        validator = validators[pointer]
    except KeyError as error:
        raise TransactionBlockedError("workspace legacy YAML pointer is unsupported") from error
    validator(mapping, binding)


def _validate_opencode(mapping: Mapping[str, JsonValue], binding: LegacyShapeBinding) -> None:
    options = _mapping(mapping.get("options"), "workspace legacy OpenCode options")
    _require_base(options.get("baseURL"), binding.base_url)
    _require_credential_reference(options.get("apiKey"), binding.credential_value_sha256)
    _mapping(mapping.get("models"), "workspace legacy OpenCode models")


def _validate_pi(mapping: Mapping[str, JsonValue], binding: LegacyShapeBinding) -> None:
    _require_base(mapping.get("baseUrl"), binding.base_url)
    _require_credential_reference(mapping.get("apiKey"), binding.credential_value_sha256)
    if not isinstance(mapping.get("models"), list):
        raise TransactionBlockedError("workspace legacy Pi models are invalid")


def _validate_copilot(mapping: Mapping[str, JsonValue], binding: LegacyShapeBinding) -> None:
    for model in mapping.values():
        item = _mapping(model, "workspace legacy Copilot model")
        _require_base(item.get("url"), binding.base_url)
        _require_credential_reference(item.get("apiKey"), binding.credential_value_sha256)


def _validate_hermes_provider(
    mapping: Mapping[str, JsonValue], binding: LegacyShapeBinding
) -> None:
    _require_base(mapping.get("base_url"), binding.base_url)
    if mapping.get("key_env") != "OPENAI_API_KEY" or mapping.get("discover_models") is not False:
        raise TransactionBlockedError("workspace legacy Hermes provider is invalid")
    if not isinstance(mapping.get("models"), list):
        raise TransactionBlockedError("workspace legacy Hermes models are invalid")


def _validate_hermes_routes(mapping: Mapping[str, JsonValue], binding: LegacyShapeBinding) -> None:
    del binding
    if not mapping or any(provider != "openai" for provider in mapping.values()):
        raise TransactionBlockedError("workspace legacy Hermes routes are invalid")


def _mapping(value: JsonValue | None, label: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, dict):
        raise TransactionBlockedError(f"{label} must be an object")
    return value


def _require_base(value: JsonValue | None, expected: str) -> None:
    if value != expected:
        raise TransactionBlockedError("workspace legacy base URL does not match evidence")


def _require_credential_reference(value: JsonValue | None, expected_sha256: str) -> None:
    if not isinstance(value, str) or not value or sha256(value.encode()) != expected_sha256:
        raise TransactionBlockedError("workspace legacy credential reference is invalid")
