"""Explicit pack environment metadata for compose env planning."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dokploy_wizard.packs.catalog import iter_pack_catalog

if TYPE_CHECKING:
    from dokploy_wizard.dokploy.env_spec import DokployEnvSpec


@dataclass(frozen=True)
class PackEnvMetadata:
    """Classification and placeholder policy for one pack-owned env key."""

    pack_name: str
    env_key: str
    sensitive: bool
    required: bool
    shared: bool
    source: str
    owner: str
    target_service_suffixes: tuple[str, ...]
    canonical_placeholder_name: str

    def target_services_for_stack(self, stack_name: str) -> tuple[str, ...]:
        return tuple(f"{stack_name}-{suffix}" for suffix in self.target_service_suffixes)

    @property
    def required_placeholder(self) -> str | None:
        if not self.required:
            return None
        return _required_placeholder(self.canonical_placeholder_name)


class PackEnvMetadataError(ValueError):
    """Raised when pack env metadata is incomplete or colliding."""


_SENSITIVE_ENV_KEYS = frozenset(
    {
        "AI_DEFAULT_API_KEY",
        "LITELLM_OPENCODE_GO_API_KEY",
        "LITELLM_OPENROUTER_API_KEY",
        "LITELLM_NVIDIA_API_KEY",
        "OPENCODE_GO_API_KEY",
    }
)
_REQUIRED_ENV_KEYS: frozenset[str] = frozenset()
_SHARED_AI_ENV_KEYS = frozenset(
    {
        "AI_DEFAULT_API_KEY",
        "AI_DEFAULT_BASE_URL",
        "AI_DEFAULT_MODEL",
        "AI_DEFAULT_MODEL_PATTERN",
        "AI_DEFAULT_PROVIDER",
    }
)
_SHARED_LITELLM_ENV_KEYS = frozenset(
    {
        "LITELLM_OPENCODE_GO_API_KEY",
        "LITELLM_OPENROUTER_API_KEY",
        "LITELLM_OPENROUTER_MODELS",
        "LITELLM_NVIDIA_API_KEY",
        "LITELLM_NVIDIA_BASE_URL",
        "LITELLM_NVIDIA_MODELS",
        "NVIDIA_BASE_URL",
    }
)
_SHARED_MAIL_ENV_KEYS = frozenset({"OUTBOUND_SMTP_FROM_ADDRESS", "OUTBOUND_SMTP_HOSTNAME"})

_MAIL_KEYS = ("OUTBOUND_SMTP_HOSTNAME", "OUTBOUND_SMTP_FROM_ADDRESS")
_CODER_KEYS = (
    "HERMES_INFERENCE_PROVIDER",
    "HERMES_MODEL",
    "HERMES_DESKTOP_CHANNEL",
    "AI_DEFAULT_API_KEY",
    "AI_DEFAULT_BASE_URL",
    "AI_DEFAULT_PROVIDER",
    "AI_DEFAULT_MODEL",
    "AI_DEFAULT_MODEL_PATTERN",
    "LITELLM_OPENCODE_GO_API_KEY",
    "LITELLM_OPENROUTER_API_KEY",
    "LITELLM_OPENROUTER_MODELS",
    "LITELLM_NVIDIA_API_KEY",
    "LITELLM_NVIDIA_BASE_URL",
    "LITELLM_NVIDIA_MODELS",
    "NVIDIA_BASE_URL",
    "OPENCODE_GO_API_KEY",
    "OPENCODE_GO_BASE_URL",
)


def iter_pack_env_metadata(pack_name: str | None = None) -> tuple[PackEnvMetadata, ...]:
    if pack_name is None:
        return _PACK_ENV_METADATA
    return tuple(metadata for metadata in _PACK_ENV_METADATA if metadata.pack_name == pack_name)


def get_pack_env_metadata(pack_name: str, env_key: str) -> PackEnvMetadata:
    for metadata in _PACK_ENV_METADATA:
        if metadata.pack_name == pack_name and metadata.env_key == env_key:
            return metadata
    raise KeyError(f"No env metadata for pack '{pack_name}' key '{env_key}'.")


def validate_pack_env_metadata(
    metadata_entries: Iterable[PackEnvMetadata] | None = None,
) -> None:
    metadata = tuple(_PACK_ENV_METADATA if metadata_entries is None else metadata_entries)
    catalog_keys = {
        (pack.name, key) for pack in iter_pack_catalog() for key in pack.mutable_env_keys
    }
    metadata_keys = {(entry.pack_name, entry.env_key) for entry in metadata}
    missing = sorted(catalog_keys - metadata_keys)
    extra = sorted(metadata_keys - catalog_keys)
    if missing or extra:
        raise PackEnvMetadataError(
            "Pack env metadata must exactly cover mutable env keys; "
            f"missing={missing} extra={extra}."
        )

    by_placeholder: dict[str, list[PackEnvMetadata]] = {}
    for entry in metadata:
        by_placeholder.setdefault(entry.canonical_placeholder_name, []).append(entry)

    collisions = []
    for placeholder_name, entries in sorted(by_placeholder.items()):
        if len(entries) <= 1 or all(entry.shared for entry in entries):
            continue
        owners = sorted({entry.owner for entry in entries})
        keys = sorted({entry.env_key for entry in entries})
        collisions.append(f"{placeholder_name} owners={owners} keys={keys}")
    if collisions:
        raise PackEnvMetadataError(
            "Non-shared pack env placeholder collision(s): " + "; ".join(collisions)
        )


def build_pack_env_specs(
    *,
    stack_name: str,
    enabled_packs: tuple[str, ...],
    values: Mapping[str, str],
) -> tuple[DokployEnvSpec, ...]:
    """Build least-privilege env specs from explicit pack metadata and present values."""

    from dokploy_wizard.dokploy.env_spec import DokployEnvSpec

    validate_pack_env_metadata()
    specs_by_name: dict[str, DokployEnvSpec] = {}
    for metadata in _metadata_for_enabled_packs(enabled_packs):
        raw_value = values.get(metadata.env_key, "")
        value = raw_value if raw_value.strip() != "" else _default_value_for_key(metadata.env_key)
        if value is None:
            continue
        spec = _metadata_to_env_spec(metadata, stack_name=stack_name, value=value)
        existing = specs_by_name.get(spec.name)
        if existing is None:
            specs_by_name[spec.name] = spec
            continue
        if not metadata.shared:
            raise PackEnvMetadataError(
                "Non-shared env spec collision for "
                f"key '{metadata.env_key}' owner '{metadata.owner}' placeholder '{spec.name}'."
            )
        if existing.value != spec.value or existing.sensitive != spec.sensitive:
            raise PackEnvMetadataError(
                "Shared env spec collision for "
                f"placeholder '{spec.name}' owners '{existing.owner}' and '{metadata.owner}'."
            )
        specs_by_name[spec.name] = DokployEnvSpec(
            variable=existing.variable,
            owner=existing.owner,
            target_services=tuple(
                dict.fromkeys((*existing.target_services, *spec.target_services))
            ),
            placeholder=existing.placeholder,
            required=existing.required,
            dokploy_scope=existing.dokploy_scope,
            ownership_marker=existing.ownership_marker,
            redacted_fingerprint=existing.redacted_fingerprint,
        )
    return tuple(specs_by_name[name] for name in sorted(specs_by_name))


def _metadata_for_enabled_packs(enabled_packs: tuple[str, ...]) -> tuple[PackEnvMetadata, ...]:
    enabled = set(enabled_packs)
    return tuple(metadata for metadata in _PACK_ENV_METADATA if metadata.pack_name in enabled)


def _default_value_for_key(key: str) -> str | None:
    return None


def _metadata_to_env_spec(
    metadata: PackEnvMetadata, *, stack_name: str, value: str
) -> DokployEnvSpec:
    from dokploy_wizard.dokploy.env_spec import DokployEnvSpec, DokployEnvVar

    return DokployEnvSpec(
        variable=DokployEnvVar(
            name=metadata.canonical_placeholder_name,
            value=value,
            sensitive=metadata.sensitive,
            source=metadata.source,
        ),
        owner=metadata.owner,
        target_services=metadata.target_services_for_stack(stack_name),
        placeholder=metadata.required_placeholder,
        required=metadata.required,
    )


def _pack_entries(
    pack_name: str,
    keys: tuple[str, ...],
    *,
    owner: str,
    target_service_suffixes: tuple[str, ...],
    shared_keys: frozenset[str] = frozenset(),
    source: str = "operator-input",
) -> tuple[PackEnvMetadata, ...]:
    return tuple(
        _pack_entry(
            pack_name,
            key,
            owner=owner,
            target_service_suffixes=_target_suffixes_for_key(
                key, default_suffixes=target_service_suffixes
            ),
            shared=key in shared_keys or key in _SHARED_LITELLM_ENV_KEYS,
            source=_source_for_key(key, default_source=source),
        )
        for key in keys
    )


def _pack_entry(
    pack_name: str,
    key: str,
    *,
    owner: str,
    target_service_suffixes: tuple[str, ...],
    shared: bool,
    source: str,
) -> PackEnvMetadata:
    return PackEnvMetadata(
        pack_name=pack_name,
        env_key=key,
        sensitive=key in _SENSITIVE_ENV_KEYS,
        required=key in _REQUIRED_ENV_KEYS,
        shared=shared,
        source=source,
        owner=_owner_for_key(key, default_owner=owner, shared=shared),
        target_service_suffixes=target_service_suffixes,
        canonical_placeholder_name=(key if shared else _service_prefixed_name(owner, key)),
    )


def _target_suffixes_for_key(
    key: str, *, default_suffixes: tuple[str, ...]
) -> tuple[str, ...]:
    if key in _SHARED_LITELLM_ENV_KEYS:
        return ("shared-litellm",)
    return default_suffixes


def _owner_for_key(key: str, *, default_owner: str, shared: bool) -> str:
    if key in _SHARED_AI_ENV_KEYS:
        return "shared-ai-defaults"
    if key in _SHARED_LITELLM_ENV_KEYS:
        return "shared-litellm"
    if key in _SHARED_MAIL_ENV_KEYS:
        return "shared-mail-relay"
    if shared:
        return f"shared-{default_owner}"
    return default_owner


def _source_for_key(key: str, *, default_source: str) -> str:
    if key in _SHARED_AI_ENV_KEYS:
        return f"operator-input:{key}:shared-ai-defaults"
    if key in _SHARED_LITELLM_ENV_KEYS:
        return f"operator-input:{key}:shared-litellm"
    return f"{default_source}:{key}"


def _service_prefixed_name(owner: str, key: str) -> str:
    return f"{_env_name(owner)}_{key}"


def _env_name(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value.upper()).strip("_")


def _required_placeholder(name: str) -> str:
    return f"${{{name}:?{name} is required}}"


_PACK_ENV_METADATA: tuple[PackEnvMetadata, ...] = (
    *_pack_entries(
        "moodle",
        _MAIL_KEYS,
        owner="shared-mail-relay",
        target_service_suffixes=("shared-postfix",),
        shared_keys=_SHARED_MAIL_ENV_KEYS,
        source="operator-input:mail-relay",
    ),
    *_pack_entries(
        "docuseal",
        _MAIL_KEYS,
        owner="shared-mail-relay",
        target_service_suffixes=("shared-postfix",),
        shared_keys=_SHARED_MAIL_ENV_KEYS,
        source="operator-input:mail-relay",
    ),
    *_pack_entries(
        "coder",
        _CODER_KEYS,
        owner="coder",
        target_service_suffixes=("coder",),
        shared_keys=_SHARED_AI_ENV_KEYS,
    ),
)

validate_pack_env_metadata()
