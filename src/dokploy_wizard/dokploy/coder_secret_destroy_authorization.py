"""Authorization checks for Coder secret destroy cleanup."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from uuid import UUID

from dokploy_wizard.dokploy.coder_secret_destroy_contract import CoderSecretDestroyError
from dokploy_wizard.dokploy.coder_secret_receipts import (
    CoderSecretReceipt,
    CoderSecretReceiptError,
    CoderSecretReceiptStep,
    CoderSecretReceiptStore,
    canonical_receipt_bytes,
)
from dokploy_wizard.dokploy.coder_secret_specs import CODER_SECRET_NAMES


def coder_secret_owner_id(stack_name: str, hostname: str) -> str:
    return sha256(f"coder-secret:{stack_name}:{hostname}".encode()).hexdigest()


def completed_source_receipt(state_dir: Path) -> CoderSecretReceipt:
    try:
        receipt = CoderSecretReceiptStore(state_dir).load()
    except CoderSecretReceiptError as error:
        raise CoderSecretDestroyError("Coder secret receipt is invalid") from error
    if receipt is None:
        raise CoderSecretDestroyError("Coder secret receipt does not authorize destroy")
    if receipt.status != "completed":
        raise CoderSecretDestroyError("Coder secret receipt is not completed")
    observed_names: frozenset[str] = frozenset(step.secret_name for step in receipt.steps)
    if observed_names != CODER_SECRET_NAMES:
        raise CoderSecretDestroyError("Coder secret receipt inventory is incomplete")
    secret_ids = tuple(secret_id(step) for step in receipt.steps)
    if len(set(secret_ids)) != len(secret_ids):
        raise CoderSecretDestroyError("Coder secret receipt inventory has duplicate IDs")
    return receipt


def source_receipt(state_dir: Path, owner_id: str) -> CoderSecretReceipt:
    receipt = completed_source_receipt(state_dir)
    if receipt.owner_id != owner_id:
        raise CoderSecretDestroyError("Coder secret receipt does not authorize destroy")
    return receipt


def source_receipt_sha256(receipt: CoderSecretReceipt) -> str:
    return sha256(canonical_receipt_bytes(receipt)).hexdigest()


def secret_id(step: CoderSecretReceiptStep) -> str:
    if step.secret_id is None:
        raise CoderSecretDestroyError("Coder secret receipt lacks a secret id")
    try:
        parsed = UUID(step.secret_id)
    except ValueError as error:
        raise CoderSecretDestroyError("Coder secret receipt secret id is invalid") from error
    if str(parsed) != step.secret_id:
        raise CoderSecretDestroyError("Coder secret receipt secret id is invalid")
    return step.secret_id
