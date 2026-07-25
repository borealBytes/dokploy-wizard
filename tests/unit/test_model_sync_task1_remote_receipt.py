from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Callable

import pytest

from dokploy_wizard.proof import JsonValue, require_mapping
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_schema import (
    JournalStatus,
    Task1CloudflareJournalDocument,
    Task1CloudflareJournalVersion,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import (
    Task1ProofContextV1,
    sha256,
)
from dokploy_wizard.proof.model_sync_task1_remote_receipt import (
    advance_receipt,
    begin_receipt,
    collect_receipt,
    receipt_path,
)
from dokploy_wizard.proof.model_sync_task1_remote_receipt_schema import (
    Task1RemoteProofReceipt,
    append_stage,
    complete_receipt,
    new_receipt,
)
from dokploy_wizard.proof.model_sync_task1_remote_receipt_schema_types import (
    TASK1_REMOTE_PROOF_STAGES,
    Task1RemoteProofBinding,
    Task1RemoteProofExpectation,
    Task1RemoteProofStage,
    Task1RemoteReceiptError,
)
from dokploy_wizard.proof.model_sync_task1_remote_receipt_validation import (
    require_terminal_receipt,
)
from dokploy_wizard.state.store import STATE_DOCUMENT_FILES


def _binding() -> Task1RemoteProofBinding:
    return Task1RemoteProofBinding(
        proof_commit="1" * 40,
        context_sha256="2" * 64,
        uploaded_env_sha256="3" * 64,
        archive_sha256="4" * 64,
    )


def _terminal_receipt() -> Task1RemoteProofReceipt:
    receipt = new_receipt(_binding(), ())
    for index, stage in enumerate(TASK1_REMOTE_PROOF_STAGES, start=5):
        receipt = append_stage(receipt, stage, f"{index:x}" * 64)
    return complete_receipt(receipt)


def _payload() -> dict[str, JsonValue]:
    return require_mapping(json.loads(_terminal_receipt().to_bytes()), "receipt")


def _canonical(payload: dict[str, JsonValue]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b'{"schema_version":1',
        b'{"schema_version":1}\ntrailing',
    ],
)
def test_terminal_receipt_rejects_missing_or_truncated_bytes(content: bytes) -> None:
    expectation = Task1RemoteProofExpectation("1" * 40, "2" * 64, "3" * 64)

    with pytest.raises(Task1RemoteReceiptError):
        require_terminal_receipt(content, expectation)


def test_terminal_receipt_rejects_partial_progress() -> None:
    partial = new_receipt(
        _binding(),
        (
            (Task1RemoteProofStage.ARCHIVE, "5" * 64),
            (Task1RemoteProofStage.UPLOAD, "6" * 64),
        ),
    )
    expectation = Task1RemoteProofExpectation("1" * 40, "2" * 64, "3" * 64)

    with pytest.raises(Task1RemoteReceiptError, match="not successful"):
        require_terminal_receipt(partial.to_bytes(), expectation)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"unknown": "field"}),
        lambda payload: payload["stages"].append(payload["stages"][-1]),
        lambda payload: payload["stages"].__setitem__(
            1, {**payload["stages"][1], "stage": "archive"}
        ),
        lambda payload: payload["stages"].__setitem__(
            1, {**payload["stages"][1], "stage": "unknown"}
        ),
        lambda payload: payload["stages"][2].update({"evidence_sha256": "a" * 64}),
    ],
    ids=("extra-key", "duplicate", "out-of-order", "unknown-stage", "tampered-chain"),
)
def test_receipt_rejects_closed_schema_order_and_chain_tampering(
    mutate: Callable[[dict[str, JsonValue]], None],
) -> None:
    payload = _payload()
    mutate(payload)

    with pytest.raises(Task1RemoteReceiptError):
        Task1RemoteProofReceipt.from_bytes(_canonical(payload))


@pytest.mark.parametrize(
    "expectation",
    [
        Task1RemoteProofExpectation("9" * 40, "2" * 64, "3" * 64),
        Task1RemoteProofExpectation("1" * 40, "9" * 64, "3" * 64),
        Task1RemoteProofExpectation("1" * 40, "2" * 64, "9" * 64),
    ],
    ids=("foreign-commit", "foreign-context", "foreign-upload"),
)
def test_terminal_receipt_rejects_foreign_binding(
    expectation: Task1RemoteProofExpectation,
) -> None:
    with pytest.raises(Task1RemoteReceiptError, match="another invocation"):
        require_terminal_receipt(_terminal_receipt().to_bytes(), expectation)


def _remote_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Task1ProofContextV1]:
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(b"committed archive")
    env_file = tmp_path / "upload.env"
    env_file.write_bytes(b"ROOT_DOMAIN=example.test\n")
    digest = "a" * 64
    context = Task1ProofContextV1(
        context_id="b" * 32,
        source_env_sha256=digest,
        normalized_env_sha256="c" * 64,
        overlay_env_sha256="d" * 64,
        uploaded_env_sha256=sha256(env_file.read_bytes()),
        namespace_sha256="e" * 64,
        expected_restored_source_sha256=digest,
        source_env_mode=0o600,
        root_domain="example.test",
        stack_name="proof-stack",
        tunnel_name="proof-tunnel",
        dokploy_subdomain="dokploy-proof",
        coder_subdomain="coder-proof",
        seaweedfs_subdomain="s3-proof",
        litellm_admin_subdomain="litellm-proof",
    )
    context_file = tmp_path / "context.json"
    context_file.write_bytes(context.to_bytes())
    env_file.chmod(0o600)
    context_file.chmod(0o600)
    return archive, env_file, context_file, context


def test_begin_rejects_invalid_commit_without_publishing_receipt(tmp_path: Path) -> None:
    archive, env_file, context_file, context = _remote_inputs(tmp_path)
    state_dir = tmp_path / "state"

    with pytest.raises(Task1RemoteReceiptError):
        begin_receipt(
            state_dir=state_dir,
            archive_path=archive,
            env_file=env_file,
            context_file=context_file,
            proof_commit="invalid",
            expected_archive_sha256=sha256(archive.read_bytes()),
        )

    assert not receipt_path(state_dir, sha256(context.to_bytes())).exists()


def test_begin_is_mode_0600_atomic_and_replay_fails_closed(tmp_path: Path) -> None:
    archive, env_file, context_file, context = _remote_inputs(tmp_path)
    state_dir = tmp_path / "state"
    receipt = begin_receipt(
        state_dir=state_dir,
        archive_path=archive,
        env_file=env_file,
        context_file=context_file,
        proof_commit="1" * 40,
        expected_archive_sha256=sha256(archive.read_bytes()),
    )

    path = receipt_path(state_dir, sha256(context.to_bytes()))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == receipt.to_bytes()
    with pytest.raises(Task1RemoteReceiptError, match="replay"):
        begin_receipt(
            state_dir=state_dir,
            archive_path=archive,
            env_file=env_file,
            context_file=context_file,
            proof_commit="1" * 40,
            expected_archive_sha256=sha256(archive.read_bytes()),
        )


def test_install_stage_requires_remote_state_and_cloudflare_journal(tmp_path: Path) -> None:
    archive, env_file, context_file, context = _remote_inputs(tmp_path)
    state_dir = tmp_path / "state"
    begin_receipt(
        state_dir=state_dir,
        archive_path=archive,
        env_file=env_file,
        context_file=context_file,
        proof_commit="1" * 40,
        expected_archive_sha256=sha256(archive.read_bytes()),
    )

    with pytest.raises(Task1RemoteReceiptError, match="state after install"):
        advance_receipt(
            state_dir=state_dir,
            context_sha256=sha256(context.to_bytes()),
            stage=Task1RemoteProofStage.INSTALL,
        )


def test_complete_remote_progression_returns_terminal_context_bound_receipt(tmp_path: Path) -> None:
    archive, env_file, context_file, context = _remote_inputs(tmp_path)
    context_sha256 = sha256(context.to_bytes())
    state_dir = tmp_path / "state"
    begin_receipt(
        state_dir=state_dir,
        archive_path=archive,
        env_file=env_file,
        context_file=context_file,
        proof_commit="1" * 40,
        expected_archive_sha256=sha256(archive.read_bytes()),
    )
    for name in STATE_DOCUMENT_FILES:
        path = state_dir / name
        path.write_bytes(b"{}\n")
        path.chmod(0o600)
    journal = Task1CloudflareJournalDocument(
        context_sha256=context_sha256,
        version=Task1CloudflareJournalVersion(1, "9" * 64),
        status=JournalStatus.ACTIVE,
        operations=(),
    )
    journal_path = state_dir / "task1-cloudflare-cleanup.journal.json"
    journal_path.write_bytes(journal.to_bytes())
    journal_path.chmod(0o600)
    for stage in (
        Task1RemoteProofStage.INSTALL,
        Task1RemoteProofStage.VERIFY,
        Task1RemoteProofStage.INSPECT,
    ):
        advance_receipt(
            state_dir=state_dir,
            context_sha256=context_sha256,
            stage=stage,
        )

    content = collect_receipt(state_dir=state_dir, context_sha256=context_sha256)

    receipt = require_terminal_receipt(
        content,
        Task1RemoteProofExpectation("1" * 40, context_sha256, context.uploaded_env_sha256),
    )
    assert tuple(record.stage for record in receipt.stages) == TASK1_REMOTE_PROOF_STAGES


def test_terminal_receipt_contains_no_remote_or_secret_values() -> None:
    content = _terminal_receipt().to_bytes()

    for forbidden in (
        b"host.example.test",
        b"password-sentinel",
        b"provider-id",
        b"/root/dokploy-wizard",
        b"stdout",
        b"stderr",
    ):
        assert forbidden not in content
