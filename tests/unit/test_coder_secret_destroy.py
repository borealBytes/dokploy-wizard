from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.coder_secret_destroy import (
    CoderSecretDestroyer,
    CoderSecretDestroyError,
)
from dokploy_wizard.dokploy.coder_secret_receipts import (
    CoderSecretReceipt,
    CoderSecretReceiptStep,
    CoderSecretReceiptStore,
    metadata_sha256,
)
from dokploy_wizard.dokploy.coder_secret_reconciliation import CoderSecretMetadata
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import WorkspaceVerificationReceipt

_OWNER_ID = sha256(b"coder-secret:stack:coder.example.test").hexdigest()
_UPDATED_AT = "2026-07-31T00:00:00Z"
_SECRET_IDENTITIES = (
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


class SecretClient:
    def __init__(self) -> None:
        self.events: list[str] = []

    def list_secrets(self) -> tuple[CoderSecretMetadata, ...]:
        self.events.append("list")
        return ()

    def delete_secret(self, secret: CoderSecretMetadata) -> None:
        self.events.append(f"delete:{secret.secret_id}")

    def require_workspace_receipt_absence(self, receipt: WorkspaceVerificationReceipt) -> None:
        del receipt
        self.events.append("workspace-absence")


def _step(identity: tuple[str, str, str, str]) -> CoderSecretReceiptStep:
    secret_id, secret_name, env_name, description = identity
    metadata = metadata_sha256(
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
        pre_metadata_sha256=metadata,
        second_pre_metadata_sha256=metadata,
        source_value_sha256="a" * 64,
        expected_post_sha256=metadata,
        response_sha256="b" * 64,
        workspace_verification_sha256="c" * 64,
        updated_at=_UPDATED_AT,
    )


def _write_completed_secret_receipt(state_dir: Path) -> None:
    CoderSecretReceiptStore(state_dir).write(
        CoderSecretReceipt(
            owner_id=_OWNER_ID,
            status="completed",
            steps=tuple(_step(identity) for identity in _SECRET_IDENTITIES),
        )
    )


def test_destroy_requires_completed_secret_receipt_before_listing_secrets(tmp_path: Path) -> None:
    client = SecretClient()

    with pytest.raises(CoderSecretDestroyError, match="secret receipt"):
        CoderSecretDestroyer(state_dir=tmp_path, client=client, owner_id=_OWNER_ID).destroy()

    assert client.events == []
