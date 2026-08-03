from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal, assert_never

import pytest

from dokploy_wizard.dokploy.coder_secret_destroy import CoderSecretDestroyer
from dokploy_wizard.dokploy.coder_secret_destroy_authorization import source_receipt_sha256
from dokploy_wizard.dokploy.coder_secret_destroy_receipts import (
    CoderSecretDestroyReceiptStore,
)
from dokploy_wizard.dokploy.coder_secret_receipts import CoderSecretReceiptStore
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import (
    WorkspaceVerificationReceiptStore,
)
from dokploy_wizard.dokploy.coder_service_teardown_finalizer import (
    destroy_receipt_sha256,
    workspace_receipt_sha256,
)
from dokploy_wizard.dokploy.coder_service_teardown_receipts import (
    CoderServiceTeardownReceipt,
    CoderServiceTeardownReceiptStore,
)
from dokploy_wizard.packs.coder import CODER_SERVICE_RESOURCE_TYPE
from dokploy_wizard.uninstall.coder_lifecycle import (
    CoderServiceDeletion,
    delete_coder_service,
)
from dokploy_wizard.uninstall.errors import UninstallExecutionError

from .coder_secret_destroy_support import (
    OWNER_ID,
    SECRET_IDENTITIES,
    SecretDestroyClient,
    metadata,
    write_completed_secret_receipt,
    write_terminal_workspace_receipt,
)
from .test_coder_service_teardown_transaction import ExecutorBackend, _destroy_inputs

HashField = Literal["source", "destroy", "workspace"]


def _write_intent(
    state_dir: Path,
    *,
    owner_id: str,
    tampered_hash: HashField | None = None,
) -> SecretDestroyClient:
    write_completed_secret_receipt(state_dir)
    source_store = CoderSecretReceiptStore(state_dir)
    source = source_store.load()
    assert source is not None
    if source.owner_id != owner_id:
        source = replace(source, owner_id=owner_id)
        source_store.write(source)
    workspace_store = WorkspaceVerificationReceiptStore(state_dir)
    workspace = write_terminal_workspace_receipt(state_dir)
    if workspace.owner_id != owner_id:
        workspace = replace(workspace, owner_id=owner_id)
        workspace_store.write(workspace)
    client = SecretDestroyClient([metadata(identity) for identity in SECRET_IDENTITIES])
    CoderSecretDestroyer(state_dir, client, owner_id).destroy()
    destroy = CoderSecretDestroyReceiptStore(state_dir).load()
    assert destroy is not None
    source_hash = source_receipt_sha256(source)
    destroy_hash = destroy_receipt_sha256(destroy)
    workspace_hash = workspace_receipt_sha256(workspace)
    match tampered_hash:
        case "source":
            source_hash = "0" * 64
        case "destroy":
            destroy_hash = "0" * 64
        case "workspace":
            workspace_hash = "0" * 64
        case None:
            pass
        case unreachable:
            assert_never(unreachable)
    CoderServiceTeardownReceiptStore(state_dir).write(
        CoderServiceTeardownReceipt(
            stack_name="stack",
            resource_type=CODER_SERVICE_RESOURCE_TYPE,
            resource_id="coder-service",
            resource_scope="stack:stack:coder",
            owner_id=owner_id,
            source_receipt_sha256=source_hash,
            destroy_receipt_sha256=destroy_hash,
            workspace_receipt_sha256=workspace_hash,
            phase="intent",
            updated_at="2026-07-31T00:00:00Z",
        )
    )
    client.api_available = False
    return client


def _delete_context(state_dir: Path, backend: ExecutorBackend) -> CoderServiceDeletion:
    _, desired, _, plan = _destroy_inputs()
    return CoderServiceDeletion(state_dir, desired, plan.deletions[0], backend, plan.mode)


def test_foreign_owner_intent_blocks_before_backend_service_delete(tmp_path: Path) -> None:
    client = _write_intent(tmp_path, owner_id="f" * 64)
    backend = ExecutorBackend(tmp_path, client)

    with pytest.raises(UninstallExecutionError, match="teardown transaction"):
        delete_coder_service(_delete_context(tmp_path, backend))

    assert backend.events == []


@pytest.mark.parametrize("tampered_hash", ("source", "destroy", "workspace"))
def test_tampered_intent_hash_blocks_before_backend_service_delete(
    tmp_path: Path,
    tampered_hash: HashField,
) -> None:
    client = _write_intent(tmp_path, owner_id=OWNER_ID, tampered_hash=tampered_hash)
    backend = ExecutorBackend(tmp_path, client)

    with pytest.raises(UninstallExecutionError, match="teardown transaction"):
        delete_coder_service(_delete_context(tmp_path, backend))

    assert backend.events == []
