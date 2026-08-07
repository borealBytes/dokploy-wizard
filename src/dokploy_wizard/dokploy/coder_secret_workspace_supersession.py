from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Iterator

from dokploy_wizard.dokploy.coder_migration_types import JsonValue
from dokploy_wizard.dokploy.coder_secret_types import CoderSecretClientError
from dokploy_wizard.dokploy.coder_secret_workspace_contract import (
    CoderWorkspaceRunner,
    WorkspaceVerificationIntent,
)
from dokploy_wizard.dokploy.coder_secret_workspace_inventory import workspace_records
from dokploy_wizard.dokploy.coder_secret_workspace_receipt import (
    WorkspaceVerificationReceipt,
    WorkspaceVerificationReceiptStore,
)
from dokploy_wizard.dokploy.coder_secret_workspace_receipt_fs import read_receipt_bytes
from dokploy_wizard.dokploy.coder_secret_workspace_receipt_schema_v2 import (
    CORRECTED_CREATE_PROTOCOL_FINGERPRINT,
    parse_v2_parent,
)
from dokploy_wizard.dokploy.coder_secret_workspace_receipt_types import (
    WorkspaceVerificationPhase,
)

_AUTHORIZATION_KEYS: Final = frozenset(
    {
        "attempt_context_sha256",
        "authorization_kind",
        "env_name",
        "expected_value_sha256",
        "final_commit",
        "lifecycle_sha256",
        "machine_sha256",
        "owner_id",
        "parent_receipt_base64",
        "parent_receipt_sha256",
        "predecessor_protocol",
        "schema_version",
        "ssh_sha256",
        "stack_sha256",
        "successor_protocol",
        "successor_protocol_fingerprint",
        "template_id",
        "template_name",
        "workspace_name",
    }
)
_LOCK_FILENAME: Final = ".coder-workspace-verification-supersession.lock"


@dataclass(frozen=True, slots=True)
class WorkspaceSupersessionAuthorization:
    parent_bytes: bytes
    owner_id: str
    workspace_name: str
    template_id: str
    template_name: str
    env_name: str
    expected_value_sha256: str
    machine_sha256: str
    ssh_sha256: str
    lifecycle_sha256: str
    stack_sha256: str
    final_commit: str
    attempt_context_sha256: str


def supersede_exhausted_receipt(
    runner: CoderWorkspaceRunner,
    store: WorkspaceVerificationReceiptStore,
    intent: WorkspaceVerificationIntent,
    authorization_path: Path,
) -> WorkspaceVerificationReceipt:
    with _supersession_lock(store.state_dir):
        parent_bytes = store.load_bytes()
        if parent_bytes is None:
            raise _state_error()
        parent = parse_v2_parent(parent_bytes)
        authorization = load_workspace_supersession_authorization(authorization_path)
        if authorization.parent_bytes != parent_bytes:
            raise _authorization_error()
        _require_intent(parent, intent, authorization)
        candidates = tuple(
            workspace
            for workspace in workspace_records(runner(("list", "--output", "json")))
            if workspace.workspace_name == parent.workspace_name
        )
        if candidates:
            raise CoderSecretClientError(
                "Coder workspace verification supersession candidate exists",
                kind="client_workspace_present",
            )
        current = store.load_bytes()
        if current != parent_bytes:
            raise _state_error()
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        successor = replace(
            parent,
            create_attempts=0,
            phase=WorkspaceVerificationPhase.PLANNED,
            failure_reason=None,
            created_at=now,
            updated_at=now,
            protocol_revision=2,
            predecessor_receipt_bytes=parent_bytes,
        )
        store.replace_exact(parent_bytes, successor)
        return successor


def load_workspace_supersession_authorization(
    path: Path,
) -> WorkspaceSupersessionAuthorization:
    payload = read_receipt_bytes(path.parent, path.name)
    if payload is None:
        raise _authorization_error()
    try:
        value: JsonValue = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _authorization_error() from error
    if not isinstance(value, dict) or frozenset(value) != _AUTHORIZATION_KEYS:
        raise _authorization_error()
    if (
        value.get("schema_version") != 1
        or value.get("authorization_kind") != "task18-verifier-v1-to-v2"
        or value.get("predecessor_protocol") != "legacy-create-v1"
        or value.get("successor_protocol") != "corrected-create-v2"
        or value.get("successor_protocol_fingerprint")
        != CORRECTED_CREATE_PROTOCOL_FINGERPRINT
    ):
        raise _authorization_error()
    encoded = _text(value.get("parent_receipt_base64"))
    try:
        parent_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise _authorization_error() from error
    if hashlib.sha256(parent_bytes).hexdigest() != _digest(
        value.get("parent_receipt_sha256")
    ):
        raise _authorization_error()
    parse_v2_parent(parent_bytes)
    attempt_context_sha256 = _digest(value.get("attempt_context_sha256"))
    lifecycle_sha256 = _digest(value.get("lifecycle_sha256"))
    machine_sha256 = _digest(value.get("machine_sha256"))
    ssh_sha256 = _digest(value.get("ssh_sha256"))
    stack_sha256 = _digest(value.get("stack_sha256"))
    final_commit = _text(value.get("final_commit"))
    if len(final_commit) != 40 or set(final_commit) - set("0123456789abcdef"):
        raise _authorization_error()
    return WorkspaceSupersessionAuthorization(
        parent_bytes=parent_bytes,
        owner_id=_text(value.get("owner_id")),
        workspace_name=_text(value.get("workspace_name")),
        template_id=_text(value.get("template_id")),
        template_name=_text(value.get("template_name")),
        env_name=_text(value.get("env_name")),
        expected_value_sha256=_digest(value.get("expected_value_sha256")),
        machine_sha256=machine_sha256,
        ssh_sha256=ssh_sha256,
        lifecycle_sha256=lifecycle_sha256,
        stack_sha256=stack_sha256,
        final_commit=final_commit,
        attempt_context_sha256=attempt_context_sha256,
    )


def _require_intent(
    parent: WorkspaceVerificationReceipt,
    intent: WorkspaceVerificationIntent,
    authorization: WorkspaceSupersessionAuthorization,
) -> None:
    expected = (
        parent.owner_id,
        parent.workspace_name,
        parent.template_id,
        parent.template_name,
        parent.env_name,
        parent.expected_value_sha256,
    )
    authorized = (
        authorization.owner_id,
        authorization.workspace_name,
        authorization.template_id,
        authorization.template_name,
        authorization.env_name,
        authorization.expected_value_sha256,
    )
    if expected != authorized or (
        parent.owner_id,
        parent.env_name,
        parent.expected_value_sha256,
    ) != (intent.owner_id, intent.env_name, intent.expected_value_sha256):
        raise _authorization_error()


@contextmanager
def _supersession_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = state_dir.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(directory.st_mode)
        or stat.S_IMODE(directory.st_mode) != 0o700
        or directory.st_uid != os.geteuid()
    ):
        raise _state_error()
    lock_path = state_dir / _LOCK_FILENAME
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
            ):
                raise _state_error()
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            raise _state_error() from error
        yield
    finally:
        os.close(descriptor)


def _text(value: JsonValue | None) -> str:
    if not isinstance(value, str) or value == "":
        raise _authorization_error()
    return value


def _digest(value: JsonValue | None) -> str:
    text = _text(value)
    if len(text) != 64 or set(text) - set("0123456789abcdef"):
        raise _authorization_error()
    return text


def _authorization_error() -> CoderSecretClientError:
    return CoderSecretClientError(
        "Coder workspace verification supersession authorization is invalid",
        kind="client_workspace_receipt_invalid",
    )


def _state_error() -> CoderSecretClientError:
    return CoderSecretClientError(
        "Coder workspace verification supersession state is invalid",
        kind="client_workspace_receipt_state",
    )
