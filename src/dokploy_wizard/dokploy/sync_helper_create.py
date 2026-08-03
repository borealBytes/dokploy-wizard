"""Crash-safe create-intent binding for the external sync helper container."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable

from dokploy_wizard.state.sync_schema import (
    JsonValue,
    SyncOwner,
    SyncStateError,
    require_digest,
    require_exact_keys,
    require_string,
)

if TYPE_CHECKING:
    from dokploy_wizard.dokploy.sync_helper_schema import LeaseRequest


@dataclass(frozen=True, slots=True)
class CreateIntent:
    lease: str
    name: str
    stack: str
    owner: str
    labels: tuple[tuple[str, str], ...]
    image_digest: str
    network: str
    volume_fingerprint: str
    command_sha256: str
    request_sha256: str
    mode: str
    container_id: str | None
    phase: str = "create_intent"
    receipt_version: int = 1
    schema_version: int = 1

    @classmethod
    def for_request(
        cls,
        *,
        request: LeaseRequest,
        stack: str,
        owner: str,
        image_digest: str,
        network: str,
        volume_fingerprint: str,
        command_sha256: str,
    ) -> CreateIntent:
        return cls(
            lease=request.lease,
            name=f"{stack}-opencode-go-lock-{request.lease[:8]}",
            stack=stack,
            owner=owner,
            labels=_labels(request.lease, owner, stack),
            image_digest=image_digest,
            network=network,
            volume_fingerprint=volume_fingerprint,
            command_sha256=command_sha256,
            request_sha256=request.sha256(),
            mode=request.mode,
            container_id=None,
        )

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.receipt_version < 1:
            raise SyncStateError("Create intent version is invalid.")
        if self.phase not in {"create_intent", "created"} or self.stack == "":
            raise SyncStateError("Create intent shape is invalid.")
        SyncOwner(owner_id=self.owner)
        if self.labels != _labels(self.lease, self.owner, self.stack):
            raise SyncStateError("Create intent labels do not bind the exact owner and stack.")
        if self.name != f"{self.stack}-opencode-go-lock-{self.lease[:8]}":
            raise SyncStateError("Create intent name does not bind the lease.")
        if (self.phase == "create_intent") != (self.container_id is None):
            raise SyncStateError("Create intent phase and container id disagree.")
        _require_pinned_image(self.image_digest)
        for value, field_name in (
            (self.volume_fingerprint, "volume_fingerprint"),
            (self.command_sha256, "command_sha256"),
            (self.request_sha256, "request_sha256"),
        ):
            require_digest(value, field_name)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "command_sha256": self.command_sha256,
            "container_id": self.container_id,
            "image_digest": self.image_digest,
            "labels": [{"name": name, "value": value} for name, value in self.labels],
            "lease": self.lease,
            "mode": self.mode,
            "name": self.name,
            "network": self.network,
            "owner": self.owner,
            "phase": self.phase,
            "receipt_version": self.receipt_version,
            "request_sha256": self.request_sha256,
            "schema_version": self.schema_version,
            "stack": self.stack,
            "volume_fingerprint": self.volume_fingerprint,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, JsonValue]) -> CreateIntent:
        require_exact_keys(payload, frozenset(cls.__dataclass_fields__), "create intent")
        labels = payload["labels"]
        if not isinstance(labels, list):
            raise SyncStateError("Create intent labels must be a list.")
        parsed_labels: list[tuple[str, str]] = []
        for item in labels:
            if not isinstance(item, dict):
                raise SyncStateError("Create intent labels must be objects.")
            require_exact_keys(item, frozenset({"name", "value"}), "create intent label")
            parsed_labels.append((require_string(item, "name"), require_string(item, "value")))
        container_id = payload["container_id"]
        if container_id is not None and not isinstance(container_id, str):
            raise SyncStateError("Create intent container_id must be a string or null.")
        return cls(
            lease=require_string(payload, "lease"),
            name=require_string(payload, "name"),
            stack=require_string(payload, "stack"),
            owner=require_string(payload, "owner"),
            labels=tuple(parsed_labels),
            image_digest=require_string(payload, "image_digest"),
            network=require_string(payload, "network"),
            volume_fingerprint=require_string(payload, "volume_fingerprint"),
            command_sha256=require_string(payload, "command_sha256"),
            request_sha256=require_string(payload, "request_sha256"),
            mode=require_string(payload, "mode"),
            container_id=container_id,
            phase=require_string(payload, "phase"),
            receipt_version=_int(payload, "receipt_version"),
            schema_version=_int(payload, "schema_version"),
        )


@dataclass(frozen=True, slots=True)
class HelperContainerObservation:
    container_id: str
    name: str
    labels: tuple[tuple[str, str], ...]
    image_digest: str
    network: str
    volume_fingerprint: str
    command_sha256: str


def bind_created_container(
    intent: CreateIntent,
    *,
    matches: tuple[HelperContainerObservation, ...],
    create_container: Callable[[], HelperContainerObservation],
    persist_created: Callable[[CreateIntent], None],
) -> CreateIntent:
    """Bind one exact container and durably checkpoint its full ID before start."""

    if len(matches) > 1:
        raise SyncStateError("Multiple helper containers match the deterministic create intent.")
    if intent.phase == "created":
        if len(matches) != 1 or matches[0].container_id != intent.container_id:
            raise SyncStateError("Created helper intent cannot recover its exact container.")
        _require_exact_container(intent, matches[0])
        return intent
    observation = matches[0] if matches else create_container()
    _require_exact_container(intent, observation)
    created = replace(
        intent,
        phase="created",
        container_id=observation.container_id,
        receipt_version=intent.receipt_version + 1,
    )
    persist_created(created)
    return created


def _require_exact_container(intent: CreateIntent, observed: HelperContainerObservation) -> None:
    invalid_id = observed.container_id == "" or any(
        character.isspace() for character in observed.container_id
    )
    if invalid_id:
        raise SyncStateError("Helper container returned an invalid full ID.")
    expected = (
        intent.name,
        intent.labels,
        intent.image_digest,
        intent.network,
        intent.volume_fingerprint,
        intent.command_sha256,
    )
    actual = (
        observed.name,
        observed.labels,
        observed.image_digest,
        observed.network,
        observed.volume_fingerprint,
        observed.command_sha256,
    )
    if actual != expected:
        raise SyncStateError("Helper container does not match the exact create intent.")


def _labels(lease: str, owner: str, stack: str) -> tuple[tuple[str, str], ...]:
    return (
        ("dokploy-wizard.stack", stack),
        ("dokploy-wizard.owner", owner),
        ("dokploy-wizard.lease", lease),
    )


def _require_pinned_image(value: str) -> None:
    repository, marker, digest = value.partition("@sha256:")
    if repository == "" or marker == "" or "@" in repository:
        raise SyncStateError("Create intent image must be digest-pinned.")
    require_digest(digest, "image_digest")


def _int(payload: dict[str, JsonValue], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise SyncStateError(f"{key} must be an integer.")
    return value
