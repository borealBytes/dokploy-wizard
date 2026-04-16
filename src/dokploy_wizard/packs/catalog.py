"""Pure pack metadata catalog for selection and planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PackHostname:
    key: str
    default_subdomain: str
    env_key: str


@dataclass(frozen=True)
class PackDefinition:
    name: str
    prompt_label: str
    env_flag: str
    default_enabled: bool
    depends_on: tuple[str, ...]
    slot: str | None
    shared_core_requirements: tuple[str, ...]
    hostnames: tuple[PackHostname, ...]
    mutable_env_keys: tuple[str, ...]
    mutable_resource_keys: tuple[str, ...]
    enabled_features: tuple[str, ...]
    resource_profile: Literal["core", "recommended"]


_PACK_CATALOG: tuple[PackDefinition, ...] = (
    PackDefinition(
        name="headscale",
        prompt_label="Headscale",
        env_flag="ENABLE_HEADSCALE",
        default_enabled=True,
        depends_on=(),
        slot=None,
        shared_core_requirements=(),
        hostnames=(
            PackHostname(
                key="headscale",
                default_subdomain="headscale",
                env_key="HEADSCALE_SUBDOMAIN",
            ),
        ),
        mutable_env_keys=(),
        mutable_resource_keys=(),
        enabled_features=("headscale",),
        resource_profile="core",
    ),
    PackDefinition(
        name="matrix",
        prompt_label="Matrix",
        env_flag="ENABLE_MATRIX",
        default_enabled=False,
        depends_on=("headscale",),
        slot=None,
        shared_core_requirements=("postgres", "redis"),
        hostnames=(
            PackHostname(
                key="matrix",
                default_subdomain="matrix",
                env_key="MATRIX_SUBDOMAIN",
            ),
        ),
        mutable_env_keys=(),
        mutable_resource_keys=(),
        enabled_features=(),
        resource_profile="recommended",
    ),
    PackDefinition(
        name="nextcloud",
        prompt_label="Nextcloud + OnlyOffice",
        env_flag="ENABLE_NEXTCLOUD",
        default_enabled=False,
        depends_on=("headscale",),
        slot=None,
        shared_core_requirements=("postgres", "redis"),
        hostnames=(
            PackHostname(
                key="nextcloud",
                default_subdomain="nextcloud",
                env_key="NEXTCLOUD_SUBDOMAIN",
            ),
            PackHostname(
                key="onlyoffice",
                default_subdomain="office",
                env_key="ONLYOFFICE_SUBDOMAIN",
            ),
        ),
        mutable_env_keys=(),
        mutable_resource_keys=(),
        enabled_features=(),
        resource_profile="recommended",
    ),
    PackDefinition(
        name="seaweedfs",
        prompt_label="SeaweedFS",
        env_flag="ENABLE_SEAWEEDFS",
        default_enabled=False,
        depends_on=(),
        slot=None,
        shared_core_requirements=(),
        hostnames=(
            PackHostname(
                key="s3",
                default_subdomain="s3",
                env_key="SEAWEEDFS_SUBDOMAIN",
            ),
        ),
        mutable_env_keys=(),
        mutable_resource_keys=(),
        enabled_features=(),
        resource_profile="recommended",
    ),
    PackDefinition(
        name="coder",
        prompt_label="Coder",
        env_flag="ENABLE_CODER",
        default_enabled=False,
        depends_on=(),
        slot=None,
        shared_core_requirements=("postgres",),
        hostnames=(
            PackHostname(
                key="coder",
                default_subdomain="coder",
                env_key="CODER_SUBDOMAIN",
            ),
            PackHostname(
                key="coder-wildcard",
                default_subdomain="*.coder",
                env_key="CODER_WILDCARD_SUBDOMAIN",
            ),
        ),
        mutable_env_keys=(),
        mutable_resource_keys=(),
        enabled_features=(),
        resource_profile="recommended",
    ),
    PackDefinition(
        name="openclaw",
        prompt_label="OpenClaw",
        env_flag="ENABLE_OPENCLAW",
        default_enabled=False,
        depends_on=("headscale",),
        slot=None,
        shared_core_requirements=("postgres",),
        hostnames=(
            PackHostname(
                key="openclaw",
                default_subdomain="openclaw",
                env_key="OPENCLAW_SUBDOMAIN",
            ),
        ),
        mutable_env_keys=(
            "OPENCLAW_CHANNELS",
            "OPENCLAW_GATEWAY_TOKEN",
            "OPENCLAW_OPENROUTER_API_KEY",
            "OPENCLAW_NVIDIA_API_KEY",
            "OPENCLAW_PRIMARY_MODEL",
            "OPENCLAW_FALLBACK_MODELS",
            "OPENCLAW_TELEGRAM_BOT_TOKEN",
            "OPENCLAW_TELEGRAM_OWNER_USER_ID",
        ),
        mutable_resource_keys=("OPENCLAW_REPLICAS",),
        enabled_features=(),
        resource_profile="recommended",
    ),
    PackDefinition(
        name="my-farm-advisor",
        prompt_label="My Farm Advisor",
        env_flag="ENABLE_MY_FARM_ADVISOR",
        default_enabled=False,
        depends_on=("headscale",),
        slot=None,
        shared_core_requirements=("postgres",),
        hostnames=(
            PackHostname(
                key="my-farm-advisor",
                default_subdomain="farm",
                env_key="MY_FARM_ADVISOR_SUBDOMAIN",
            ),
        ),
        mutable_env_keys=(
            "MY_FARM_ADVISOR_CHANNELS",
            "MY_FARM_ADVISOR_OPENROUTER_API_KEY",
            "MY_FARM_ADVISOR_NVIDIA_API_KEY",
            "MY_FARM_ADVISOR_PRIMARY_MODEL",
            "MY_FARM_ADVISOR_FALLBACK_MODELS",
            "MY_FARM_ADVISOR_TELEGRAM_BOT_TOKEN",
            "MY_FARM_ADVISOR_TELEGRAM_OWNER_USER_ID",
        ),
        mutable_resource_keys=("MY_FARM_ADVISOR_REPLICAS",),
        enabled_features=(),
        resource_profile="recommended",
    ),
)

_PACKS_BY_NAME = {pack.name: pack for pack in _PACK_CATALOG}


def iter_pack_catalog() -> tuple[PackDefinition, ...]:
    return _PACK_CATALOG


def get_pack_definition(name: str) -> PackDefinition:
    try:
        return _PACKS_BY_NAME[name]
    except KeyError as error:
        known_packs = ", ".join(sorted(_PACKS_BY_NAME))
        raise ValueError(f"Unknown pack '{name}'. Known packs: {known_packs}.") from error


def get_known_pack_names() -> tuple[str, ...]:
    return tuple(sorted(_PACKS_BY_NAME))


def get_mutable_pack_env_keys() -> tuple[str, ...]:
    keys = {key for pack in _PACK_CATALOG for key in pack.mutable_env_keys}
    return tuple(sorted(keys))


def get_mutable_pack_resource_keys() -> tuple[str, ...]:
    keys = {key for pack in _PACK_CATALOG for key in pack.mutable_resource_keys}
    return tuple(sorted(keys))
