"""Strict request and create-intent schemas for the external sync helper."""

from __future__ import annotations

from dataclasses import dataclass

from dokploy_wizard.dokploy.sync_helper_create import CreateIntent as CreateIntent
from dokploy_wizard.state.sync_schema import (
    JsonValue,
    SyncStateError,
    canonical_digest,
    require_digest,
    require_exact_keys,
    require_string,
)


@dataclass(frozen=True, slots=True)
class LeaseRequest:
    lease: str
    generation: int
    receipt_version: int
    mode: str
    parent_pid: int
    parent_start_time_ticks: int
    parent_argv_sha256: str
    env: tuple[tuple[str, str], ...]
    input_sha256: str
    config_sha256: str
    expected_state_sha256: str
    tombstone_sha256: str | None
    created_at: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.lease == "":
            raise SyncStateError("Lease request identity is invalid.")
        if self.generation < 1 or self.receipt_version < 1:
            raise SyncStateError("Lease request generation is invalid.")
        if self.mode not in {"reconcile", "disable"}:
            raise SyncStateError("Lease request mode is invalid.")
        if self.parent_pid < 1 or self.parent_start_time_ticks < 0:
            raise SyncStateError("Lease request parent identity is invalid.")
        for value, field_name in (
            (self.parent_argv_sha256, "parent_argv_sha256"),
            (self.input_sha256, "input_sha256"),
            (self.config_sha256, "config_sha256"),
            (self.expected_state_sha256, "expected_state_sha256"),
        ):
            require_digest(value, field_name)
        if self.tombstone_sha256 is not None:
            require_digest(self.tombstone_sha256, "tombstone_sha256")
        if tuple(sorted(self.env)) != self.env or len(dict(self.env)) != len(self.env):
            raise SyncStateError("Lease request env must be sorted and unique.")
        for name, value_sha256 in self.env:
            if name == "":
                raise SyncStateError("Lease request env names must be non-empty.")
            require_digest(value_sha256, "env value_sha256")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "config_sha256": self.config_sha256,
            "created_at": self.created_at,
            "env": [
                {"name": name, "value_sha256": value_sha256}
                for name, value_sha256 in self.env
            ],
            "expected_state_sha256": self.expected_state_sha256,
            "generation": self.generation,
            "input_sha256": self.input_sha256,
            "lease": self.lease,
            "mode": self.mode,
            "parent_argv_sha256": self.parent_argv_sha256,
            "parent_pid": self.parent_pid,
            "parent_start_time_ticks": self.parent_start_time_ticks,
            "receipt_version": self.receipt_version,
            "schema_version": self.schema_version,
            "tombstone_sha256": self.tombstone_sha256,
        }

    def sha256(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, JsonValue]) -> LeaseRequest:
        require_exact_keys(payload, frozenset(cls.__dataclass_fields__), "lease request")
        env = payload["env"]
        if not isinstance(env, list):
            raise SyncStateError("Lease request env must be a list.")
        parsed_env: list[tuple[str, str]] = []
        for item in env:
            if not isinstance(item, dict):
                raise SyncStateError("Lease request env entries must be objects.")
            require_exact_keys(item, frozenset({"name", "value_sha256"}), "lease env")
            parsed_env.append((require_string(item, "name"), require_string(item, "value_sha256")))
        return cls(
            lease=require_string(payload, "lease"),
            generation=_int(payload, "generation"),
            receipt_version=_int(payload, "receipt_version"),
            mode=require_string(payload, "mode"),
            parent_pid=_int(payload, "parent_pid"),
            parent_start_time_ticks=_int(payload, "parent_start_time_ticks"),
            parent_argv_sha256=require_string(payload, "parent_argv_sha256"),
            env=tuple(parsed_env),
            input_sha256=require_string(payload, "input_sha256"),
            config_sha256=require_string(payload, "config_sha256"),
            expected_state_sha256=require_string(payload, "expected_state_sha256"),
            tombstone_sha256=_optional(payload, "tombstone_sha256"),
            created_at=require_string(payload, "created_at"),
            schema_version=_int(payload, "schema_version"),
        )

def _int(payload: dict[str, JsonValue], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise SyncStateError(f"{key} must be an integer.")
    return value


def _optional(payload: dict[str, JsonValue], key: str) -> str | None:
    value = payload[key]
    if value is not None and (not isinstance(value, str) or value == ""):
        raise SyncStateError(f"{key} must be a non-empty string or null.")
    return value
