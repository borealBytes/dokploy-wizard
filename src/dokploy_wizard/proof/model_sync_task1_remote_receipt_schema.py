"""Closed value-free schema for one Task 1 remote proof execution."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

from dokploy_wizard.proof import JsonValue
from dokploy_wizard.proof.model_sync_task1_remote_receipt_hashing import (
    COMMIT_PATTERN,
    binding_sha256,
    canonical_receipt_bytes,
    require_sha256,
    stage_chain_sha256,
)
from dokploy_wizard.proof.model_sync_task1_remote_receipt_schema_types import (
    TASK1_REMOTE_PROOF_STAGES,
    Task1RemoteProofBinding,
    Task1RemoteProofResult,
    Task1RemoteProofStage,
    Task1RemoteProofStageRecord,
    Task1RemoteReceiptError,
)


@dataclass(frozen=True, slots=True)
class Task1RemoteProofReceipt:
    binding: Task1RemoteProofBinding
    result: Task1RemoteProofResult
    stages: tuple[Task1RemoteProofStageRecord, ...]

    def to_bytes(self) -> bytes:
        payload: dict[str, JsonValue] = {
            "archive_sha256": self.binding.archive_sha256,
            "command_mode": self.binding.command_mode,
            "context_sha256": self.binding.context_sha256,
            "expected_terminal_stage": self.binding.expected_terminal_stage,
            "proof_commit": self.binding.proof_commit,
            "result": str(self.result),
            "schema_version": 1,
            "stages": [
                {
                    "chain_sha256": record.chain_sha256,
                    "evidence_sha256": record.evidence_sha256,
                    "sequence": record.sequence,
                    "stage": str(record.stage),
                }
                for record in self.stages
            ],
            "uploaded_env_sha256": self.binding.uploaded_env_sha256,
        }
        return canonical_receipt_bytes(payload)

    @classmethod
    def from_bytes(cls, content: bytes) -> Task1RemoteProofReceipt:
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Task1RemoteReceiptError(
                "Task 1 remote proof receipt is not valid JSON"
            ) from error
        expected = {
            "archive_sha256",
            "command_mode",
            "context_sha256",
            "expected_terminal_stage",
            "proof_commit",
            "result",
            "schema_version",
            "stages",
            "uploaded_env_sha256",
        }
        if not isinstance(value, dict) or set(value) != expected or value["schema_version"] != 1:
            raise Task1RemoteReceiptError("Task 1 remote proof receipt schema is invalid")
        try:
            binding = Task1RemoteProofBinding(
                proof_commit=value["proof_commit"],
                context_sha256=value["context_sha256"],
                uploaded_env_sha256=value["uploaded_env_sha256"],
                archive_sha256=value["archive_sha256"],
                command_mode=value["command_mode"],
                expected_terminal_stage=value["expected_terminal_stage"],
            )
            result = Task1RemoteProofResult(value["result"])
            raw_stages = value["stages"]
            if not isinstance(raw_stages, list):
                raise Task1RemoteReceiptError("Task 1 remote proof receipt stages are invalid")
            records = tuple(_parse_stage_record(item) for item in raw_stages)
        except (KeyError, TypeError, ValueError) as error:
            raise Task1RemoteReceiptError(
                "Task 1 remote proof receipt fields are invalid"
            ) from error
        receipt = cls(binding=binding, result=result, stages=records)
        _validate_receipt(receipt)
        if content != receipt.to_bytes():
            raise Task1RemoteReceiptError("Task 1 remote proof receipt is not canonical")
        return receipt


def new_receipt(
    binding: Task1RemoteProofBinding,
    evidence: tuple[tuple[Task1RemoteProofStage, str], ...],
) -> Task1RemoteProofReceipt:
    receipt = Task1RemoteProofReceipt(binding, Task1RemoteProofResult.PENDING, ())
    _validate_receipt(receipt)
    for stage, evidence_sha256 in evidence:
        receipt = append_stage(receipt, stage, evidence_sha256)
    return receipt


def append_stage(
    receipt: Task1RemoteProofReceipt,
    stage: Task1RemoteProofStage,
    evidence_sha256: str,
) -> Task1RemoteProofReceipt:
    if receipt.result is not Task1RemoteProofResult.PENDING:
        raise Task1RemoteReceiptError("Task 1 remote proof receipt is already terminal")
    expected_index = len(receipt.stages)
    if expected_index >= len(TASK1_REMOTE_PROOF_STAGES):
        raise Task1RemoteReceiptError("Task 1 remote proof receipt has duplicate stages")
    if stage is not TASK1_REMOTE_PROOF_STAGES[expected_index]:
        raise Task1RemoteReceiptError("Task 1 remote proof receipt stage order is invalid")
    require_sha256(evidence_sha256)
    previous = binding_sha256(receipt.binding)
    if receipt.stages:
        previous = receipt.stages[-1].chain_sha256
    sequence = expected_index + 1
    chain_sha256 = stage_chain_sha256(previous, sequence, stage, evidence_sha256)
    return replace(
        receipt,
        stages=(
            *receipt.stages,
            Task1RemoteProofStageRecord(sequence, stage, evidence_sha256, chain_sha256),
        ),
    )


def complete_receipt(receipt: Task1RemoteProofReceipt) -> Task1RemoteProofReceipt:
    if receipt.result is Task1RemoteProofResult.SUCCESS:
        return receipt
    if tuple(record.stage for record in receipt.stages) != TASK1_REMOTE_PROOF_STAGES:
        raise Task1RemoteReceiptError("Task 1 remote proof receipt is incomplete")
    return replace(receipt, result=Task1RemoteProofResult.SUCCESS)


def _parse_stage_record(value: JsonValue) -> Task1RemoteProofStageRecord:
    expected = {"chain_sha256", "evidence_sha256", "sequence", "stage"}
    if not isinstance(value, dict) or set(value) != expected:
        raise Task1RemoteReceiptError("Task 1 remote proof receipt stage schema is invalid")
    sequence = value["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise Task1RemoteReceiptError("Task 1 remote proof receipt stage sequence is invalid")
    stage = value["stage"]
    evidence = value["evidence_sha256"]
    chain = value["chain_sha256"]
    if not isinstance(stage, str) or not isinstance(evidence, str) or not isinstance(chain, str):
        raise Task1RemoteReceiptError("Task 1 remote proof receipt stage fields are invalid")
    return Task1RemoteProofStageRecord(sequence, Task1RemoteProofStage(stage), evidence, chain)


def _validate_receipt(receipt: Task1RemoteProofReceipt) -> None:
    binding = receipt.binding
    if COMMIT_PATTERN.fullmatch(binding.proof_commit) is None:
        raise Task1RemoteReceiptError("Task 1 remote proof commit is invalid")
    for digest in (
        binding.context_sha256,
        binding.uploaded_env_sha256,
        binding.archive_sha256,
    ):
        require_sha256(digest)
    if binding.command_mode != "proof" or binding.expected_terminal_stage != "collect":
        raise Task1RemoteReceiptError("Task 1 remote proof receipt mode is invalid")
    if len(receipt.stages) > len(TASK1_REMOTE_PROOF_STAGES):
        raise Task1RemoteReceiptError("Task 1 remote proof receipt has too many stages")
    previous = binding_sha256(binding)
    for expected_sequence, (record, expected_stage) in enumerate(
        zip(receipt.stages, TASK1_REMOTE_PROOF_STAGES, strict=False), start=1
    ):
        require_sha256(record.evidence_sha256)
        require_sha256(record.chain_sha256)
        if record.sequence != expected_sequence or record.stage is not expected_stage:
            raise Task1RemoteReceiptError("Task 1 remote proof receipt stage order is invalid")
        expected_chain = stage_chain_sha256(
            previous, record.sequence, record.stage, record.evidence_sha256
        )
        if record.chain_sha256 != expected_chain:
            raise Task1RemoteReceiptError("Task 1 remote proof receipt chain is invalid")
        previous = record.chain_sha256
    if receipt.result is Task1RemoteProofResult.SUCCESS and len(receipt.stages) != len(
        TASK1_REMOTE_PROOF_STAGES
    ):
        raise Task1RemoteReceiptError("Task 1 remote proof success is incomplete")
