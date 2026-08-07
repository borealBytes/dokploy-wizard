from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import (
    WORKSPACE_VERIFICATION_RECEIPT_FILENAME,
)
from dokploy_wizard.dokploy.coder_secret_workspace_receipt_fs import (
    read_receipt_bytes,
    write_receipt_bytes,
)
from dokploy_wizard.dokploy.coder_secret_workspace_receipt_schema_v2 import (
    CORRECTED_CREATE_PROTOCOL_FINGERPRINT,
    parse_v2_parent,
)


@dataclass(frozen=True, slots=True)
class WorkspaceSupersessionContext:
    machine_sha256: str
    ssh_sha256: str
    lifecycle_sha256: str
    stack_sha256: str
    final_commit: str
    attempt_context_sha256: str


def capture_workspace_supersession_authorization(
    state_dir: Path,
    output: Path,
    context: WorkspaceSupersessionContext,
) -> None:
    parent_bytes = read_receipt_bytes(
        state_dir, WORKSPACE_VERIFICATION_RECEIPT_FILENAME
    )
    if parent_bytes is None:
        raise _authorization_error()
    parent = parse_v2_parent(parent_bytes)
    existing = read_receipt_bytes(output.parent, output.name)
    if existing is not None:
        require_workspace_supersession_context(output, context)
        from dokploy_wizard.dokploy.coder_secret_workspace_supersession import (
            load_workspace_supersession_authorization,
        )

        if load_workspace_supersession_authorization(output).parent_bytes != parent_bytes:
            raise _authorization_error()
        return
    for digest in (
        context.machine_sha256,
        context.ssh_sha256,
        context.lifecycle_sha256,
        context.stack_sha256,
        context.attempt_context_sha256,
    ):
        _require_digest(digest)
    if (
        len(context.final_commit) != 40
        or set(context.final_commit) - set("0123456789abcdef")
    ):
        raise _authorization_error()
    payload = json.dumps(
        {
            "attempt_context_sha256": context.attempt_context_sha256,
            "authorization_kind": "task18-verifier-v1-to-v2",
            "env_name": parent.env_name,
            "expected_value_sha256": parent.expected_value_sha256,
            "final_commit": context.final_commit,
            "lifecycle_sha256": context.lifecycle_sha256,
            "machine_sha256": context.machine_sha256,
            "owner_id": parent.owner_id,
            "parent_receipt_base64": base64.b64encode(parent_bytes).decode(),
            "parent_receipt_sha256": hashlib.sha256(parent_bytes).hexdigest(),
            "predecessor_protocol": "legacy-create-v1",
            "schema_version": 1,
            "ssh_sha256": context.ssh_sha256,
            "stack_sha256": context.stack_sha256,
            "successor_protocol": "corrected-create-v2",
            "successor_protocol_fingerprint": CORRECTED_CREATE_PROTOCOL_FINGERPRINT,
            "template_id": parent.template_id,
            "template_name": parent.template_name,
            "workspace_name": parent.workspace_name,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    write_receipt_bytes(output.parent, output.name, payload)


def require_workspace_supersession_context(
    authorization_path: Path, context: WorkspaceSupersessionContext
) -> None:
    from dokploy_wizard.dokploy.coder_secret_workspace_supersession import (
        load_workspace_supersession_authorization,
    )

    authorization = load_workspace_supersession_authorization(authorization_path)
    if (
        authorization.machine_sha256,
        authorization.ssh_sha256,
        authorization.lifecycle_sha256,
        authorization.stack_sha256,
        authorization.final_commit,
        authorization.attempt_context_sha256,
    ) != (
        context.machine_sha256,
        context.ssh_sha256,
        context.lifecycle_sha256,
        context.stack_sha256,
        context.final_commit,
        context.attempt_context_sha256,
    ):
        raise _authorization_error()


def _require_digest(value: str) -> None:
    if len(value) != 64 or set(value) - set("0123456789abcdef"):
        raise _authorization_error()


def _authorization_error() -> CoderSecretClientError:
    return CoderSecretClientError(
        "Coder workspace verification supersession authorization cannot be captured",
        kind="client_workspace_receipt_invalid",
    )
