"""Value-free durable receipt primitives for interrupted proof-env recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

ReceiptPayload: TypeAlias = dict[str, str | int]


@dataclass(frozen=True, slots=True)
class EnvReceipt:
    """Exact paths, hashes, and mode required to recover one proof environment."""

    env_path: str
    backup_path: str
    original_sha256: str
    proof_sha256: str
    mode: int


def parse_env_receipt(value: ReceiptPayload | None) -> EnvReceipt | None:
    """Parse one complete value-free receipt or fail closed."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "backup_path", "env_path", "mode", "original_sha256", "proof_sha256"
    }:
        raise ValueError("abort guard env receipt is invalid")
    backup_path = value.get("backup_path")
    env_path = value.get("env_path")
    mode = value.get("mode")
    original_sha256 = value.get("original_sha256")
    proof_sha256 = value.get("proof_sha256")
    if (
        not isinstance(backup_path, str)
        or not isinstance(env_path, str)
        or not isinstance(mode, int)
        or not isinstance(original_sha256, str)
        or not isinstance(proof_sha256, str)
        or not backup_path
        or not env_path
        or mode < 0
        or mode > 0o777
        or not _valid_sha256(original_sha256)
        or not _valid_sha256(proof_sha256)
    ):
        raise ValueError("abort guard env receipt is invalid")
    return EnvReceipt(env_path, backup_path, original_sha256, proof_sha256, mode)


def env_receipt_payload(receipt: EnvReceipt | None) -> ReceiptPayload | None:
    """Serialize a receipt without retaining any environment values."""
    if receipt is None:
        return None
    return {
        "backup_path": receipt.backup_path,
        "env_path": receipt.env_path,
        "mode": receipt.mode,
        "original_sha256": receipt.original_sha256,
        "proof_sha256": receipt.proof_sha256,
    }


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
