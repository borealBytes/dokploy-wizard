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
        default_enabled=False,
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
        depends_on=(),
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
        depends_on=(),
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
        name="moodle",
        prompt_label="Moodle",
        env_flag="ENABLE_MOODLE",
        default_enabled=False,
        depends_on=(),
        slot=None,
        shared_core_requirements=("postgres",),
        hostnames=(
            PackHostname(
                key="moodle",
                default_subdomain="moodle",
                env_key="MOODLE_SUBDOMAIN",
            ),
        ),
        mutable_env_keys=("OUTBOUND_SMTP_HOSTNAME", "OUTBOUND_SMTP_FROM_ADDRESS"),
        mutable_resource_keys=(),
        enabled_features=(),
        resource_profile="recommended",
    ),
    PackDefinition(
        name="docuseal",
        prompt_label="DocuSeal",
        env_flag="ENABLE_DOCUSEAL",
        default_enabled=False,
        depends_on=(),
        slot=None,
        shared_core_requirements=("postgres",),
        hostnames=(
            PackHostname(
                key="docuseal",
                default_subdomain="docuseal",
                env_key="DOCUSEAL_SUBDOMAIN",
            ),
        ),
        mutable_env_keys=("OUTBOUND_SMTP_HOSTNAME", "OUTBOUND_SMTP_FROM_ADDRESS"),
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
        # The S3 endpoint is deliberately not exposed on a public hostname: the
        # gateway is anonymous-by-default (no s3.json on this deployment) and a
        # bare open bucket on the internet invites abuse. SeaweedFS serves S3 on
        # the internal host '<stack>-s3.internal' only; a re-exposure needs an
        # identity layer first (see the plan doc).
        hostnames=(),
        mutable_env_keys=(),
        mutable_resource_keys=(),
        enabled_features=(),
        resource_profile="recommended",
    ),
    PackDefinition(
        name="filex",
        prompt_label="FileX",
        env_flag="ENABLE_FILEX",
        default_enabled=False,
        depends_on=("seaweedfs",),
        slot=None,
        shared_core_requirements=(),
        hostnames=(
            PackHostname(
                key="filex",
                default_subdomain="filex",
                env_key="FILEX_SUBDOMAIN",
            ),
        ),
        mutable_env_keys=(),
        mutable_resource_keys=(),
        enabled_features=(),
        resource_profile="recommended",
    ),
    PackDefinition(
        name="gitea",
        prompt_label="Gitea",
        env_flag="ENABLE_GITEA",
        default_enabled=False,
        depends_on=("seaweedfs",),
        slot=None,
        shared_core_requirements=("postgres",),
        hostnames=(
            PackHostname(
                key="gitea",
                default_subdomain="gitea",
                env_key="GITEA_SUBDOMAIN",
            ),
        ),
        mutable_env_keys=(),
        mutable_resource_keys=(),
        enabled_features=(),
        # Rootless image, repos on the data volume, LFS/attachments in the
        # SeaweedFS bucket, shared-core Postgres. Built-in SSH listens on 2222
        # inside the overlay network only (git-over-HTTPS rides the public
        # route; publishing 2222 is an operator choice, not a wizard default).
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
                default_subdomain="*",
                env_key="CODER_WILDCARD_SUBDOMAIN",
            ),
        ),
        mutable_env_keys=(
            "HERMES_INFERENCE_PROVIDER",
            "HERMES_MODEL",
            # Which Hermes build the Desktop workspace installs; the browser-hosted
            # Desktop the tile serves exists only on the newer channel.
            "HERMES_DESKTOP_CHANNEL",
            "AI_DEFAULT_API_KEY",
            "AI_DEFAULT_BASE_URL",
            "AI_DEFAULT_PROVIDER",
            "AI_DEFAULT_MODEL",
            # The pattern drives the sidecar's sync/default-chat selection, so it is
            # operator-tunable on the same footing as AI_DEFAULT_MODEL itself.
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
        ),
        mutable_resource_keys=(),
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
