from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from dokploy_wizard.dokploy.coder_secret_receipts import (
    CoderSecretReceipt,
    CoderSecretReceiptStep,
    CoderSecretReceiptStore,
    metadata_sha256,
)
from dokploy_wizard.dokploy.coder_secret_reconciliation import CoderSecretMetadata
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import (
    WorkspaceVerificationPhase,
    WorkspaceVerificationReceipt,
    WorkspaceVerificationReceiptStore,
)
from dokploy_wizard.state import OwnedResource
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore

OWNER_ID = sha256(b"coder-secret:stack:coder.example.test").hexdigest()
UPDATED_AT = "2026-07-31T00:00:00Z"
SECRET_IDENTITIES = (
    (
        "00000000-0000-4000-8000-000000000001",
        "hermes-inference-provider",
        "HERMES_INFERENCE_PROVIDER",
        "Hermes provider",
    ),
    (
        "00000000-0000-4000-8000-000000000002",
        "hermes-model",
        "HERMES_MODEL",
        "Hermes model",
    ),
    (
        "00000000-0000-4000-8000-000000000003",
        "hermes-openai-api-base",
        "OPENAI_API_BASE",
        "Hermes LiteLLM base URL",
    ),
    (
        "00000000-0000-4000-8000-000000000004",
        "hermes-openai-api-key",
        "OPENAI_API_KEY",
        "Hermes LiteLLM key",
    ),
    (
        "00000000-0000-4000-8000-000000000005",
        "kdense-litellm-api-key",
        "KDENSE_LITELLM_API_KEY",
        "K-Dense LiteLLM key",
    ),
)


class SecretDestroyClient:
    def __init__(
        self,
        secrets: list[CoderSecretMetadata],
        failing_secret_id: str | None = None,
        workspace_present: bool = False,
        api_available: bool = True,
    ) -> None:
        self.secrets = secrets
        self.failing_secret_id = failing_secret_id
        self.workspace_present = workspace_present
        self.api_available = api_available
        self.events: list[str] = []

    def list_secrets(self) -> tuple[CoderSecretMetadata, ...]:
        if not self.api_available:
            raise AssertionError("Coder API accessed after service teardown")
        self.events.append("list")
        return tuple(self.secrets)

    def delete_secret(self, secret: CoderSecretMetadata) -> None:
        self.events.append(f"delete:{secret.secret_id}")
        if secret.secret_id == self.failing_secret_id:
            raise SecretDestroyFailure("delete failed")
        self.secrets = [item for item in self.secrets if item.secret_id != secret.secret_id]

    def require_workspace_receipt_absence(self, receipt: WorkspaceVerificationReceipt) -> None:
        if not self.api_available:
            raise AssertionError("Coder API accessed after service teardown")
        del receipt
        self.events.append("workspace-absence")
        if self.workspace_present:
            raise SecretDestroyFailure("workspace remains")


class SecretDestroyFailure(RuntimeError):
    """Synthetic remote mutation failure for destroy recovery tests."""


def metadata(identity: tuple[str, str, str, str]) -> CoderSecretMetadata:
    secret_id, name, env_name, description = identity
    return CoderSecretMetadata(secret_id, name, env_name, description)


def write_completed_secret_receipt(state_dir: Path) -> None:
    CoderSecretReceiptStore(state_dir).write(
        CoderSecretReceipt(
            owner_id=OWNER_ID,
            status="completed",
            steps=tuple(_secret_step(identity) for identity in SECRET_IDENTITIES),
        )
    )


def write_terminal_workspace_receipt(state_dir: Path) -> WorkspaceVerificationReceipt:
    receipt = WorkspaceVerificationReceipt(
        owner_id=OWNER_ID,
        workspace_id="00000000-0000-4000-8000-000000000201",
        workspace_name="proof-workspace",
        workspace_owner_id="00000000-0000-4000-8000-000000000202",
        workspace_owner_name="operator",
        template_id="00000000-0000-4000-8000-000000000203",
        template_name="proof-template",
        env_name="OPENAI_API_KEY",
        expected_value_sha256="a" * 64,
        observed_value_sha256="a" * 64,
        create_attempts=1,
        phase=WorkspaceVerificationPhase.DELETED,
        failure_reason=None,
        created_at=UPDATED_AT,
        updated_at=UPDATED_AT,
    )
    WorkspaceVerificationReceiptStore(state_dir).write(receipt)
    return receipt


def seed_uninstall_authority(state_dir: Path, resources: tuple[OwnedResource, ...]) -> None:
    store = UninstallAuthorityStore(state_dir)
    for resource in resources:
        identity = "\0".join((resource.resource_type, resource.resource_id, resource.scope))
        store.record_created(
            resource=resource,
            owner_id=OWNER_ID,
            provider="dokploy_compose",
            physical_target_id=resource.resource_id,
            parent_target_id="stack",
            expected_fingerprint=sha256(identity.encode()).hexdigest(),
        )


def record_uninstall_deletion(state_dir: Path, resource: OwnedResource) -> None:
    UninstallAuthorityStore(state_dir).record_deletion(resource)


def _secret_step(identity: tuple[str, str, str, str]) -> CoderSecretReceiptStep:
    secret_id, secret_name, env_name, description = identity
    metadata_hash = metadata_sha256(
        {
            "secret_id": secret_id,
            "name": secret_name,
            "env_name": env_name,
            "description": description,
        }
    )
    return CoderSecretReceiptStep(
        secret_name=secret_name,
        secret_id=secret_id,
        env_name=env_name,
        description=description,
        operation="update",
        status="verified",
        pre_metadata_sha256=metadata_hash,
        second_pre_metadata_sha256=metadata_hash,
        source_value_sha256="a" * 64,
        expected_post_sha256=metadata_hash,
        response_sha256="b" * 64,
        workspace_verification_sha256="c" * 64,
        updated_at=UPDATED_AT,
    )
