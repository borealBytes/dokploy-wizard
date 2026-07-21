from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dokploy_wizard.proof.model_sync_artifacts import JsonValue, write_protected_manifest
from dokploy_wizard.proof.model_sync_results import atomic_finalize, build_result
from dokploy_wizard.proof.model_sync_state import (
    AbortGuardError,
    arm_abort_guard,
    claim_abort_guard,
    disarm_abort_guard,
    process_identity_matches,
    process_start_time_ticks,
    read_abort_guard,
    recover_dead_abort_claim,
    transfer_abort_guard_to_plan,
)


def test_guard_claim_transfer(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)

    claim_abort_guard(guard, pid=1234, start_time_ticks="456", claim_token="a" * 32)
    transfer_abort_guard_to_plan(guard, claim_token="a" * 32)

    status = read_abort_guard(guard)
    assert status.state == "armed"
    assert status.claimant_kind == "plan"
    assert status.claim_token is None


def test_abort_dead_pid_starttime_recovery(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim_abort_guard(guard, pid=1234, start_time_ticks="456", claim_token="a" * 32)

    recovered = recover_dead_abort_claim(
        guard,
        process_identity=lambda _pid, _start_time: False,
    )

    status = read_abort_guard(guard)
    assert recovered is True
    assert status.claimant_kind == "plan"


def test_arm_rejects_an_existing_process_claim_without_overwriting_it(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim_abort_guard(guard, pid=1234, start_time_ticks="456", claim_token="a" * 32)
    before = guard.read_bytes()

    with pytest.raises(AbortGuardError):
        arm_abort_guard(guard)

    assert guard.read_bytes() == before


def test_process_claim_rejects_an_invalid_token_without_mutating_guard(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    guard.write_text(
        json.dumps(
            {
                "claim_token": "invalid",
                "claimant_kind": "process",
                "env_receipt": None,
                "pid": 1234,
                "schema_version": 1,
                "start_time_ticks": "456",
                "state": "armed",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    before = guard.read_bytes()

    with pytest.raises(AbortGuardError):
        read_abort_guard(guard)

    assert guard.read_bytes() == before


def test_abort_dead_pid_starttime_recovery_preserves_live_claim_bytes(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim_abort_guard(guard, pid=1234, start_time_ticks="456", claim_token="a" * 32)
    before = guard.read_bytes()

    recovered = recover_dead_abort_claim(
        guard,
        process_identity=lambda pid, start_time: (pid, start_time) == (1234, "456"),
    )

    assert recovered is False
    assert guard.read_bytes() == before


def test_process_identity_rejects_pid_reuse_start_time_mismatch() -> None:
    start_time_ticks = process_start_time_ticks(Path("/proc/self/stat").read_text(encoding="utf-8"))

    assert process_identity_matches(os.getpid(), start_time_ticks)
    assert not process_identity_matches(os.getpid(), "0")


def test_process_identity_parses_field_22_after_a_parenthesized_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dokploy_wizard.proof import model_sync_state

    stat = "123 (cmd with ) spaces) S " + " ".join(str(value) for value in range(4, 53))
    monkeypatch.setattr(model_sync_state.Path, "read_text", lambda _path, **_kwargs: stat)

    assert process_identity_matches(123, "22")


def test_schema_v1_guard_without_receipt_remains_readable(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    guard.write_text(
        json.dumps(
            {
                "claim_token": None,
                "claimant_kind": "plan",
                "pid": None,
                "schema_version": 1,
                "start_time_ticks": None,
                "state": "armed",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    status = read_abort_guard(guard)

    assert status.claimant_kind == "plan"
    assert status.env_receipt is None


def test_atomic_write_removes_sibling_temp_when_write_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dokploy_wizard.proof import model_sync_artifacts

    output = tmp_path / "secret.env"

    def interrupt(_descriptor: int, _content: bytes) -> None:
        raise SystemExit(1)

    monkeypatch.setattr(model_sync_artifacts, "_write_all", interrupt)

    with pytest.raises(SystemExit):
        model_sync_artifacts.atomic_write_bytes(output, b"SECRET-NOT-PERSISTED")

    assert not list(tmp_path.glob(".secret.env.*.tmp"))


def test_disarm_rejects_plan_owned_unresolved_receipt(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim_abort_guard(guard, pid=1234, start_time_ticks="456", claim_token="a" * 32)
    from dokploy_wizard.proof.model_sync_results import EnvReceipt
    from dokploy_wizard.proof.model_sync_state import record_env_receipt

    record_env_receipt(
        guard,
        claim_token="a" * 32,
        receipt=EnvReceipt("/tmp/env", "/tmp/backup", "a" * 64, "b" * 64, 0o600, False),
    )
    transfer_abort_guard_to_plan(guard, claim_token="a" * 32)

    with pytest.raises(AbortGuardError):
        disarm_abort_guard(guard)


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

    with pytest.raises(ValueError):
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


def test_result_accepts_non_placeholder_captured_values() -> None:
    result = build_result(
        {
            "schema_version": 1,
            "source_base_commit": "a" * 40,
            "proof_commit": "b" * 40,
            "coder_image_digest": "ghcr.io/coder/coder@sha256:" + "1" * 64,
            "litellm_image_digest": "ghcr.io/berriai/litellm@sha256:" + "2" * 64,
            "shared_core_image_digests": {
                "pgvector": "pgvector/pgvector@sha256:" + "3" * 64,
                "redis": "redis@sha256:" + "4" * 64,
                "postfix": "postfix@sha256:" + "5" * 64,
                "litellm": "ghcr.io/berriai/litellm@sha256:" + "2" * 64,
            },
            "env_original_sha256": "a" * 64,
            "env_proof_sha256": "b" * 64,
            "env_mode": 384,
            "external_backup_path": "/var/tmp/backup",
            "abort_guard_path": "/tmp/guard",
            "abort_guard_sha256": "c" * 64,
            "host_a_preflight_sha256": "d" * 64,
            "host_b_preflight_sha256": "e" * 64,
            "host_identities_distinct": True,
            "host_architectures_equal": True,
            "baseline_sha256": "f" * 64,
            "protected_artifacts_before_path": "/tmp/manifest",
            "protected_artifacts_before_sha256": "0f" * 32,
            "coder_secret_inventory_sha256": "1" * 64,
            "legacy_workspace_managed_fingerprints_sha256": "2" * 64,
        }
    )

    assert result["coder_image_digest"] == "ghcr.io/coder/coder@sha256:" + "1" * 64


def test_result_rejects_placeholder_and_zero_capture_values() -> None:
    values: dict[str, JsonValue] = {
        "schema_version": 1,
        "source_base_commit": "a" * 40,
        "proof_commit": "b" * 40,
        "coder_image_digest": "unavailable-before-capture",
        "litellm_image_digest": "unavailable-before-capture",
        "shared_core_image_digests": {"pgvector": "", "redis": "", "postfix": "", "litellm": ""},
        "env_original_sha256": "a" * 64,
        "env_proof_sha256": "b" * 64,
        "env_mode": 384,
        "external_backup_path": "/var/tmp/backup",
        "abort_guard_path": "/tmp/guard",
        "abort_guard_sha256": "c" * 64,
        "host_a_preflight_sha256": "d" * 64,
        "host_b_preflight_sha256": "e" * 64,
        "host_identities_distinct": True,
        "host_architectures_equal": True,
        "baseline_sha256": "f" * 64,
        "protected_artifacts_before_path": "/tmp/manifest",
        "protected_artifacts_before_sha256": "0" * 64,
        "coder_secret_inventory_sha256": "0" * 64,
        "legacy_workspace_managed_fingerprints_sha256": "0" * 64,
    }

    with pytest.raises(ValueError):
        build_result(values)
