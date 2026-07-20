from __future__ import annotations

import json
from pathlib import Path

import pytest

from dokploy_wizard.proof.model_sync_artifacts import write_protected_manifest
from dokploy_wizard.proof.model_sync_state import (
    AbortGuardError,
    arm_abort_guard,
    atomic_finalize,
    claim_abort_guard,
    disarm_abort_guard,
    read_abort_guard,
    recover_dead_abort_claim,
    transfer_abort_guard_to_plan,
)


def test_guard_claim_transfer(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)

    claim_abort_guard(guard, pid=1234, start_time_ticks="456", claim_token="claim-token")
    transfer_abort_guard_to_plan(guard, claim_token="claim-token")

    status = read_abort_guard(guard)
    assert status.state == "armed"
    assert status.claimant_kind == "plan"
    assert status.claim_token is None


def test_abort_dead_pid_starttime_recovery(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim_abort_guard(guard, pid=1234, start_time_ticks="456", claim_token="claim-token")

    recovered = recover_dead_abort_claim(
        guard,
        process_identity=lambda _pid, _start_time: False,
    )

    status = read_abort_guard(guard)
    assert recovered is True
    assert status.claimant_kind == "plan"


def test_abort_disarm(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)

    disarm_abort_guard(guard)

    assert read_abort_guard(guard).state == "disarmed"


def test_protected_manifest_redacts_and_fsyncs(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    secret = "do-not-persist-this-secret"

    write_protected_manifest(
        manifest,
        {"credential": secret, "credential_value_sha256": "a" * 64, "name": "coder"},
    )

    written = manifest.read_text(encoding="utf-8")
    assert secret not in written
    assert json.loads(written)["credential"] == "<REDACTED>"
    assert manifest.stat().st_mode & 0o777 == 0o600


def test_atomic_finalize_rejects_different_parent_without_output_mutation(tmp_path: Path) -> None:
    temp_parent = tmp_path / "temp"
    output_parent = tmp_path / "output"
    temp_parent.mkdir()
    output_parent.mkdir()
    temp = temp_parent / "result.tmp"
    output = output_parent / "result.json"
    temp.write_text("safe\n", encoding="utf-8")

    with pytest.raises(AbortGuardError):
        atomic_finalize(temp=temp, output=output)

    assert temp.exists()
    assert not output.exists()


def test_confirm_receipt_rejects_malformed_guard_without_mutation(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    guard.write_text('{"state":"armed"}\n', encoding="utf-8")
    before = guard.read_bytes()

    with pytest.raises(AbortGuardError):
        read_abort_guard(guard)

    assert guard.read_bytes() == before
