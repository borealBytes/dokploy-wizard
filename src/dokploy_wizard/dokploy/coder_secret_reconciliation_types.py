from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal, Protocol

from dokploy_wizard.dokploy.coder_secret_receipts import (
    CoderSecretReceipt,
    CoderSecretReceiptStep,
    SecretOperation,
    metadata_sha256,
)
from dokploy_wizard.state.sync_schema import JsonValue

CoderSecretFailureKind = Literal[
    "blocked",
    "client",
    "metadata",
    "receipt",
    "receipt_invalid",
    "receipt_owner",
    "receipt_read",
    "receipt_read_directory",
    "receipt_read_file",
    "receipt_read_json",
    "receipt_read_schema",
    "receipt_read_schema_fields",
    "receipt_schema",
    "receipt_write",
    "reconciliation",
    "unknown",
]


@dataclass(slots=True)
class CoderSecretError(RuntimeError):
    reason: str
    kind: CoderSecretFailureKind = "unknown"

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class CoderSecretMetadata:
    secret_id: str
    name: str
    env_name: str
    description: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "secret_id": self.secret_id,
            "name": self.name,
            "env_name": self.env_name,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class CoderSecretSpec:
    name: str
    env_name: str
    value: str
    description: str


class CoderSecretClient(Protocol):
    def list_secrets(self) -> tuple[CoderSecretMetadata, ...]: ...

    def write_secret(self, operation: str, spec: CoderSecretSpec) -> str: ...

    def verify_workspace_value_hash(self, spec: CoderSecretSpec, owner_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class MetadataPair:
    first: CoderSecretMetadata | None
    second: CoderSecretMetadata | None

    @property
    def first_sha256(self) -> str:
        return metadata_hash(self.first)

    @property
    def second_sha256(self) -> str:
        return metadata_hash(self.second)

    @property
    def stable(self) -> bool:
        return self.first_sha256 == self.second_sha256


def metadata_hash(metadata: CoderSecretMetadata | None) -> str:
    payload: dict[str, JsonValue] = {"exists": False} if metadata is None else metadata.to_dict()
    return metadata_sha256(payload)


def expected_metadata_hash(secret_id: str | None, spec: CoderSecretSpec) -> str:
    return metadata_sha256(
        {
            "secret_id": secret_id,
            "name": spec.name,
            "env_name": spec.env_name,
            "description": spec.description,
        }
    )


def metadata_matches(metadata: CoderSecretMetadata, spec: CoderSecretSpec) -> bool:
    return (
        metadata.name == spec.name
        and metadata.env_name == spec.env_name
        and metadata.description == spec.description
    )


def operation_for_metadata(
    previous: CoderSecretReceiptStep | None, metadata: CoderSecretMetadata | None
) -> SecretOperation:
    if metadata is None:
        if previous is not None:
            raise CoderSecretError(
                "Coder secret metadata drift blocks recreation",
                kind="metadata",
            )
        return "create"
    return "update"


def current_step(receipt: CoderSecretReceipt, name: str) -> CoderSecretReceiptStep | None:
    return next((step for step in receipt.steps if step.secret_name == name), None)


def replace_step(receipt: CoderSecretReceipt, step: CoderSecretReceiptStep) -> CoderSecretReceipt:
    remaining = tuple(
        current for current in receipt.steps if current.secret_name != step.secret_name
    )
    steps = tuple(sorted((*remaining, step), key=_step_name))
    return replace(receipt, status="running", steps=steps)


def new_intent(
    spec: CoderSecretSpec,
    operation: SecretOperation,
    pair: MetadataPair,
) -> CoderSecretReceiptStep:
    secret_id = None if pair.first is None else pair.first.secret_id
    expected_id = None if operation == "create" else secret_id
    return CoderSecretReceiptStep(
        secret_name=spec.name,
        secret_id=secret_id,
        env_name=spec.env_name,
        description=spec.description,
        operation=operation,
        status="intent",
        pre_metadata_sha256=pair.first_sha256,
        second_pre_metadata_sha256=pair.second_sha256,
        source_value_sha256=value_hash(spec.value),
        expected_post_sha256=expected_metadata_hash(expected_id, spec),
        response_sha256=None,
        workspace_verification_sha256=None,
        updated_at=now(),
    )


def validate_receipt_for_specs(
    receipt: CoderSecretReceipt, specs: tuple[CoderSecretSpec, ...]
) -> None:
    by_name = {spec.name: spec for spec in specs}
    receipt_names = tuple(step.secret_name for step in receipt.steps)
    if any(name not in by_name for name in receipt_names):
        raise CoderSecretError(
            "Coder secret receipt contains an unknown managed secret",
            kind="receipt_schema",
        )
    if receipt.status == "completed" and receipt_names != tuple(sorted(by_name)):
        raise CoderSecretError(
            "Coder secret completed receipt inventory is incomplete",
            kind="receipt_schema",
        )
    for step in receipt.steps:
        spec = by_name[step.secret_name]
        if step.env_name != spec.env_name or step.description != spec.description:
            raise CoderSecretError(
                "Coder secret receipt metadata does not match the requested secret",
                kind="receipt_schema",
            )
        expected_id = None if step.operation == "create" else step.secret_id
        if step.expected_post_sha256 != expected_metadata_hash(expected_id, spec):
            raise CoderSecretError(
                "Coder secret receipt expected metadata is invalid",
                kind="receipt_schema",
            )
        if step.status != "verified" and step.source_value_sha256 != value_hash(spec.value):
            raise CoderSecretError(
                "Coder secret receipt source value does not match the requested secret",
                kind="receipt_schema",
            )


def value_hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def valid_hash(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _step_name(step: CoderSecretReceiptStep) -> str:
    return step.secret_name
