from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard.proof import (
    BaselineAttestation,
    EnvReceipt,
    atomic_finalize,
    canonical_json_bytes,
    model_sync_artifacts,
    parse_baseline_attestation,
)
from dokploy_wizard.proof.model_sync_artifacts import JsonValue, write_protected_manifest
from dokploy_wizard.proof.model_sync_results import (
    REQUIRED_RESULT_KEYS,
    build_result,
    result_bytes_from_attestation,
    validate_attestation,
    verify_result_bytes,
)
from dokploy_wizard.proof.model_sync_state import (
    AbortGuardError,
    arm_abort_guard,
    begin_rollback,
    claim_abort_guard,
    complete_abort_guard,
    disarm_abort_guard,
    process_identity_matches,
    process_start_time_ticks,
    read_abort_guard,
    record_env_intent,
    record_finalize_intent,
    record_proof_active,
    recover_dead_abort_claim,
    transfer_abort_guard_to_plan,
)


def _valid_result_values(tmp_path: Path, receipt: EnvReceipt) -> dict[str, JsonValue]:
    artifact_dir = tmp_path / "artifacts"
    return {
        "schema_version": 2,
        "host_identity_mode": "distinct",
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
        "env_original_sha256": receipt.original_sha256,
        "env_proof_sha256": receipt.proof_sha256,
        "env_mode": receipt.mode,
        "external_backup_path": receipt.backup_path,
        "abort_guard_path": str((tmp_path / "abort-guard.json").resolve()),
        "abort_guard_sha256": "6" * 64,
        "host_a_preflight_sha256": "d" * 64,
        "host_b_preflight_sha256": "e" * 64,
        "host_identities_distinct": True,
        "host_architectures_equal": True,
        "single_host_lifecycle_path": None,
        "single_host_lifecycle_sha256": None,
        "temporal_clean_epoch_evidence": False,
        "baseline_sha256": "f" * 64,
        "protected_artifacts_before_path": str(
            (artifact_dir / "protected-artifacts-before.txt").resolve()
        ),
        "protected_artifacts_before_sha256": "7" * 64,
        "coder_secret_inventory_sha256": "8" * 64,
        "legacy_workspace_managed_fingerprints_sha256": "9" * 64,
        "preexisting_cloudflare_sha256": "a" * 64,
        "post_install_cloudflare_sha256": "b" * 64,
    }


def _valid_attestation(tmp_path: Path, guard_id: str = "c" * 64) -> BaselineAttestation:
    receipt = EnvReceipt(
        str((tmp_path / "install.env").resolve()),
        str((tmp_path / "install.env.backup").resolve()),
        "a" * 64,
        "b" * 64,
        0o600,
    )
    result = build_result(_valid_result_values(tmp_path, receipt))
    artifact_dir = tmp_path / "artifacts"
    return BaselineAttestation(
        guard_id,
        str((tmp_path / "abort-guard.json").resolve()),
        str(artifact_dir.resolve()),
        str((artifact_dir / "result.json").resolve()),
        receipt,
        "distinct",
        "b" * 64,
        {
            "baseline.json": "f" * 64,
            "host-a-preflight.json": "d" * 64,
            "host-b-preflight.json": "e" * 64,
            "protected-artifacts-before.txt": "7" * 64,
        },
        {key: value for key, value in result.items() if key != "abort_guard_sha256"},
    )


def _write_guard_payload(path: Path, payload: dict[str, JsonValue]) -> None:
    path.write_bytes(canonical_json_bytes(payload) + b"\n")
    path.chmod(0o600)


@pytest.mark.parametrize("post_hash", [None, "A" * 64, "not-a-sha256"])
def test_result_rejects_missing_or_malformed_post_install_cloudflare_hash(
    tmp_path: Path, post_hash: str | None
) -> None:
    # Given
    receipt = EnvReceipt(
        str((tmp_path / "install.env").resolve()),
        str((tmp_path / "install.env.backup").resolve()),
        "a" * 64,
        "b" * 64,
        0o600,
    )
    result = _valid_result_values(tmp_path, receipt)
    if post_hash is None:
        result.pop("post_install_cloudflare_sha256", None)
    else:
        result["post_install_cloudflare_sha256"] = post_hash

    # When / Then
    with pytest.raises(ValueError):
        build_result(result)


def _rollback_payload(
    tmp_path: Path,
    *,
    claimant: str,
    include_receipt: bool,
    include_attestation: bool,
) -> dict[str, JsonValue]:
    attestation = _valid_attestation(tmp_path)
    process = claimant == "process"
    return {
        "attestation": attestation.to_payload() if include_attestation else None,
        "claim_token": "t" * 32 if process else None,
        "claimant_kind": claimant,
        "env_receipt": attestation.env_receipt.to_payload() if include_receipt else None,
        "guard_id": attestation.guard_id,
        "phase": "rollback",
        "pid": 1234 if process else None,
        "schema_version": 3,
        "start_time_ticks": "456" if process else None,
        "state": "armed",
    }


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
            "schema_version": 2,
            "host_identity_mode": "distinct",
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


def test_atomic_write_removes_interrupted_temp_and_preserves_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "secret.env"
    output.write_bytes(b"ORIGINAL")
    output.chmod(0o600)

    def interrupt(_descriptor: int, _content: bytes) -> None:
        raise SystemExit(1)

    monkeypatch.setattr(model_sync_artifacts, "_write_all", interrupt)

    with pytest.raises(SystemExit):
        model_sync_artifacts.atomic_write_bytes(output, b"SECRET-NOT-PERSISTED")

    assert output.read_bytes() == b"ORIGINAL"
    assert not tuple(tmp_path.glob(".secret.env.*.tmp"))


@pytest.mark.parametrize("boundary", ["fsync", "replace"])
def test_atomic_write_removes_temp_for_catchable_commit_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    output = tmp_path / "artifact.json"
    output.write_bytes(b"original")
    output.chmod(0o600)

    def interrupt_fsync(_descriptor: int) -> None:
        raise SystemExit(73)

    def interrupt_replace(_source: Path, _destination: Path) -> None:
        raise SystemExit(73)

    match boundary:
        case "fsync":
            monkeypatch.setattr(os, "fsync", interrupt_fsync)
        case "replace":
            monkeypatch.setattr(os, "replace", interrupt_replace)
        case unexpected:
            raise AssertionError(f"unexpected boundary {unexpected}")

    with pytest.raises(SystemExit, match="73"):
        model_sync_artifacts.atomic_write_bytes(output, b"replacement")

    assert output.read_bytes() == b"original"
    assert not tuple(tmp_path.glob(".artifact.json.*.tmp"))


def test_atomic_write_cleanup_failure_does_not_mask_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "artifact.json"

    def interrupt(_descriptor: int, _content: bytes) -> None:
        raise SystemExit(29)

    def reject_cleanup(_path: Path, *, missing_ok: bool = False) -> None:
        del missing_ok
        raise OSError("cleanup failed")

    monkeypatch.setattr(model_sync_artifacts, "_write_all", interrupt)
    monkeypatch.setattr(Path, "unlink", reject_cleanup)

    with pytest.raises(SystemExit, match="29"):
        model_sync_artifacts.atomic_write_bytes(output, b"replacement")


def test_atomic_write_fsyncs_file_before_replace_and_parent_afterward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "artifact.json"
    events: list[str] = []
    original_fsync = os.fsync
    original_replace = os.replace

    def record_fsync(descriptor: int) -> None:
        kind = "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        events.append(kind)
        original_fsync(descriptor)

    def record_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        original_replace(source, destination)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "replace", record_replace)

    model_sync_artifacts.atomic_write_bytes(output, b"expected")

    assert events == ["file", "replace", "directory"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_exact_output_inspection_tolerates_short_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = b"short-read-safe"
    output = tmp_path / "artifact.json"
    output.write_bytes(expected)
    output.chmod(0o600)
    original_read = os.read
    requests: list[int] = []

    def short_read(descriptor: int, size: int) -> bytes:
        requests.append(size)
        return original_read(descriptor, min(size, 2))

    monkeypatch.setattr(os, "read", short_read)

    assert model_sync_artifacts.read_exact_regular_bytes(output, expected)
    assert len(requests) > 2
    assert max(requests) <= len(expected) + 1


def test_exact_output_inspection_rejects_trailing_bytes(
    tmp_path: Path,
) -> None:
    output = tmp_path / "artifact.json"
    output.write_bytes(b"expected-trailing")
    output.chmod(0o600)

    with pytest.raises(model_sync_artifacts.CaptureSchemaError):
        model_sync_artifacts.read_exact_regular_bytes(output, b"expected")


def test_exact_output_inspection_bounds_oversize_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = b"ok"
    output = tmp_path / "artifact.json"
    with output.open("wb") as stream:
        stream.write(expected)
        stream.truncate(1024 * 1024)
    output.chmod(0o600)
    original_read = os.read
    requests: list[int] = []

    def bounded_read(descriptor: int, size: int) -> bytes:
        requests.append(size)
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", bounded_read)

    with pytest.raises(model_sync_artifacts.CaptureSchemaError):
        model_sync_artifacts.read_exact_regular_bytes(output, expected)

    assert not requests or max(requests) <= len(expected) + 1


def test_exact_output_inspection_rejects_path_replacement_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = b"expected"
    output = tmp_path / "artifact.json"
    replacement = tmp_path / "replacement.json"
    output.write_bytes(expected)
    output.chmod(0o600)
    replacement.write_bytes(expected)
    replacement.chmod(0o600)
    original_read = os.read
    replaced = False

    def replace_path(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            os.replace(replacement, output)
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", replace_path)

    with pytest.raises(model_sync_artifacts.CaptureSchemaError):
        model_sync_artifacts.read_exact_regular_bytes(output, expected)

    assert output.read_bytes() == expected


def test_exact_output_inspection_rejects_metadata_change_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = b"expected"
    output = tmp_path / "artifact.json"
    output.write_bytes(expected)
    output.chmod(0o600)
    original_read = os.read
    changed = False

    def change_metadata(descriptor: int, size: int) -> bytes:
        nonlocal changed
        content = original_read(descriptor, size)
        if not changed:
            changed = True
            metadata = output.stat()
            os.utime(output, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000))
        return content

    monkeypatch.setattr(os, "read", change_metadata)

    with pytest.raises(model_sync_artifacts.CaptureSchemaError):
        model_sync_artifacts.read_exact_regular_bytes(output, expected)


def test_exact_output_inspection_rejects_growth_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = b"expected"
    output = tmp_path / "artifact.json"
    output.write_bytes(expected)
    output.chmod(0o600)
    original_read = os.read
    grown = False

    def grow_after_read(descriptor: int, size: int) -> bytes:
        nonlocal grown
        content = original_read(descriptor, size)
        if not grown:
            grown = True
            with output.open("ab") as stream:
                stream.write(b"trailing")
        return content

    monkeypatch.setattr(os, "read", grow_after_read)

    with pytest.raises(model_sync_artifacts.CaptureSchemaError):
        model_sync_artifacts.read_exact_regular_bytes(output, expected)


@pytest.mark.parametrize("kind", ["mode", "special-mode", "symlink", "directory", "fifo"])
def test_exact_output_inspection_rejects_unauthorized_file_kinds(tmp_path: Path, kind: str) -> None:
    output = tmp_path / "artifact.json"
    match kind:
        case "mode":
            output.write_bytes(b"expected")
            output.chmod(0o640)
        case "special-mode":
            output.write_bytes(b"expected")
            output.chmod(0o4600)
        case "symlink":
            target = tmp_path / "target.json"
            target.write_bytes(b"expected")
            target.chmod(0o600)
            output.symlink_to(target.name)
        case "directory":
            output.mkdir()
            output.chmod(0o600)
        case "fifo":
            os.mkfifo(output, mode=0o600)
        case unexpected:
            raise AssertionError(f"unexpected kind {unexpected}")

    with pytest.raises(model_sync_artifacts.CaptureSchemaError):
        model_sync_artifacts.read_exact_regular_bytes(output, b"expected")


def test_exact_output_unlink_preserves_replacement_raced_after_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = b"expected"
    unknown = b"unknown-replacement"
    output = tmp_path / "artifact.json"
    output.write_bytes(expected)
    output.chmod(0o600)
    original_unlink = os.unlink
    replaced = False

    def replace_before_unlink(
        path: str | bytes | Path,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            output.write_bytes(unknown)
            output.chmod(0o600)
        if dir_fd is None:
            original_unlink(path)
        else:
            original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", replace_before_unlink)

    model_sync_artifacts.unlink_exact_regular_bytes(output, expected)

    assert output.read_bytes() == unknown


def test_exact_output_unlink_restores_replacement_raced_before_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = b"expected"
    unknown = b"unknown-replacement"
    output = tmp_path / "artifact.json"
    replacement = tmp_path / "replacement.json"
    output.write_bytes(expected)
    output.chmod(0o600)
    replacement.write_bytes(unknown)
    replacement.chmod(0o600)
    original_rename = os.rename
    replaced = False

    def replace_before_rename(
        source: str | bytes,
        destination: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            os.replace(replacement, output)
        original_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "rename", replace_before_rename)

    with pytest.raises(model_sync_artifacts.CaptureSchemaError):
        model_sync_artifacts.unlink_exact_regular_bytes(output, expected)

    assert output.read_bytes() == unknown
    assert not tuple(tmp_path.glob(".artifact.json.*.unlink"))


def test_exact_output_helpers_preserve_unknown_bytes(tmp_path: Path) -> None:
    output = tmp_path / "artifact.json"
    output.write_bytes(b"unknown")
    output.chmod(0o600)

    with pytest.raises(model_sync_artifacts.CaptureSchemaError):
        model_sync_artifacts.write_or_verify_exact_bytes(output, b"expected")
    with pytest.raises(model_sync_artifacts.CaptureSchemaError):
        model_sync_artifacts.unlink_exact_regular_bytes(output, b"expected")

    assert output.read_bytes() == b"unknown"


def test_exact_output_writer_preserves_unknown_bytes_raced_into_absent_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = b"expected"
    unknown = b"unknown-replacement"
    output = tmp_path / "artifact.json"
    original_link = os.link
    original_replace = os.replace
    raced = False

    def inject_unknown(destination: Path) -> None:
        nonlocal raced
        if not raced and Path(destination) == output:
            raced = True
            output.write_bytes(unknown)
            output.chmod(0o600)

    def race_replace(source: Path, destination: Path) -> None:
        inject_unknown(destination)
        original_replace(source, destination)

    def race_link(
        source: Path,
        destination: Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        inject_unknown(destination)
        original_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "replace", race_replace)
    monkeypatch.setattr(os, "link", race_link)

    with pytest.raises(model_sync_artifacts.CaptureSchemaError):
        model_sync_artifacts.write_or_verify_exact_bytes(output, expected)

    assert output.read_bytes() == unknown


def test_exact_output_unlink_fsyncs_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "artifact.json"
    output.write_bytes(b"expected")
    output.chmod(0o600)
    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", synced.append)

    model_sync_artifacts.unlink_exact_regular_bytes(output, b"expected")

    assert len(synced) == 1


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
    manifest = (f"{'a' * 64}  .omo/evidence/x\n{'b' * 64}  {alias}\n").encode()

    with pytest.raises(model_sync_artifacts.CaptureSchemaError, match="duplicate normalized path"):
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
            "schema_version": 2,
            "host_identity_mode": "distinct",
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
            "single_host_lifecycle_path": None,
            "single_host_lifecycle_sha256": None,
            "temporal_clean_epoch_evidence": False,
            "baseline_sha256": "f" * 64,
            "protected_artifacts_before_path": "/tmp/manifest",
            "protected_artifacts_before_sha256": "0f" * 32,
            "coder_secret_inventory_sha256": "1" * 64,
            "legacy_workspace_managed_fingerprints_sha256": "2" * 64,
                "preexisting_cloudflare_sha256": "3" * 64,
                "post_install_cloudflare_sha256": "4" * 64,
        }
    )

    assert result["coder_image_digest"] == "ghcr.io/coder/coder@sha256:" + "1" * 64


def test_result_rejects_placeholder_and_zero_capture_values() -> None:
    values: dict[str, JsonValue] = {
        "schema_version": 2,
        "host_identity_mode": "distinct",
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
        "single_host_lifecycle_path": None,
        "single_host_lifecycle_sha256": None,
        "temporal_clean_epoch_evidence": False,
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
    from dokploy_wizard.proof.model_sync_results import derive_result_from_attestation

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
            "schema_version": 2,
            "host_identity_mode": "distinct",
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
            "single_host_lifecycle_path": None,
            "single_host_lifecycle_sha256": None,
            "temporal_clean_epoch_evidence": False,
            "baseline_sha256": "f" * 64,
            "protected_artifacts_before_path": str(
                (artifact_dir / "protected-artifacts-before.txt").resolve()
            ),
            "protected_artifacts_before_sha256": "1" * 64,
            "coder_secret_inventory_sha256": "2" * 64,
            "legacy_workspace_managed_fingerprints_sha256": "3" * 64,
                "preexisting_cloudflare_sha256": "4" * 64,
                "post_install_cloudflare_sha256": "5" * 64,
        }
    )
    attestation = BaselineAttestation(
        guard_id=guard.guard_id,
        guard_path=str(guard_path.resolve()),
        artifact_dir=str(artifact_dir.resolve()),
        result_path=str((artifact_dir / "result.json").resolve()),
            env_receipt=receipt,
            host_identity_mode="distinct",
            post_install_cloudflare_sha256="5" * 64,
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
    assert (
        derived["abort_guard_sha256"]
        == hashlib.sha256(canonical_json_bytes(attestation.to_payload())).hexdigest()
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("schema_version"),
        lambda value: value.update({"extra": 1}),
        lambda value: value.update({"mode": True}),
        lambda value: value.update({"mode": 0o1600}),
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


def test_guard_read_tolerates_short_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard_path = tmp_path / "abort-guard.json"
    expected = arm_abort_guard(guard_path)
    original_read = os.read
    requests: list[int] = []

    def short_read(descriptor: int, size: int) -> bytes:
        requests.append(size)
        return original_read(descriptor, min(size, 2))

    monkeypatch.setattr(os, "read", short_read)

    assert read_abort_guard(guard_path) == expected
    assert len(requests) > 2


def test_guard_read_rejects_growth_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_path = tmp_path / "abort-guard.json"
    arm_abort_guard(guard_path)
    original_read = os.read
    grown = False

    def grow_after_read(descriptor: int, size: int) -> bytes:
        nonlocal grown
        content = original_read(descriptor, size)
        if not grown:
            grown = True
            with guard_path.open("ab") as stream:
                stream.write(b"trailing")
        return content

    monkeypatch.setattr(os, "read", grow_after_read)

    with pytest.raises(AbortGuardError):
        read_abort_guard(guard_path)


def test_guard_read_rejects_path_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_path = tmp_path / "abort-guard.json"
    replacement = tmp_path / "replacement.json"
    arm_abort_guard(guard_path)
    replacement.write_bytes(guard_path.read_bytes())
    replacement.chmod(0o600)
    original_read = os.read
    replaced = False

    def replace_path(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            os.replace(replacement, guard_path)
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", replace_path)

    with pytest.raises(AbortGuardError):
        read_abort_guard(guard_path)


def test_guard_read_rejects_descriptor_metadata_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_path = tmp_path / "abort-guard.json"
    arm_abort_guard(guard_path)
    original_read = os.read
    changed = False

    def change_metadata(descriptor: int, size: int) -> bytes:
        nonlocal changed
        content = original_read(descriptor, size)
        if not changed:
            changed = True
            metadata = os.fstat(descriptor)
            os.utime(
                descriptor,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000),
            )
        return content

    monkeypatch.setattr(os, "read", change_metadata)

    with pytest.raises(AbortGuardError):
        read_abort_guard(guard_path)


def test_guard_read_rejects_oversize_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_path = tmp_path / "abort-guard.json"
    arm_abort_guard(guard_path)
    with guard_path.open("r+b") as stream:
        stream.truncate(1024 * 1024)
    requests: list[int] = []
    original_read = os.read

    def record_read(descriptor: int, size: int) -> bytes:
        requests.append(size)
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", record_read)

    with pytest.raises(AbortGuardError):
        read_abort_guard(guard_path)

    assert requests == []


@pytest.mark.parametrize("kind", ["trailing", "nan", "mode", "special-mode", "symlink"])
def test_guard_read_rejects_noncanonical_or_unauthorized_file(
    tmp_path: Path,
    kind: str,
) -> None:
    guard_path = tmp_path / "abort-guard.json"
    arm_abort_guard(guard_path)
    match kind:
        case "trailing":
            with guard_path.open("ab") as stream:
                stream.write(b" ")
        case "nan":
            guard_path.write_bytes(guard_path.read_bytes().replace(b'"pid":null', b'"pid":NaN'))
        case "mode":
            guard_path.chmod(0o640)
        case "special-mode":
            guard_path.chmod(0o4600)
        case "symlink":
            target = tmp_path / "target.json"
            os.replace(guard_path, target)
            guard_path.symlink_to(target.name)
        case unexpected:
            raise AssertionError(f"unexpected guard file kind {unexpected}")

    with pytest.raises(AbortGuardError):
        read_abort_guard(guard_path)


@pytest.mark.parametrize(
    ("claimant", "include_receipt", "include_attestation"),
    [
        ("plan", False, False),
        ("plan", True, False),
        ("plan", True, True),
        ("process", False, False),
        ("process", True, False),
        ("process", True, True),
    ],
)
def test_rollback_accepts_only_explicit_legal_combinations(
    tmp_path: Path,
    claimant: str,
    include_receipt: bool,
    include_attestation: bool,
) -> None:
    guard_path = tmp_path / "abort-guard.json"
    _write_guard_payload(
        guard_path,
        _rollback_payload(
            tmp_path,
            claimant=claimant,
            include_receipt=include_receipt,
            include_attestation=include_attestation,
        ),
    )

    guard = read_abort_guard(guard_path)

    assert guard.phase == "rollback"
    assert guard.claimant_kind == claimant


def test_rollback_rejects_attestation_without_receipt(tmp_path: Path) -> None:
    guard_path = tmp_path / "abort-guard.json"
    payload = _rollback_payload(
        tmp_path,
        claimant="plan",
        include_receipt=False,
        include_attestation=True,
    )
    _write_guard_payload(guard_path, payload)

    with pytest.raises(AbortGuardError):
        read_abort_guard(guard_path)


def test_rollback_rejects_mismatched_guard_id(tmp_path: Path) -> None:
    guard_path = tmp_path / "abort-guard.json"
    payload = _rollback_payload(
        tmp_path,
        claimant="plan",
        include_receipt=True,
        include_attestation=True,
    )
    payload["guard_id"] = "d" * 64
    _write_guard_payload(guard_path, payload)

    with pytest.raises(AbortGuardError):
        read_abort_guard(guard_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("env_path", "/tmp/other.env"),
        ("backup_path", "/tmp/other.backup"),
        ("original_sha256", "d" * 64),
        ("proof_sha256", "e" * 64),
        ("mode", 0o640),
    ],
)
def test_rollback_rejects_each_mismatched_receipt_identity_field(
    tmp_path: Path,
    field: str,
    value: JsonValue,
) -> None:
    guard_path = tmp_path / "abort-guard.json"
    payload = _rollback_payload(
        tmp_path,
        claimant="plan",
        include_receipt=True,
        include_attestation=True,
    )
    receipt = payload["env_receipt"]
    assert isinstance(receipt, dict)
    receipt[field] = value
    _write_guard_payload(guard_path, payload)

    with pytest.raises(AbortGuardError):
        read_abort_guard(guard_path)


@pytest.mark.parametrize(
    ("claimant", "state", "pid", "start_time", "token"),
    [
        ("plan", "armed", 1234, None, None),
        ("process", "armed", None, None, None),
        ("process", "armed", True, "456", "t" * 32),
        ("plan", "disarmed", None, None, None),
    ],
)
def test_rollback_rejects_illegal_owner_state_and_process_fields(
    tmp_path: Path,
    claimant: str,
    state: str,
    pid: int | bool | None,
    start_time: str | None,
    token: str | None,
) -> None:
    guard_path = tmp_path / "abort-guard.json"
    payload = _rollback_payload(
        tmp_path,
        claimant=claimant,
        include_receipt=False,
        include_attestation=False,
    )
    payload.update(
        {"claim_token": token, "pid": pid, "start_time_ticks": start_time, "state": state}
    )
    _write_guard_payload(guard_path, payload)

    with pytest.raises(AbortGuardError):
        read_abort_guard(guard_path)


def test_finalize_intent_and_terminal_guard_remain_valid(tmp_path: Path) -> None:
    guard_path = tmp_path / "abort-guard.json"
    guard = arm_abort_guard(guard_path)
    claim_abort_guard(guard_path, pid=1234, start_time_ticks="456", claim_token="t" * 32)
    attestation = _valid_attestation(tmp_path, guard.guard_id)
    record_env_intent(
        guard_path,
        claim_token="t" * 32,
        receipt=attestation.env_receipt,
    )
    record_proof_active(guard_path, claim_token="t" * 32)
    record_finalize_intent(
        guard_path,
        claim_token="t" * 32,
        attestation=attestation,
    )

    assert read_abort_guard(guard_path).phase == "finalize_intent"

    complete_abort_guard(guard_path, claim_token="t" * 32)

    assert read_abort_guard(guard_path).phase == "complete"


def test_baseline_attestation_round_trip_preserves_exact_canonical_bytes(
    tmp_path: Path,
) -> None:
    attestation = _valid_attestation(tmp_path)
    raw = canonical_json_bytes(attestation.to_payload())

    parsed = parse_baseline_attestation(json.loads(raw))

    assert parsed == attestation
    assert parsed is not None
    assert canonical_json_bytes(parsed.to_payload()) == raw


def test_baseline_attestation_v2_rejects_missing_and_extra_keys(tmp_path: Path) -> None:
    payload = _valid_attestation(tmp_path).to_payload()
    missing = dict(payload)
    missing.pop("result_path")
    extra = dict(payload)
    extra["extra"] = None

    with pytest.raises(ValueError):
        parse_baseline_attestation(missing)
    with pytest.raises(ValueError):
        parse_baseline_attestation(extra)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("kind", 1),
        ("host_identity_mode", "single_sequential"),
        ("guard_id", 1),
        ("guard_path", "relative/guard.json"),
        ("artifact_dir", "relative/artifacts"),
        ("result_path", 1),
        ("env_receipt", None),
        ("required_terminal", []),
        ("output_sha256", []),
        ("result_body", []),
    ],
)
def test_baseline_attestation_v2_rejects_wrong_types(
    tmp_path: Path,
    field: str,
    value: JsonValue,
) -> None:
    payload = _valid_attestation(tmp_path).to_payload()
    payload[field] = value

    with pytest.raises(ValueError):
        parse_baseline_attestation(payload)


@pytest.mark.parametrize("integer_field", ["schema_version", "env_mode"])
def test_result_rejects_boolean_integer_fields(tmp_path: Path, integer_field: str) -> None:
    attestation = _valid_attestation(tmp_path)
    values = {**attestation.result_body, "abort_guard_sha256": "6" * 64}
    values[integer_field] = True

    with pytest.raises(ValueError):
        build_result(values)


def test_attestation_result_body_keys_are_plan_keys_without_guard_hash(tmp_path: Path) -> None:
    attestation = _valid_attestation(tmp_path)

    assert frozenset(attestation.result_body) == REQUIRED_RESULT_KEYS - frozenset(
        {"abort_guard_sha256"}
    )

    missing = dict(attestation.result_body)
    missing.pop("proof_commit")
    extra = {**attestation.result_body, "extra": None}
    with pytest.raises(ValueError):
        validate_attestation(replace(attestation, result_body=missing))
    with pytest.raises(ValueError):
        validate_attestation(replace(attestation, result_body=extra))


def test_result_and_attestation_reject_host_identity_mode_relabel(tmp_path: Path) -> None:
    # Given
    attestation = _valid_attestation(tmp_path)
    body = dict(attestation.result_body)
    body["host_identity_mode"] = "single_sequential"

    # When / Then
    with pytest.raises(ValueError):
        validate_attestation(replace(attestation, result_body=body))
    payload = attestation.to_payload()
    payload["host_identity_mode"] = "single_sequential"
    with pytest.raises(ValueError):
        parse_baseline_attestation(payload)


@pytest.mark.parametrize(
    ("field", "mutated"),
    [
        ("env_original_sha256", "1" * 64),
        ("env_proof_sha256", "2" * 64),
        ("env_mode", 0o640),
        ("external_backup_path", "/tmp/different.backup"),
        ("abort_guard_path", "/tmp/different-guard.json"),
        ("host_a_preflight_sha256", "3" * 64),
        ("host_b_preflight_sha256", "4" * 64),
        ("baseline_sha256", "5" * 64),
        ("protected_artifacts_before_sha256", "6" * 64),
        ("protected_artifacts_before_path", "/tmp/protected-artifacts-before.txt"),
        ("post_install_cloudflare_sha256", "c" * 64),
        ("host_identity_mode", "single_sequential"),
    ],
)
def test_attestation_rejects_each_result_cross_binding_mutation(
    tmp_path: Path,
    field: str,
    mutated: JsonValue,
) -> None:
    attestation = _valid_attestation(tmp_path)
    body = dict(attestation.result_body)
    body[field] = mutated

    with pytest.raises(ValueError):
        validate_attestation(replace(attestation, result_body=body))


@pytest.mark.parametrize(
    ("output_name", "mutated"),
    [
        ("host-a-preflight.json", "1" * 64),
        ("host-b-preflight.json", "2" * 64),
        ("baseline.json", "3" * 64),
        ("protected-artifacts-before.txt", "4" * 64),
    ],
)
def test_attestation_rejects_each_output_map_cross_binding_mutation(
    tmp_path: Path,
    output_name: str,
    mutated: str,
) -> None:
    attestation = _valid_attestation(tmp_path)
    outputs = dict(attestation.output_sha256)
    outputs[output_name] = mutated

    with pytest.raises(ValueError):
        validate_attestation(replace(attestation, output_sha256=outputs))


def test_attestation_rejects_non_plan_guard_and_result_paths(tmp_path: Path) -> None:
    attestation = _valid_attestation(tmp_path)
    body = dict(attestation.result_body)
    body["abort_guard_path"] = "relative/abort-guard.json"
    wrong_guard = replace(
        attestation,
        guard_path="relative/abort-guard.json",
        result_body=body,
    )
    wrong_result = replace(
        attestation,
        result_path=str((tmp_path / "other-result.json").resolve()),
    )

    with pytest.raises(ValueError):
        validate_attestation(wrong_guard)
    with pytest.raises(ValueError):
        validate_attestation(wrong_result)


def test_result_and_attestation_bytes_are_bidirectionally_derived(tmp_path: Path) -> None:
    attestation = _valid_attestation(tmp_path)
    projection = canonical_json_bytes(attestation.to_payload())
    digest = hashlib.sha256(projection).hexdigest()
    expected = (
        canonical_json_bytes({**attestation.result_body, "abort_guard_sha256": digest}) + b"\n"
    )

    assert result_bytes_from_attestation(attestation) == expected
    verify_result_bytes(attestation, expected)

    mutated_attestation = replace(attestation, guard_id="d" * 64)
    with pytest.raises(ValueError):
        verify_result_bytes(mutated_attestation, expected)

    mutated_result = json.loads(expected)
    mutated_result["proof_commit"] = "c" * 40
    with pytest.raises(ValueError):
        verify_result_bytes(
            attestation,
            canonical_json_bytes(mutated_result) + b"\n",
        )
