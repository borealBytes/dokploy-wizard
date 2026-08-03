"""Strict schema for the Shared Core state-upgrade intent."""

from __future__ import annotations

from dataclasses import dataclass

from dokploy_wizard.state.shared_core_sync import SyncOwner, SyncStateError

BASE_KEYS = (
    "raw_input",
    "desired",
    "applied",
    "ledger",
    "litellm_keys",
    "surfsense_secrets",
    "seaweedfs_secrets",
)
ALL_KEYS = BASE_KEYS + ("owner",)
WRITE_ORDER = ("owner", "desired", "applied", "ledger")


class StateUpgradeError(RuntimeError):
    """Raised when an upgrade intent cannot safely start or resume."""


@dataclass(frozen=True, slots=True)
class StateUpgradeIntent:
    generation: int
    cas_token: str
    status: str
    owner_id: str
    pre_hashes: dict[str, str]
    post_hashes: dict[str, str]
    completed_writes: tuple[str, ...]
    created_at: str
    updated_at: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.generation < 1 or len(self.cas_token) != 64:
            raise StateUpgradeError("State upgrade generation, token, or schema is invalid.")
        try:
            SyncOwner(owner_id=self.owner_id)
        except SyncStateError as error:
            raise StateUpgradeError(str(error)) from error
        if set(self.pre_hashes) != set(ALL_KEYS) or set(self.post_hashes) != set(ALL_KEYS):
            raise StateUpgradeError("State upgrade hash maps must contain exact managed paths.")
        if self.status not in {"planned", "writing", "complete"}:
            raise StateUpgradeError("State upgrade status is invalid.")
        if self.completed_writes != WRITE_ORDER[: len(self.completed_writes)]:
            raise StateUpgradeError("State upgrade writes are not in canonical order.")
        if self.status == "planned" and self.completed_writes:
            raise StateUpgradeError("Planned state upgrades cannot contain completed writes.")
        if self.status == "complete" and self.completed_writes != WRITE_ORDER:
            raise StateUpgradeError("Complete state upgrades require every canonical write.")

    def to_dict(self) -> dict[str, object]:
        return {
            "cas_token": self.cas_token,
            "completed_writes": list(self.completed_writes),
            "created_at": self.created_at,
            "generation": self.generation,
            "owner_id": self.owner_id,
            "post_hashes": dict(sorted(self.post_hashes.items())),
            "pre_hashes": dict(sorted(self.pre_hashes.items())),
            "schema_version": self.schema_version,
            "status": self.status,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> StateUpgradeIntent:
        if set(payload) != set(cls.__dataclass_fields__) or payload.get("schema_version") != 1:
            raise StateUpgradeError("State upgrade intent has an unsupported schema.")
        completed = payload["completed_writes"]
        if not isinstance(completed, list) or not all(isinstance(item, str) for item in completed):
            raise StateUpgradeError("State upgrade completed_writes is malformed.")
        return cls(
            generation=_int(payload, "generation"),
            cas_token=_text(payload, "cas_token"),
            status=_text(payload, "status"),
            owner_id=_text(payload, "owner_id"),
            pre_hashes=_hashes(payload["pre_hashes"]),
            post_hashes=_hashes(payload["post_hashes"]),
            completed_writes=tuple(completed),
            created_at=_text(payload, "created_at"),
            updated_at=_text(payload, "updated_at"),
            schema_version=_int(payload, "schema_version"),
        )


def _hashes(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(ALL_KEYS):
        raise StateUpgradeError("State upgrade hash map is malformed.")
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise StateUpgradeError("State upgrade hash map is malformed.")
    return dict(value)


def _text(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or value == "":
        raise StateUpgradeError(f"State upgrade {key} is malformed.")
    return value


def _int(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise StateUpgradeError(f"State upgrade {key} is malformed.")
    return value
