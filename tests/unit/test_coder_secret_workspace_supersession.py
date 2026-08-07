from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError
from dokploy_wizard.dokploy.coder_secret_workspace_contract import (
    WorkspaceVerificationIntent,
)
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import (
    WorkspaceVerificationPlan,
    WorkspaceVerificationReceiptStore,
)
from dokploy_wizard.dokploy.coder_secret_workspace_receipt_schema import (
    parse_receipt_bytes,
)
from dokploy_wizard.dokploy.coder_secret_workspace_supersession import (
    supersede_exhausted_receipt,
)


def _legacy_parent_bytes() -> bytes:
    return json.dumps(
        {
            "create_attempts": 2,
            "created_at": "2026-08-07T00:00:00Z",
            "env_name": "OPENAI_API_KEY",
            "expected_value_sha256": "a" * 64,
            "failure_reason": "create_retry_exhausted",
            "observed_value_sha256": None,
            "owner_id": "b" * 64,
            "phase": "failed",
            "schema_version": 1,
            "template_id": "00000000-0000-4000-8000-000000000003",
            "template_name": "ubuntu-vscode-opencode-pi",
            "updated_at": "2026-08-07T00:00:02Z",
            "workspace_id": None,
            "workspace_name": "proof-workspace",
            "workspace_owner_id": None,
            "workspace_owner_name": None,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_v2_receipt_preserves_exact_exhausted_v1_ancestry() -> None:
    parent = _legacy_parent_bytes()
    successor = json.loads(parent)
    successor.update(
        {
            "create_attempts": 0,
            "created_at": "2026-08-07T00:00:03Z",
            "failure_reason": None,
            "phase": "planned",
            "updated_at": "2026-08-07T00:00:03Z",
        }
    )
    payload = json.dumps(
        {
            "create_attempt_limit": 2,
            "predecessor": {
                "protocol_revision": "legacy-create-v1",
                "receipt_base64": base64.b64encode(parent).decode(),
                "receipt_sha256": hashlib.sha256(parent).hexdigest(),
            },
            "protocol_revision": "corrected-create-v2",
            "protocol_fingerprint": hashlib.sha256(
                b"coder-create-v2:no-wait:parameter-defaults"
            ).hexdigest(),
            "schema_version": 2,
            "successor": successor,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    receipt = parse_receipt_bytes(payload)

    assert receipt.create_attempts == 0
    assert receipt.predecessor_receipt_bytes == parent
    assert receipt.protocol_revision == 2


def _exhausted_store(tmp_path: Path) -> tuple[WorkspaceVerificationReceiptStore, bytes]:
    state_dir = tmp_path / "state"
    store = WorkspaceVerificationReceiptStore(state_dir)
    receipt = store.write_planned(
        WorkspaceVerificationPlan(
            owner_id="b" * 64,
            workspace_name="proof-workspace",
            template_id="00000000-0000-4000-8000-000000000003",
            template_name="ubuntu-vscode-opencode-pi",
            env_name="OPENAI_API_KEY",
            expected_value_sha256="a" * 64,
        )
    )
    receipt = store.begin_create(receipt)
    receipt = store.begin_create(receipt)
    try:
        store.begin_create(receipt)
    except CoderSecretClientError:
        pass
    return store, (state_dir / "coder-workspace-verification-receipt.json").read_bytes()


def _authorization(tmp_path: Path, parent: bytes) -> Path:
    directory = tmp_path / "authorization"
    directory.mkdir(mode=0o700)
    path = directory / "coder-workspace-verification-authorization.json"
    path.write_text(
        json.dumps(
            {
                "attempt_context_sha256": "c" * 64,
                "authorization_kind": "task18-verifier-v1-to-v2",
                "expected_value_sha256": "a" * 64,
                "final_commit": "d" * 40,
                "lifecycle_sha256": "e" * 64,
                "machine_sha256": "f" * 64,
                "owner_id": "b" * 64,
                "parent_receipt_base64": base64.b64encode(parent).decode(),
                "parent_receipt_sha256": hashlib.sha256(parent).hexdigest(),
                "predecessor_protocol": "legacy-create-v1",
                "schema_version": 1,
                "ssh_sha256": "1" * 64,
                "stack_sha256": "2" * 64,
                "successor_protocol": "corrected-create-v2",
                "successor_protocol_fingerprint": hashlib.sha256(
                    b"coder-create-v2:no-wait:parameter-defaults"
                ).hexdigest(),
                "template_id": "00000000-0000-4000-8000-000000000003",
                "template_name": "ubuntu-vscode-opencode-pi",
                "workspace_name": "proof-workspace",
                "env_name": "OPENAI_API_KEY",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    os.chmod(path, 0o600)
    return path


def test_exact_authorization_supersedes_exhausted_parent_once(tmp_path: Path) -> None:
    store, parent = _exhausted_store(tmp_path)
    authorization = _authorization(tmp_path, parent)
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> str:
        commands.append(command)
        return "[]"

    successor = supersede_exhausted_receipt(
        runner,
        store,
        WorkspaceVerificationIntent("b" * 64, "OPENAI_API_KEY", "a" * 64),
        authorization,
    )

    assert successor.protocol_revision == 2
    assert successor.create_attempts == 0
    assert successor.predecessor_receipt_bytes == parent
    assert commands == [("list", "--output", "json")]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("owner_id", "9" * 64),
        ("parent_receipt_sha256", "9" * 64),
        ("successor_protocol", "future-create-v3"),
    ),
)
def test_supersession_rejects_tampered_authorization(
    tmp_path: Path, field: str, value: str
) -> None:
    store, parent = _exhausted_store(tmp_path)
    authorization = _authorization(tmp_path, parent)
    payload = json.loads(authorization.read_text())
    payload[field] = value
    authorization.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    with pytest.raises(CoderSecretClientError, match="authorization is invalid"):
        supersede_exhausted_receipt(
            lambda _command: "[]",
            store,
            WorkspaceVerificationIntent("b" * 64, "OPENAI_API_KEY", "a" * 64),
            authorization,
        )

    assert store.load_bytes() == parent


def test_supersession_rejects_existing_exact_name_candidate(tmp_path: Path) -> None:
    store, parent = _exhausted_store(tmp_path)
    authorization = _authorization(tmp_path, parent)
    inventory = json.dumps(
        [
            {
                "id": "00000000-0000-4000-8000-000000000001",
                "latest_build": {"status": "running"},
                "name": "proof-workspace",
                "owner_id": "00000000-0000-4000-8000-000000000002",
                "owner_name": "admin",
                "template_id": "00000000-0000-4000-8000-000000000003",
                "template_name": "ubuntu-vscode-opencode-pi",
            }
        ]
    )

    with pytest.raises(CoderSecretClientError, match="candidate exists"):
        supersede_exhausted_receipt(
            lambda _command: inventory,
            store,
            WorkspaceVerificationIntent("b" * 64, "OPENAI_API_KEY", "a" * 64),
            authorization,
        )

    assert store.load_bytes() == parent


def test_supersession_rejects_malformed_inventory(tmp_path: Path) -> None:
    store, parent = _exhausted_store(tmp_path)
    authorization = _authorization(tmp_path, parent)

    with pytest.raises(CoderSecretClientError, match="workspace inventory is invalid"):
        supersede_exhausted_receipt(
            lambda _command: "{}",
            store,
            WorkspaceVerificationIntent("b" * 64, "OPENAI_API_KEY", "a" * 64),
            authorization,
        )

    assert store.load_bytes() == parent


def test_v2_exhaustion_cannot_be_superseded_again(tmp_path: Path) -> None:
    store, parent = _exhausted_store(tmp_path)
    authorization = _authorization(tmp_path, parent)
    successor = supersede_exhausted_receipt(
        lambda _command: "[]",
        store,
        WorkspaceVerificationIntent("b" * 64, "OPENAI_API_KEY", "a" * 64),
        authorization,
    )
    successor = store.begin_create(successor)
    successor = store.begin_create(successor)
    persisted = store.load()
    assert persisted is not None
    assert persisted.create_attempts == 2
    assert persisted.protocol_revision == 2
    with pytest.raises(CoderSecretClientError, match="create retries exhausted"):
        store.begin_create(successor)
    exhausted = store.load_bytes()

    with pytest.raises(CoderSecretClientError, match="receipt is invalid"):
        supersede_exhausted_receipt(
            lambda _command: "[]",
            store,
            WorkspaceVerificationIntent("b" * 64, "OPENAI_API_KEY", "a" * 64),
            authorization,
        )

    assert store.load_bytes() == exhausted
