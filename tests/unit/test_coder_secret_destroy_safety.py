from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.coder_secret_destroy import (
    CoderSecretDestroyer,
    CoderSecretDestroyError,
)
from dokploy_wizard.dokploy.coder_secret_receipts import (
    CoderSecretReceiptStore,
    metadata_sha256,
)
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import (
    WorkspaceVerificationPhase,
    WorkspaceVerificationReceipt,
    WorkspaceVerificationReceiptStore,
)

from .coder_secret_destroy_support import (
    OWNER_ID,
    SECRET_IDENTITIES,
    SecretDestroyClient,
    metadata,
    write_completed_secret_receipt,
)


def test_same_name_replacement_blocks_without_deletion(tmp_path: Path) -> None:
    write_completed_secret_receipt(tmp_path)
    first = SECRET_IDENTITIES[0]
    replacement = metadata(("00000000-0000-4000-8000-000000000099", *first[1:]))
    client = SecretDestroyClient(
        [replacement, *(metadata(identity) for identity in SECRET_IDENTITIES[1:])]
    )

    with pytest.raises(CoderSecretDestroyError, match="drift"):
        CoderSecretDestroyer(tmp_path, client, OWNER_ID).destroy()

    assert not any(event.startswith("delete:") for event in client.events)
    assert client.secrets[0] == replacement


def test_metadata_drift_blocks_without_deletion(tmp_path: Path) -> None:
    write_completed_secret_receipt(tmp_path)
    first = SECRET_IDENTITIES[0]
    drifted = metadata((first[0], first[1], "DRIFTED_ENV", first[3]))
    client = SecretDestroyClient(
        [drifted, *(metadata(identity) for identity in SECRET_IDENTITIES[1:])]
    )

    with pytest.raises(CoderSecretDestroyError, match="drift"):
        CoderSecretDestroyer(tmp_path, client, OWNER_ID).destroy()

    assert not any(event.startswith("delete:") for event in client.events)
    assert client.secrets[0] == drifted


def test_partial_delete_failure_resumes_without_touching_unowned_secret(tmp_path: Path) -> None:
    write_completed_secret_receipt(tmp_path)
    extra = metadata(
        ("00000000-0000-4000-8000-000000000099", "external", "EXTERNAL", "External")
    )
    client = SecretDestroyClient(
        [*(metadata(identity) for identity in SECRET_IDENTITIES), extra],
        failing_secret_id=SECRET_IDENTITIES[2][0],
    )
    destroyer = CoderSecretDestroyer(tmp_path, client, OWNER_ID)

    with pytest.raises(CoderSecretDestroyError, match="delete failed"):
        destroyer.destroy()

    assert (tmp_path / "coder-secret-receipts-v1.json").exists()
    assert (tmp_path / "coder-secret-destroy-receipts-v1.json").exists()
    assert extra in client.secrets

    client.failing_secret_id = None
    destroyer.destroy()

    assert client.secrets == [extra]
    assert (tmp_path / "coder-secret-receipts-v1.json").exists()
    assert (tmp_path / "coder-secret-destroy-receipts-v1.json").exists()


def test_already_absent_owned_secret_is_idempotent_success(tmp_path: Path) -> None:
    write_completed_secret_receipt(tmp_path)
    client = SecretDestroyClient(
        [metadata(identity) for identity in SECRET_IDENTITIES[1:]]
    )

    CoderSecretDestroyer(tmp_path, client, OWNER_ID).destroy()

    assert client.secrets == []
    assert f"delete:{SECRET_IDENTITIES[0][0]}" not in client.events


def test_duplicate_receipt_secret_ids_block_before_listing(tmp_path: Path) -> None:
    write_completed_secret_receipt(tmp_path)
    store = CoderSecretReceiptStore(tmp_path)
    receipt = store.load()
    assert receipt is not None
    first, second, *remaining = receipt.steps
    duplicate_hash = metadata_sha256(
        {
            "secret_id": first.secret_id,
            "name": second.secret_name,
            "env_name": second.env_name,
            "description": second.description,
        }
    )
    duplicate = replace(
        second,
        secret_id=first.secret_id,
        pre_metadata_sha256=duplicate_hash,
        second_pre_metadata_sha256=duplicate_hash,
        expected_post_sha256=duplicate_hash,
    )
    store.write(replace(receipt, steps=(first, duplicate, *remaining)))
    client = SecretDestroyClient([metadata(identity) for identity in SECRET_IDENTITIES])

    with pytest.raises(CoderSecretDestroyError, match="inventory"):
        CoderSecretDestroyer(tmp_path, client, OWNER_ID).destroy()

    assert client.events == []


def test_foreign_workspace_receipt_is_preserved_after_secret_absence(tmp_path: Path) -> None:
    write_completed_secret_receipt(tmp_path)
    receipt = WorkspaceVerificationReceipt(
        owner_id="f" * 64,
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
        created_at="2026-07-31T00:00:00Z",
        updated_at="2026-07-31T00:00:00Z",
    )
    WorkspaceVerificationReceiptStore(tmp_path).write(receipt)
    client = SecretDestroyClient([])

    with pytest.raises(CoderSecretDestroyError, match="workspace"):
        CoderSecretDestroyer(tmp_path, client, OWNER_ID).destroy()

    stored = WorkspaceVerificationReceiptStore(tmp_path).load()
    assert stored == receipt


def test_malformed_secret_receipt_blocks_before_listing(tmp_path: Path) -> None:
    write_completed_secret_receipt(tmp_path)
    path = tmp_path / "coder-secret-receipts-v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["unknown"] = True
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    path.chmod(0o600)
    client = SecretDestroyClient([metadata(identity) for identity in SECRET_IDENTITIES])

    with pytest.raises(CoderSecretDestroyError, match="receipt"):
        CoderSecretDestroyer(tmp_path, client, OWNER_ID).destroy()

    assert client.events == []


def test_workspace_receipt_is_preserved_after_terminal_remote_absence_until_service_delete(
    tmp_path: Path,
) -> None:
    write_completed_secret_receipt(tmp_path)
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
        created_at="2026-07-31T00:00:00Z",
        updated_at="2026-07-31T00:00:00Z",
    )
    store = WorkspaceVerificationReceiptStore(tmp_path)
    store.write(receipt)
    client = SecretDestroyClient([], workspace_present=True)
    destroyer = CoderSecretDestroyer(tmp_path, client, OWNER_ID)

    with pytest.raises(CoderSecretDestroyError, match="workspace"):
        destroyer.destroy()

    assert store.load() == receipt
    assert (tmp_path / "coder-secret-receipts-v1.json").exists()

    client.workspace_present = False
    destroyer.destroy()

    assert store.load() == receipt
    assert (tmp_path / "coder-secret-receipts-v1.json").exists()
