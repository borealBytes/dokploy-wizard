from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from dokploy_wizard.proof import model_sync_artifacts
from dokploy_wizard.proof.model_sync_artifacts import JsonValue, write_protected_manifest
from dokploy_wizard.proof.model_sync_results import atomic_finalize, build_result
from dokploy_wizard.proof.model_sync_state import (
    AbortGuardError,
    arm_abort_guard,
    begin_rollback,
    claim_abort_guard,
    disarm_abort_guard,
    process_identity_matches,
    process_start_time_ticks,
    read_abort_guard,
    record_env_intent,
    record_proof_active,
    recover_dead_abort_claim,
    transfer_abort_guard_to_plan,
)


def test_guard_claim_transfer(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)

    claim_abort_guard(guard, pid=1234, start_time_ticks="456", claim_token="a" * 32)
    from dokploy_wizard.proof import EnvReceipt

    record_env_intent(
        guard,
        claim_token="a" * 32,
        receipt=EnvReceipt("/tmp/env", "/tmp/backup", "a" * 64, "b" * 64, 0o600),
    )
    record_proof_active(guard, claim_token="a" * 32)
    begin_rollback(guard, claim_token="a" * 32)
    transfer_abort_guard_to_plan(guard, claim_token="a" * 32)

    status = read_abort_guard(guard)
    assert status.state == "armed"
    assert status.phase == "rollback"
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
    stat = "123 (cmd with ) spaces) S " + " ".join(str(value) for value in range(4, 53))
    monkeypatch.setattr(Path, "read_text", lambda _path, **_kwargs: stat)

    assert process_identity_matches(123, "22")


def test_schema_v1_guard_is_rejected_without_mutation(tmp_path: Path) -> None:
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

    with pytest.raises(AbortGuardError):
        read_abort_guard(guard)


def test_schema_v2_guard_rejects_boolean_receipt_mode_without_mutation(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    payload = {
        "attestation": None,
        "claim_token": "a" * 32,
        "claimant_kind": "process",
        "env_receipt": {
            "backup_path": "/tmp/backup",
            "env_path": "/tmp/env",
            "mode": True,
            "original_sha256": "a" * 64,
            "proof_sha256": "b" * 64,
            "schema_version": 1,
        },
        "guard_id": "c" * 64,
        "phase": "env_intent",
        "pid": 1234,
        "schema_version": 3,
        "start_time_ticks": "456",
        "state": "armed",
    }
    guard.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    guard.chmod(0o600)
    before = guard.read_bytes()

    with pytest.raises(AbortGuardError):
        read_abort_guard(guard)

    assert guard.read_bytes() == before


def test_atomic_write_leaves_interrupted_temp_inert_for_fail_closed_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dokploy_wizard.proof import model_sync_artifacts

    output = tmp_path / "secret.env"

    def interrupt(_descriptor: int, _content: bytes) -> None:
        raise SystemExit(1)

    monkeypatch.setattr(model_sync_artifacts, "_write_all", interrupt)

    with pytest.raises(SystemExit):
        model_sync_artifacts.atomic_write_bytes(output, b"SECRET-NOT-PERSISTED")

    temporary = tuple(tmp_path.glob(".secret.env.*.tmp"))
    assert len(temporary) == 1
    assert temporary[0].read_bytes() == b""
    assert not output.exists()


def test_disarm_rejects_plan_owned_unresolved_receipt(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)

    with pytest.raises(AbortGuardError):
        disarm_abort_guard(guard)


def test_abort_disarm_rejects_a_nonterminal_guard(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)

    with pytest.raises(AbortGuardError):
        disarm_abort_guard(guard)


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


@pytest.mark.parametrize(
    "manifest",
    [
        f"{'a' * 64}  ../escape.txt\n",
        f"{'a' * 64}  .omo/evidence/duplicate.txt\n{'a' * 64}  .omo/evidence/duplicate.txt\n",
        f"{'a' * 64}  .sisyphus/z.txt\n{'a' * 64}  .omo/a.txt\n",
        f"{'a' * 64}  .omo/evidence/protected-artifacts-before.txt\n",
        f"{'a' * 64}  docs/outside.txt\n",
        f"{'a' * 64}  .omo/evidence/.manifest.tmp\n",
        f"{'a' * 64}  .omo/evidence/password.txt\n",
        f"{'a' * 64}  .omo/evidence/coder-litellm-model-sync/baseline.json\n",
        f"{'a' * 64}  .omo/run-continuation/session.json\n",
        f"{'a' * 64}  .omo/plans/assets/kdense-central-only.patch\n",
    ],
)
def test_protected_manifest_validation_rejects_unsafe_scope(manifest: str) -> None:
    with pytest.raises(model_sync_artifacts.CaptureSchemaError):
        model_sync_artifacts.validate_protected_manifest_bytes(manifest.encode())


@pytest.mark.parametrize(
    "path",
    [
        ".omo/evidence/x/",
        ".omo/evidence/x//",
        ".omo//evidence/x",
        ".omo/evidence/./x",
        ".sisyphus//evidence/x",
        ".omo/evidence/\\x",
        ".omo/evidence/\x00x",
        ".omo/evidence/\tx",
        ".omo/evidence/\x1fx",
        ".omo/evidence/\x7fx",
        ".omo/evidence/\x85x",
        ".omo/evidence/ x",
        ".omo/evidence/x ",
        ".omo/evidence/x y",
        ".omo/evidence/\u00a0x",
        ".omo/evidence/\u200bx",
    ],
)
def test_protected_manifest_validation_rejects_noncanonical_path_text(path: str) -> None:
    manifest = f"{'a' * 64}  {path}\n".encode()

    with pytest.raises(model_sync_artifacts.CaptureSchemaError):
        model_sync_artifacts.validate_protected_manifest_bytes(manifest)


@pytest.mark.parametrize("alias", [".omo/evidence/x/", ".omo/evidence/x//"])
def test_protected_manifest_validation_rejects_duplicate_normalized_path(
    alias: str,
) -> None:
    manifest = (
        f"{'a' * 64}  .omo/evidence/x\n{'b' * 64}  {alias}\n"
    ).encode()

    with pytest.raises(
        model_sync_artifacts.CaptureSchemaError, match="duplicate normalized path"
    ):
        model_sync_artifacts.validate_protected_manifest_bytes(manifest)


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


def test_atomic_finalize_removes_validated_temp_when_first_fsync_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "SECRET-ATOMIC-FINALIZE-SENTINEL"
    temp = tmp_path / "result.tmp"
    output = tmp_path / "result.json"
    temp.write_text(sentinel, encoding="utf-8")

    def exit_on_first_file_fsync(_descriptor: int) -> None:
        raise SystemExit(73)

    monkeypatch.setattr(os, "fsync", exit_on_first_file_fsync)

    with pytest.raises(SystemExit, match="73"):
        atomic_finalize(temp=temp, output=output)

    captured = capsys.readouterr()
    assert not temp.exists()
    assert not output.exists()
    assert not tuple(tmp_path.iterdir())
    assert sentinel not in captured.out
    assert sentinel not in captured.err


def test_atomic_finalize_fsyncs_output_parent_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp = tmp_path / "result.tmp"
    output = tmp_path / "result.json"
    temp.write_text("safe\n", encoding="utf-8")
    synced: list[int] = []
    original_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        synced.append(descriptor)
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)
    atomic_finalize(temp=temp, output=output)

    assert output.read_text(encoding="utf-8") == "safe\n"
    assert len(synced) == 2


def test_atomic_finalize_rejects_unknown_existing_destination_bytes(tmp_path: Path) -> None:
    temp = tmp_path / "result.tmp"
    output = tmp_path / "result.json"
    temp.write_bytes(b"expected\n")
    output.write_bytes(b"unknown\n")
    os.chmod(output, 0o600)

    with pytest.raises(ValueError, match="existing output does not match"):
        atomic_finalize(temp=temp, output=output)

    assert output.read_bytes() == b"unknown\n"
    assert not temp.exists()


def test_atomic_finalize_accepts_exact_mode_0600_destination_without_rewrite(
    tmp_path: Path,
) -> None:
    temp = tmp_path / "result.tmp"
    output = tmp_path / "result.json"
    temp.write_bytes(b"expected\n")
    output.write_bytes(b"expected\n")
    os.chmod(output, 0o600)
    before = output.stat()

    atomic_finalize(temp=temp, output=output)

    after = output.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert not temp.exists()


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


def test_abort_guard_hash_binds_immutable_attestation_not_lifecycle_bytes(
    tmp_path: Path,
) -> None:
    from dokploy_wizard.proof import (
        BaselineAttestation,
        EnvReceipt,
        canonical_json_bytes,
    )
    from dokploy_wizard.proof.model_sync_results import derive_result_from_attestation
    from dokploy_wizard.proof.model_sync_state import (
        complete_abort_guard,
        record_env_intent,
        record_finalize_intent,
        record_proof_active,
    )

    guard_path = tmp_path / "abort-guard.json"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    guard = arm_abort_guard(guard_path)
    claim_abort_guard(
        guard_path,
        pid=1234,
        start_time_ticks="456",
        claim_token="a" * 32,
    )
    receipt = EnvReceipt(
        env_path=str((tmp_path / "install.env").resolve()),
        backup_path=str((tmp_path / "install.env.backup").resolve()),
        original_sha256="a" * 64,
        proof_sha256="b" * 64,
        mode=0o600,
    )
    record_env_intent(guard_path, claim_token="a" * 32, receipt=receipt)
    record_proof_active(guard_path, claim_token="a" * 32)
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
            "env_mode": 0o600,
            "external_backup_path": receipt.backup_path,
            "abort_guard_path": str(guard_path.resolve()),
            "abort_guard_sha256": "c" * 64,
            "host_a_preflight_sha256": "d" * 64,
            "host_b_preflight_sha256": "e" * 64,
            "host_identities_distinct": True,
            "host_architectures_equal": True,
            "baseline_sha256": "f" * 64,
            "protected_artifacts_before_path": str(
                (artifact_dir / "protected-artifacts-before.txt").resolve()
            ),
            "protected_artifacts_before_sha256": "1" * 64,
            "coder_secret_inventory_sha256": "2" * 64,
            "legacy_workspace_managed_fingerprints_sha256": "3" * 64,
        }
    )
    attestation = BaselineAttestation(
        guard_id=guard.guard_id,
        guard_path=str(guard_path.resolve()),
        artifact_dir=str(artifact_dir.resolve()),
        result_path=str((artifact_dir / "result.json").resolve()),
        env_receipt=receipt,
        output_sha256={
            "baseline.json": "f" * 64,
            "host-a-preflight.json": "d" * 64,
            "host-b-preflight.json": "e" * 64,
            "protected-artifacts-before.txt": "1" * 64,
        },
        result_body={key: value for key, value in result.items() if key != "abort_guard_sha256"},
    )
    record_finalize_intent(guard_path, claim_token="a" * 32, attestation=attestation)
    before_completion = guard_path.read_bytes()

    complete_abort_guard(guard_path, claim_token="a" * 32)
    derived = derive_result_from_attestation(attestation)

    assert guard_path.read_bytes() != before_completion
    assert derived["abort_guard_sha256"] == hashlib.sha256(
        canonical_json_bytes(attestation.to_payload())
    ).hexdigest()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("schema_version"),
        lambda value: value.update({"extra": 1}),
        lambda value: value.update({"mode": True}),
        lambda value: value.update({"env_path": "relative.env"}),
        lambda value: value.update({"original_sha256": "A" * 64}),
    ],
)
def test_env_receipt_v1_rejects_missing_extra_wrong_typed_and_noncanonical_fields(
    mutate: Callable[[dict[str, JsonValue]], JsonValue | None],
) -> None:
    from dokploy_wizard.proof import parse_env_receipt

    receipt: dict[str, JsonValue] = {
        "schema_version": 1,
        "env_path": "/tmp/env",
        "backup_path": "/tmp/backup",
        "original_sha256": "a" * 64,
        "proof_sha256": "b" * 64,
        "mode": 0o600,
    }
    mutate(receipt)

    with pytest.raises(ValueError):
        parse_env_receipt(receipt)


def test_guard_v3_rejects_extra_key_without_mutating_bytes(tmp_path: Path) -> None:
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    payload = json.loads(guard.read_text(encoding="utf-8"))
    payload["extra"] = "rejected"
    guard.write_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        + b"\n"
    )
    guard.chmod(0o600)
    before = guard.read_bytes()

    with pytest.raises(AbortGuardError):
        read_abort_guard(guard)

    assert guard.read_bytes() == before
    record_env_intent,
    record_proof_active,
