# ruff: noqa: E501
"""Crash-safe proof tooling for the Coder/LiteLLM model-sync baseline."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from dokploy_wizard.proof.model_sync_artifacts import JsonValue

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class EnvReceipt:
    env_path: str
    backup_path: str
    original_sha256: str
    proof_sha256: str
    mode: int

    def to_payload(self) -> dict[str, str | int]:
        return {"backup_path": self.backup_path, "env_path": self.env_path, "mode": self.mode, "original_sha256": self.original_sha256, "proof_sha256": self.proof_sha256, "schema_version": 1}


@dataclass(frozen=True, slots=True)
class BaselineAttestation:
    guard_id: str
    guard_path: str
    artifact_dir: str
    result_path: str
    env_receipt: EnvReceipt
    output_sha256: Mapping[str, str]
    result_body: Mapping[str, JsonValue]

    def to_payload(self) -> dict[str, JsonValue]:
        return {"artifact_dir": self.artifact_dir, "env_receipt": self.env_receipt.to_payload(), "guard_id": self.guard_id, "guard_path": self.guard_path, "kind": "task-1-baseline-finalization", "output_sha256": dict(self.output_sha256), "required_terminal": {"claimant_kind": "plan", "phase": "complete", "state": "armed"}, "result_body": dict(self.result_body), "result_path": self.result_path, "schema_version": 1}


def canonical_json_bytes(value: JsonValue) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def parse_env_receipt(value: JsonValue | None) -> EnvReceipt | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"backup_path", "env_path", "mode", "original_sha256", "proof_sha256", "schema_version"}:
        raise ValueError("abort guard env receipt is invalid")
    backup, env, original, proof, mode = _path(value["backup_path"]), _path(value["env_path"]), _hash(value["original_sha256"]), _hash(value["proof_sha256"]), value["mode"]
    if value["schema_version"] != 1 or isinstance(mode, bool) or not isinstance(mode, int) or not 1 <= mode <= 0o777:
        raise ValueError("abort guard env receipt is invalid")
    return EnvReceipt(env, backup, original, proof, mode)


def parse_baseline_attestation(value: JsonValue | None) -> BaselineAttestation | None:
    if value is None:
        return None
    expected = {"schema_version", "kind", "guard_id", "guard_path", "artifact_dir", "result_path", "env_receipt", "required_terminal", "output_sha256", "result_body"}
    if not isinstance(value, dict) or set(value) != expected or value["schema_version"] != 1 or value["kind"] != "task-1-baseline-finalization":
        raise ValueError("abort guard attestation is invalid")
    guard_id, guard_path, artifact_dir, result_path = _hash(value["guard_id"]), _path(value["guard_path"]), _path(value["artifact_dir"]), _path(value["result_path"])
    receipt, outputs, body = parse_env_receipt(value["env_receipt"]), value["output_sha256"], value["result_body"]
    if receipt is None or result_path != f"{artifact_dir}/result.json" or value["required_terminal"] != {"state": "armed", "phase": "complete", "claimant_kind": "plan"} or not isinstance(outputs, dict) or not isinstance(body, dict):
        raise ValueError("abort guard attestation is invalid")
    output_sha256 = {key: _hash(item) for key, item in outputs.items() if isinstance(key, str)}
    if len(output_sha256) != len(outputs) or set(output_sha256) != {"baseline.json", "host-a-preflight.json", "host-b-preflight.json", "protected-artifacts-before.txt"}:
        raise ValueError("abort guard attestation is invalid")
    return BaselineAttestation(guard_id, guard_path, artifact_dir, result_path, receipt, output_sha256, body)


def receipt_identity(receipt: EnvReceipt) -> tuple[str, str, str, str, int]:
    return (receipt.env_path, receipt.backup_path, receipt.original_sha256, receipt.proof_sha256, receipt.mode)


def receipt_payload(receipt: EnvReceipt | None) -> dict[str, str | int] | None:
    return None if receipt is None else receipt.to_payload()


def abort_guard_sha256(attestation: BaselineAttestation) -> str:
    return hashlib.sha256(canonical_json_bytes(attestation.to_payload())).hexdigest()


def _path(value: JsonValue) -> str:
    if not isinstance(value, str) or not value.isascii() or not value.startswith("/") or value != PurePosixPath(value).as_posix() or "//" in value or "/../" in value or "/./" in value:
        raise ValueError("abort guard path is invalid")
    return value


def _hash(value: JsonValue) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None or value == "0" * 64:
        raise ValueError("abort guard hash is invalid")
    return value
