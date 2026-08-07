from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.coder_secret_receipts import (
    CoderSecretReceipt,
    CoderSecretReceiptStep,
    CoderSecretReceiptStore,
    ReceiptStatus,
    SecretOperation,
    StepStatus,
    metadata_sha256,
)
from dokploy_wizard.dokploy.coder_secret_reconciliation import (
    CoderSecretError,
    CoderSecretMetadata,
    CoderSecretReconciler,
    CoderSecretSpec,
)
from dokploy_wizard.dokploy.coder_secret_specs import build_coder_secret_specs


@dataclass
class FakeCoderSecrets:
    secrets: dict[str, CoderSecretMetadata] = field(default_factory=dict)
    values: dict[str, str] = field(default_factory=dict)
    writes: list[tuple[str, str]] = field(default_factory=list)
    race_before_write: bool = False
    unsupported_env: bool = False

    def list_secrets(self) -> tuple[CoderSecretMetadata, ...]:
        return tuple(self.secrets.values())

    def write_secret(self, operation: str, spec: CoderSecretSpec) -> str:
        if self.unsupported_env:
            raise CoderSecretError("secret CLI does not support environment binding")
        if self.race_before_write:
            self.secrets["third-party"] = CoderSecretMetadata(
                secret_id="third-party-id",
                name="third-party",
                env_name="THIRD_PARTY",
                description="external",
            )
        self.writes.append((operation, spec.name))
        secret_id = self.secrets.get(
            spec.name,
            CoderSecretMetadata("secret-1", spec.name, spec.env_name, spec.description),
        ).secret_id
        self.secrets[spec.name] = CoderSecretMetadata(
            secret_id=secret_id,
            name=spec.name,
            env_name=spec.env_name,
            description=spec.description,
        )
        self.values[spec.name] = spec.value
        return sha256(operation.encode()).hexdigest()

    def verify_workspace_value_hash(self, spec: CoderSecretSpec, owner_id: str) -> str:
        del owner_id
        return sha256(self.values[spec.name].encode()).hexdigest()


@dataclass
class SequencedMetadataCoderSecrets(FakeCoderSecrets):
    snapshots: list[CoderSecretMetadata | None] = field(default_factory=list)

    def list_secrets(self) -> tuple[CoderSecretMetadata, ...]:
        if self.snapshots:
            snapshot = self.snapshots.pop(0)
            return () if snapshot is None else (snapshot,)
        return super().list_secrets()


def _specs() -> tuple[CoderSecretSpec, ...]:
    return (
        CoderSecretSpec(
            "hermes-inference-provider", "HERMES_INFERENCE_PROVIDER", "openai", "provider"
        ),
        CoderSecretSpec(
            "hermes-model", "HERMES_MODEL", "opencode-go/deepseek-v4-flash", "model"
        ),
        CoderSecretSpec(
            "hermes-openai-api-base",
            "OPENAI_API_BASE",
            "http://wizard-shared-litellm:4000/v1",
            "base",
        ),
        CoderSecretSpec(
            "hermes-openai-api-key", "OPENAI_API_KEY", "SECRET-HERMES", "hermes key"
        ),
        CoderSecretSpec(
            "kdense-litellm-api-key",
            "KDENSE_LITELLM_API_KEY",
            "SECRET-KDENSE",
            "kdense key",
        ),
    )


def _metadata_hash(metadata: CoderSecretMetadata | None) -> str:
    return metadata_sha256({"exists": False} if metadata is None else metadata.to_dict())


def _receipt_step(
    spec: CoderSecretSpec,
    *,
    operation: SecretOperation,
    status: StepStatus,
    metadata: CoderSecretMetadata | None,
    response_sha256: str | None = None,
    workspace_verification_sha256: str | None = None,
) -> CoderSecretReceiptStep:
    secret_id = None if metadata is None else metadata.secret_id
    return CoderSecretReceiptStep(
        secret_name=spec.name,
        secret_id=secret_id,
        env_name=spec.env_name,
        description=spec.description,
        operation=operation,
        status=status,
        pre_metadata_sha256=_metadata_hash(metadata),
        second_pre_metadata_sha256=_metadata_hash(metadata),
        source_value_sha256=sha256(spec.value.encode()).hexdigest(),
        expected_post_sha256=metadata_sha256(
            {
                "secret_id": secret_id,
                "name": spec.name,
                "env_name": spec.env_name,
                "description": spec.description,
            }
        ),
        response_sha256=response_sha256,
        workspace_verification_sha256=workspace_verification_sha256,
        updated_at="2026-07-30T00:00:00Z",
    )


def _write_receipt(
    tmp_path: Path,
    step: CoderSecretReceiptStep,
    *,
    status: ReceiptStatus = "running",
) -> None:
    CoderSecretReceiptStore(tmp_path).write(
        CoderSecretReceipt(owner_id="d" * 64, status=status, steps=(step,))
    )


def test_secret_create_receipt_uses_five_stdin_values_and_redacts_receipt(
    tmp_path: Path,
) -> None:
    client = FakeCoderSecrets()
    reconciler = CoderSecretReconciler(state_dir=tmp_path, client=client, owner_id="a" * 64)

    receipt = reconciler.reconcile(_specs())

    assert client.writes == [("create", spec.name) for spec in _specs()]
    assert tuple(step.secret_name for step in receipt.steps) == tuple(
        sorted(spec.name for spec in _specs())
    )
    assert all(step.status == "verified" for step in receipt.steps)
    rendered = json.dumps(receipt.to_dict(), sort_keys=True)
    assert "SECRET-HERMES" not in rendered
    assert "SECRET-KDENSE" not in rendered


def test_unowned_secret_collision_blocks_before_write(tmp_path: Path) -> None:
    spec = _specs()[0]
    client = FakeCoderSecrets(
        secrets={
            spec.name: CoderSecretMetadata(
                secret_id="foreign-secret",
                name=spec.name,
                env_name=spec.env_name,
                description=spec.description,
            )
        }
    )
    reconciler = CoderSecretReconciler(state_dir=tmp_path, client=client, owner_id="b" * 64)

    with pytest.raises(CoderSecretError, match="ownership") as raised:
        reconciler.reconcile((spec,))

    assert raised.value.kind == "blocked"
    assert client.writes == []


def test_secret_receipt_owner_mismatch_has_typed_invalid_receipt_failure(
    tmp_path: Path,
) -> None:
    CoderSecretReceiptStore(tmp_path).write(CoderSecretReceipt("a" * 64, "planned", ()))
    reconciler = CoderSecretReconciler(
        state_dir=tmp_path,
        client=FakeCoderSecrets(),
        owner_id="b" * 64,
    )

    with pytest.raises(CoderSecretError) as raised:
        reconciler.reconcile(_specs())

    assert raised.value.kind == "receipt_owner"


@pytest.mark.parametrize(
    ("payload", "expected_kind"),
    (
        (b"not-json", "receipt_read_json"),
        (b'{"schema_version":2}', "receipt_read_schema"),
    ),
)
def test_secret_receipt_load_failures_preserve_typed_origin(
    tmp_path: Path,
    payload: bytes,
    expected_kind: str,
) -> None:
    receipt_path = tmp_path / "coder-secret-receipts-v1.json"
    receipt_path.write_bytes(payload)
    receipt_path.chmod(0o600)
    reconciler = CoderSecretReconciler(
        state_dir=tmp_path,
        client=FakeCoderSecrets(),
        owner_id="b" * 64,
    )

    with pytest.raises(CoderSecretError) as raised:
        reconciler.reconcile(_specs())

    assert raised.value.kind == expected_kind


def test_workspace_secret_unsupported_blocks_without_value_leak(tmp_path: Path) -> None:
    spec = _specs()[0]
    client = FakeCoderSecrets(unsupported_env=True)
    reconciler = CoderSecretReconciler(state_dir=tmp_path, client=client, owner_id="c" * 64)

    with pytest.raises(CoderSecretError) as raised:
        reconciler.reconcile((spec,))

    assert spec.value not in str(raised.value)
    assert client.writes == []


def test_terminal_secret_receipt_has_typed_receipt_failure(tmp_path: Path) -> None:
    spec = _specs()[0]
    client = FakeCoderSecrets()
    owner_id = "d" * 64
    _write_receipt(
        tmp_path,
        _receipt_step(spec, operation="create", status="blocked", metadata=None),
        status="failed",
    )
    reconciler = CoderSecretReconciler(
        state_dir=tmp_path,
        client=client,
        owner_id=owner_id,
    )

    with pytest.raises(CoderSecretError) as raised:
        reconciler.reconcile((spec,))

    assert raised.value.kind == "receipt"
    assert client.writes == []


def test_exact_five_secret_specs_reject_blank_coder_key_before_mutation() -> None:
    with pytest.raises(ValueError, match="values are invalid"):
        build_coder_secret_specs(
            stack_name="wizard",
            default_alias="opencode-go/default",
            visible_aliases=("opencode-go/default",),
            coder_hermes_key="key-a",
            coder_kdense_key="",
        )


def test_secret_metadata_race_blocks_before_third_snapshot_can_reach_mutation(
    tmp_path: Path,
) -> None:
    spec = _specs()[0]
    client = SequencedMetadataCoderSecrets(
        snapshots=(
            [
                None,
                None,
                CoderSecretMetadata(
                    secret_id="external-secret",
                    name=spec.name,
                    env_name=spec.env_name,
                    description=spec.description,
                ),
            ]
        )
    )
    reconciler = CoderSecretReconciler(state_dir=tmp_path, client=client, owner_id="d" * 64)

    with pytest.raises(CoderSecretError, match="metadata drift"):
        reconciler.reconcile((spec,))

    assert client.writes == []


def test_post_write_metadata_drift_blocks_without_rollback(tmp_path: Path) -> None:
    spec = _specs()[0]

    class PostWriteDriftCoderSecrets(FakeCoderSecrets):
        def write_secret(self, operation: str, candidate: CoderSecretSpec) -> str:
            response_sha256 = super().write_secret(operation, candidate)
            current = self.secrets[candidate.name]
            self.secrets[candidate.name] = CoderSecretMetadata(
                secret_id=current.secret_id,
                name=current.name,
                env_name=current.env_name,
                description="externally altered metadata",
            )
            return response_sha256

    client = PostWriteDriftCoderSecrets()
    reconciler = CoderSecretReconciler(state_dir=tmp_path, client=client, owner_id="d" * 64)

    with pytest.raises(CoderSecretError, match="metadata drift"):
        reconciler.reconcile((spec,))

    assert client.writes == [("create", spec.name)]
    blocked = CoderSecretReceiptStore(tmp_path).load()
    assert blocked is not None
    assert blocked.status == "blocked"


def test_create_intent_recovery_binds_the_observed_created_id_without_replay(
    tmp_path: Path,
) -> None:
    spec = _specs()[0]
    created = CoderSecretMetadata(
        secret_id="created-secret",
        name=spec.name,
        env_name=spec.env_name,
        description=spec.description,
    )
    intent = _receipt_step(spec, operation="create", status="intent", metadata=None)
    _write_receipt(tmp_path, intent)
    client = FakeCoderSecrets(secrets={spec.name: created}, values={spec.name: spec.value})
    reconciler = CoderSecretReconciler(state_dir=tmp_path, client=client, owner_id="d" * 64)

    receipt = reconciler.reconcile((spec,))

    assert client.writes == []
    assert receipt.steps[0].operation == "create"
    assert receipt.steps[0].secret_id == created.secret_id
    assert receipt.steps[0].status == "verified"


def test_secret_update_crash_blocks_the_ambiguous_intent_without_replaying_write(
    tmp_path: Path,
) -> None:
    spec = _specs()[0]
    current = CoderSecretMetadata(
        secret_id="owned-secret",
        name=spec.name,
        env_name=spec.env_name,
        description=spec.description,
    )
    intent = _receipt_step(spec, operation="update", status="intent", metadata=current)
    _write_receipt(tmp_path, intent)
    client = FakeCoderSecrets(secrets={spec.name: current}, values={spec.name: spec.value})
    reconciler = CoderSecretReconciler(state_dir=tmp_path, client=client, owner_id="d" * 64)

    with pytest.raises(CoderSecretError, match="ambiguous"):
        reconciler.reconcile((spec,))

    assert client.writes == []
    blocked = CoderSecretReceiptStore(tmp_path).load()
    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked.steps[0].status == "blocked"


def test_submitted_update_recovery_verifies_without_a_second_write(tmp_path: Path) -> None:
    spec = _specs()[0]
    current = CoderSecretMetadata(
        secret_id="owned-secret",
        name=spec.name,
        env_name=spec.env_name,
        description=spec.description,
    )
    submitted = _receipt_step(
        spec,
        operation="update",
        status="submitted",
        metadata=current,
        response_sha256="a" * 64,
    )
    _write_receipt(tmp_path, submitted)
    client = FakeCoderSecrets(secrets={spec.name: current}, values={spec.name: spec.value})
    reconciler = CoderSecretReconciler(state_dir=tmp_path, client=client, owner_id="d" * 64)

    receipt = reconciler.reconcile((spec,))

    assert client.writes == []
    assert receipt.steps[0].operation == "update"
    assert receipt.steps[0].status == "verified"


def test_completed_receipt_is_byte_stable_on_an_unchanged_rerun(tmp_path: Path) -> None:
    spec = _specs()[0]
    client = FakeCoderSecrets()
    reconciler = CoderSecretReconciler(state_dir=tmp_path, client=client, owner_id="e" * 64)
    reconciler.reconcile((spec,))
    receipt_path = tmp_path / "coder-secret-receipts-v1.json"
    first = receipt_path.read_bytes()

    reconciler.reconcile((spec,))

    assert client.writes == [("create", spec.name)]
    assert receipt_path.read_bytes() == first
