"""Atomic remote phase receipt runtime for Task 1 proof wrappers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Final, Sequence, assert_never

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal_schema import (
    Task1CloudflareJournalDocument,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import (
    Task1ProofContextV1,
    sha256,
)
from dokploy_wizard.proof.model_sync_task1_remote_receipt_schema import (
    Task1RemoteProofReceipt,
    append_stage,
    complete_receipt,
    new_receipt,
)
from dokploy_wizard.proof.model_sync_task1_remote_receipt_schema_types import (
    Task1RemoteProofBinding,
    Task1RemoteProofResult,
    Task1RemoteProofStage,
    Task1RemoteReceiptError,
)
from dokploy_wizard.state.store import STATE_DOCUMENT_FILES

_MAX_ARCHIVE_BYTES: Final = 64 * 1024 * 1024
_MAX_ENV_BYTES: Final = 256 * 1024
_MAX_STATE_BYTES: Final = 16 * 1024 * 1024
_MAX_RECEIPT_BYTES: Final = 64 * 1024
_JOURNAL_FILE: Final = "task1-cloudflare-cleanup.journal.json"


def receipt_path(state_dir: Path, context_sha256: str) -> Path:
    """Derive one context-specific receipt pathname without persisting remote paths."""
    _require_sha256(context_sha256)
    return state_dir / f"task1-remote-proof-{context_sha256}.receipt.json"


def begin_receipt(
    *,
    state_dir: Path,
    archive_path: Path,
    env_file: Path,
    context_file: Path,
    proof_commit: str,
    expected_archive_sha256: str,
) -> Task1RemoteProofReceipt:
    """Bind the actual remote archive and uploads before any lifecycle mutation."""
    archive = _read(archive_path, _MAX_ARCHIVE_BYTES, None, "archive")
    uploaded_env = _read(env_file, _MAX_ENV_BYTES, 0o600, "upload environment")
    context_bytes = _read(context_file, _MAX_ENV_BYTES, 0o600, "context")
    context = Task1ProofContextV1.from_bytes(context_bytes)
    archive_sha256 = sha256(archive)
    if archive_sha256 != expected_archive_sha256:
        raise Task1RemoteReceiptError("Task 1 remote proof archive hash mismatched after upload")
    if sha256(uploaded_env) != context.uploaded_env_sha256:
        raise Task1RemoteReceiptError("Task 1 remote proof upload hash mismatched after upload")
    binding = Task1RemoteProofBinding(
        proof_commit=proof_commit,
        context_sha256=sha256(context_bytes),
        uploaded_env_sha256=context.uploaded_env_sha256,
        archive_sha256=archive_sha256,
    )
    receipt = new_receipt(
        binding,
        (
            (Task1RemoteProofStage.ARCHIVE, archive_sha256),
            (
                Task1RemoteProofStage.UPLOAD,
                _hash_projection((binding.context_sha256, binding.uploaded_env_sha256)),
            ),
        ),
    )
    path = receipt_path(state_dir, binding.context_sha256)
    if os.path.lexists(path):
        raise Task1RemoteReceiptError("Task 1 remote proof receipt replay is not allowed")
    artifacts.atomic_write_bytes(path, receipt.to_bytes(), mode=0o600)
    return receipt


def advance_receipt(
    *, state_dir: Path, context_sha256: str, stage: Task1RemoteProofStage
) -> Task1RemoteProofReceipt:
    """Advance exactly one ordered phase after proving remote state and journal presence."""
    path = receipt_path(state_dir, context_sha256)
    receipt = _load_receipt(path)
    if receipt.binding.context_sha256 != context_sha256:
        raise Task1RemoteReceiptError("Task 1 remote proof receipt context mismatched")
    evidence_sha256 = _state_evidence(state_dir, context_sha256, stage)
    advanced = append_stage(receipt, stage, evidence_sha256)
    artifacts.atomic_write_bytes(path, advanced.to_bytes(), mode=0o600)
    return advanced


def collect_receipt(*, state_dir: Path, context_sha256: str) -> bytes:
    """Finish collection atomically and emit only the canonical value-free receipt."""
    path = receipt_path(state_dir, context_sha256)
    receipt = _load_receipt(path)
    if receipt.result is Task1RemoteProofResult.SUCCESS:
        return receipt.to_bytes()
    if len(receipt.stages) < len(Task1RemoteProofStage):
        receipt = advance_receipt(
            state_dir=state_dir,
            context_sha256=context_sha256,
            stage=Task1RemoteProofStage.COLLECT,
        )
    completed = complete_receipt(receipt)
    artifacts.atomic_write_bytes(path, completed.to_bytes(), mode=0o600)
    return completed.to_bytes()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="model-sync-task1-remote-receipt")
    subparsers = parser.add_subparsers(dest="command", required=True)
    begin = subparsers.add_parser("begin")
    begin.add_argument("--state-dir", type=Path, required=True)
    begin.add_argument("--archive", type=Path, required=True)
    begin.add_argument("--env-file", type=Path, required=True)
    begin.add_argument("--context", type=Path, required=True)
    begin.add_argument("--proof-commit", required=True)
    begin.add_argument("--archive-sha256", required=True)
    advance = subparsers.add_parser("advance")
    advance.add_argument("--state-dir", type=Path, required=True)
    advance.add_argument("--context-sha256", required=True)
    advance.add_argument(
        "--stage",
        choices=("install", "verify", "inspect"),
        required=True,
    )
    collect = subparsers.add_parser("collect")
    collect.add_argument("--state-dir", type=Path, required=True)
    collect.add_argument("--context-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        match args.command:
            case "begin":
                begin_receipt(
                    state_dir=args.state_dir,
                    archive_path=args.archive,
                    env_file=args.env_file,
                    context_file=args.context,
                    proof_commit=args.proof_commit,
                    expected_archive_sha256=args.archive_sha256,
                )
            case "advance":
                advance_receipt(
                    state_dir=args.state_dir,
                    context_sha256=args.context_sha256,
                    stage=Task1RemoteProofStage(args.stage),
                )
            case "collect":
                sys.stdout.buffer.write(
                    collect_receipt(
                        state_dir=args.state_dir,
                        context_sha256=args.context_sha256,
                    )
                )
            case unreachable:
                assert_never(unreachable)
    except (OSError, Task1RemoteReceiptError, ValueError) as error:
        print(f"Task 1 remote proof receipt failed: {error}", file=sys.stderr)
        return 1
    return 0


def _state_evidence(state_dir: Path, context_sha256: str, stage: Task1RemoteProofStage) -> str:
    if stage not in {
        Task1RemoteProofStage.INSTALL,
        Task1RemoteProofStage.VERIFY,
        Task1RemoteProofStage.INSPECT,
        Task1RemoteProofStage.COLLECT,
    }:
        raise Task1RemoteReceiptError("Task 1 remote proof state stage is invalid")
    hashes: list[str] = []
    for name in STATE_DOCUMENT_FILES:
        content = _read(state_dir / name, _MAX_STATE_BYTES, 0o600, f"state after {stage}")
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Task1RemoteReceiptError(
                f"Task 1 remote proof state is invalid after {stage}"
            ) from error
        if not isinstance(payload, dict):
            raise Task1RemoteReceiptError(f"Task 1 remote proof state is invalid after {stage}")
        hashes.append(sha256(content))
    journal_bytes = _read(
        state_dir / _JOURNAL_FILE,
        _MAX_STATE_BYTES,
        0o600,
        f"Cloudflare journal after {stage}",
    )
    journal = Task1CloudflareJournalDocument.from_bytes(journal_bytes)
    if journal.context_sha256 != context_sha256:
        raise Task1RemoteReceiptError(
            f"Task 1 remote proof Cloudflare journal context mismatched after {stage}"
        )
    return _hash_projection((*hashes, sha256(journal_bytes)))


def _load_receipt(path: Path) -> Task1RemoteProofReceipt:
    return Task1RemoteProofReceipt.from_bytes(_read(path, _MAX_RECEIPT_BYTES, 0o600, "receipt"))


def _read(path: Path, limit: int, mode: int | None, label: str) -> bytes:
    try:
        content, _mode = proof.read_bounded_regular_bytes(path, limit, mode)
    except (OSError, ValueError) as error:
        raise Task1RemoteReceiptError(f"Task 1 remote proof {label} is absent or unsafe") from error
    return content


def _hash_projection(values: tuple[str, ...]) -> str:
    return hashlib.sha256(("\n".join(values) + "\n").encode()).hexdigest()


def _require_sha256(value: str) -> None:
    if len(value) != 64 or set(value) - set("0123456789abcdef"):
        raise Task1RemoteReceiptError("Task 1 remote proof context hash is invalid")


if __name__ == "__main__":
    raise SystemExit(main())
