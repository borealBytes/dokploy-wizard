from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Never

import pytest

from dokploy_wizard.proof import EnvReceipt, model_sync_artifacts, model_sync_env
from dokploy_wizard.proof.model_sync_env import (
    EnvPreparationError,
    prepare_proof_env,
    resolve_proof_namespace,
    resolve_proof_transport,
    restore_proof_env,
)
from dokploy_wizard.proof.model_sync_host_a import claim_plan_guard
from dokploy_wizard.proof.model_sync_state import (
    arm_abort_guard,
    begin_rollback,
    process_start_time_ticks,
    read_abort_guard,
    record_env_intent,
    reset_abort_guard,
    transfer_abort_guard_to_plan,
)
from dokploy_wizard.state import StateValidationError

_MAX_ENV_BYTES = 256 * 1024


def test_abort_status_rejects_incomplete_plan_owned_receipt(tmp_path: Path) -> None:
    from dokploy_wizard.proof import model_sync_cli

    guard = tmp_path / "abort-guard.json"
    output = tmp_path / "status.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    record_env_intent(
        guard,
        claim_token=claim.token,
        receipt=EnvReceipt("/tmp/env", "/tmp/backup", "a" * 64, "b" * 64, 0o600),
    )
    begin_rollback(guard, claim_token=claim.token)
    transfer_abort_guard_to_plan(guard, claim_token=claim.token)

    status = model_sync_cli.main(["abort-status", "--guard", str(guard), "--output", str(output)])
    assert status == 1
    assert not output.exists()


def _partial_nvidia_env(path: Path) -> bytes:
    original = (
        b"ROOT_DOMAIN=example.test\n"
        b"PACKS=coder\n"
        b"AI_DEFAULT_PROVIDER=openrouter\n"
        b"AI_DEFAULT_MODEL=example/model\n"
        b"LITELLM_NVIDIA_API_KEY=do-not-record-this\n"
    )
    path.write_bytes(original)
    path.chmod(0o600)
    return original


def _file_identity(path: Path) -> tuple[int, int]:
    metadata = path.stat()
    return metadata.st_dev, metadata.st_ino


def _descriptor_matches(descriptor: int, identity: tuple[int, int]) -> bool:
    metadata = os.fstat(descriptor)
    return (metadata.st_dev, metadata.st_ino) == identity


def test_guard_armed_before_env_write(tmp_path: Path) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"

    with pytest.raises(EnvPreparationError):
        prepare_proof_env(
            env_file=env_file,
            backup_path=backup,
            guard_path=guard,
            claim_token="a" * 32,
        )

    assert env_file.read_bytes() == original
    assert not backup.exists()

    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    prepared = prepare_proof_env(
        env_file=env_file, backup_path=backup, guard_path=guard, claim_token=claim.token
    )

    assert prepared.original_sha256 == hashlib.sha256(original).hexdigest()
    assert prepared.proof_sha256 != prepared.original_sha256
    assert backup.read_bytes() == original
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert "LITELLM_NVIDIA_" not in env_file.read_text(encoding="utf-8")


def test_noop_proof_env_still_has_immutable_receipt_and_retained_backup(tmp_path: Path) -> None:
    env_file = tmp_path / "install.env"
    original = (
        b"ROOT_DOMAIN=example.test\nPACKS=coder\nAI_DEFAULT_PROVIDER=openrouter\n"
        b"AI_DEFAULT_MODEL=example/model\n"
    )
    env_file.write_bytes(original)
    env_file.chmod(0o600)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")

    prepared = prepare_proof_env(
        env_file=env_file,
        backup_path=backup,
        guard_path=guard,
        claim_token=claim.token,
    )

    status = read_abort_guard(guard)
    assert prepared.original_sha256 == prepared.proof_sha256
    assert backup.read_bytes() == original
    assert status.phase == "proof_active"
    assert status.env_receipt == EnvReceipt(
        str(env_file.resolve()),
        str(backup.resolve()),
        prepared.original_sha256,
        prepared.proof_sha256,
        0o600,
    )


def test_env_prepare_rejects_unknown_current_hash_without_restoring(tmp_path: Path) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    prepared = prepare_proof_env(
        env_file=env_file, backup_path=backup, guard_path=guard, claim_token=claim.token
    )
    env_file.write_text("ROOT_DOMAIN=unknown.test\n", encoding="utf-8")

    with pytest.raises(EnvPreparationError):
        restore_proof_env(prepared=prepared, guard_path=guard)

    assert env_file.read_text(encoding="utf-8") == "ROOT_DOMAIN=unknown.test\n"
    assert backup.read_bytes() == original


def test_abort_signal_restore_restores_exact_env_and_plan_claim(tmp_path: Path) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    prepared = prepare_proof_env(
        env_file=env_file, backup_path=backup, guard_path=guard, claim_token=claim.token
    )

    begin_rollback(guard, claim_token=claim.token)
    restore_proof_env(prepared=prepared, guard_path=guard)
    transfer_abort_guard_to_plan(guard, claim_token=claim.token)
    reset_abort_guard(guard)

    status = read_abort_guard(guard)
    assert env_file.read_bytes() == original
    assert status.state == "armed"
    assert status.claimant_kind == "plan"


def test_timeout_after_env_replacement_restores_and_repeated_recovery_is_idempotent(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    prepared = prepare_proof_env(
        env_file=env_file, backup_path=backup, guard_path=guard, claim_token=claim.token
    )

    begin_rollback(guard, claim_token=claim.token)
    restore_proof_env(prepared=prepared, guard_path=guard)
    transfer_abort_guard_to_plan(guard, claim_token=claim.token)
    reset_abort_guard(guard)

    assert env_file.read_bytes() == original
    assert read_abort_guard(guard).claimant_kind == "plan"


def test_rollback_restores_receipted_original_before_a_second_prepare(tmp_path: Path) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    env_file.chmod(0o640)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    dead_claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    first = prepare_proof_env(
        env_file=env_file,
        backup_path=backup,
        guard_path=guard,
        claim_token=dead_claim.token,
    )

    begin_rollback(guard, claim_token=dead_claim.token)
    restore_proof_env(prepared=first, guard_path=guard)
    transfer_abort_guard_to_plan(guard, claim_token=dead_claim.token)
    reset_abort_guard(guard)
    resumed = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    second = prepare_proof_env(
        env_file=env_file,
        backup_path=backup,
        guard_path=guard,
        claim_token=resumed.token,
    )
    begin_rollback(guard, claim_token=resumed.token)
    restore_proof_env(prepared=second, guard_path=guard)
    transfer_abort_guard_to_plan(guard, claim_token=resumed.token)
    reset_abort_guard(guard)

    assert first.proof_sha256 != first.original_sha256
    assert second.original_sha256 == hashlib.sha256(original).hexdigest()
    assert env_file.read_bytes() == original
    assert env_file.stat().st_mode & 0o777 == 0o640
    assert not backup.exists()
    assert read_abort_guard(guard).claimant_kind == "plan"


@pytest.mark.parametrize("current", ["original", "proof"])
def test_rollback_restores_recognized_current_hashes(tmp_path: Path, current: str) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    prepared = prepare_proof_env(
        env_file=env_file, backup_path=backup, guard_path=guard, claim_token=claim.token
    )
    if current == "original":
        env_file.write_bytes(original)

    begin_rollback(guard, claim_token=claim.token)
    restore_proof_env(prepared=prepared, guard_path=guard)
    transfer_abort_guard_to_plan(guard, claim_token=claim.token)
    reset_abort_guard(guard)

    assert env_file.read_bytes() == original
    assert not backup.exists()
    assert read_abort_guard(guard).claimant_kind == "plan"


def test_restore_rejects_unknown_current_hash_without_transferring_claim(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "install.env"
    _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    prepared = prepare_proof_env(
        env_file=env_file, backup_path=backup, guard_path=guard, claim_token=claim.token
    )
    env_file.write_text("ROOT_DOMAIN=unknown.test\n", encoding="utf-8")

    with pytest.raises(EnvPreparationError):
        restore_proof_env(
            prepared=prepared,
            guard_path=guard,
        )

    assert read_abort_guard(guard).claimant_kind == "process"
    assert backup.exists()


def test_restore_rejects_missing_backup_without_transferring_claim(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "install.env"
    _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    prepared = prepare_proof_env(
        env_file=env_file, backup_path=backup, guard_path=guard, claim_token=claim.token
    )
    backup.unlink()

    with pytest.raises(EnvPreparationError):
        restore_proof_env(prepared=prepared, guard_path=guard)

    assert read_abort_guard(guard).claimant_kind == "process"


def test_live_claim_preserves_guard_bytes_until_a_transition(tmp_path: Path) -> None:
    env_file = tmp_path / "install.env"
    _partial_nvidia_env(env_file)
    guard = tmp_path / "abort-guard.json"
    start_time_ticks = process_start_time_ticks(Path("/proc/self/stat").read_text(encoding="utf-8"))
    arm_abort_guard(guard)
    claim_plan_guard(guard_path=guard, pid=os.getpid(), start_time_ticks=start_time_ticks)
    before = guard.read_bytes()

    assert guard.read_bytes() == before


def test_proof_transport_secrets_are_required_but_never_repr_or_namespace_serialized(
    tmp_path: Path,
) -> None:
    secret = "SECRET-PROOF-TRANSPORT-SENTINEL"
    env_file = tmp_path / "install.env"
    env_file.write_text(
        "\n".join(
            (
                "ROOT_DOMAIN=example.test",
                "STACK_NAME=proof-stack",
                "PACKS=coder",
                "AI_DEFAULT_PROVIDER=openrouter",
                "AI_DEFAULT_MODEL=example/model",
                "CLOUDFLARE_ACCOUNT_ID=account-proof",
                "CLOUDFLARE_ZONE_ID=zone-proof",
                f"CLOUDFLARE_API_TOKEN={secret}",
                "DOKPLOY_API_URL=https://dokploy.example.test",
                f"DOKPLOY_API_KEY={secret}",
                "DOKPLOY_ADMIN_EMAIL=operator@example.test",
                f"DOKPLOY_ADMIN_PASSWORD={secret}",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    transport = resolve_proof_transport(env_file)
    namespace = resolve_proof_namespace(env_file)

    assert secret not in repr(transport)
    assert secret not in json.dumps(namespace.to_dict())


def test_prepare_tolerates_short_reads_of_current_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    identity = _file_identity(env_file)
    original_read = os.read

    def short_read(descriptor: int, size: int) -> bytes:
        requested = min(size, 7) if _descriptor_matches(descriptor, identity) else size
        return original_read(descriptor, requested)

    monkeypatch.setattr(os, "read", short_read)

    prepared = prepare_proof_env(
        env_file=env_file,
        backup_path=backup,
        guard_path=guard,
        claim_token=claim.token,
    )

    assert prepared.original_sha256 == hashlib.sha256(original).hexdigest()
    assert backup.read_bytes() == original


def test_restore_tolerates_short_reads_of_external_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    prepared = prepare_proof_env(
        env_file=env_file, backup_path=backup, guard_path=guard, claim_token=claim.token
    )
    identity = _file_identity(backup)
    original_read = os.read

    def short_read(descriptor: int, size: int) -> bytes:
        requested = min(size, 5) if _descriptor_matches(descriptor, identity) else size
        return original_read(descriptor, requested)

    monkeypatch.setattr(os, "read", short_read)

    restore_proof_env(prepared=prepared, guard_path=guard)

    assert env_file.read_bytes() == original
    assert not backup.exists()


def test_prepare_rejects_oversized_env_without_mutation(tmp_path: Path) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    oversized = original + b"#" + b"x" * _MAX_ENV_BYTES
    env_file.write_bytes(oversized)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")

    with pytest.raises(EnvPreparationError):
        prepare_proof_env(
            env_file=env_file,
            backup_path=backup,
            guard_path=guard,
            claim_token=claim.token,
        )

    assert env_file.read_bytes() == oversized
    assert not backup.exists()


def test_transport_rejects_oversized_env_without_mutation(tmp_path: Path) -> None:
    env_file = tmp_path / "install.env"
    original = (
        b"ROOT_DOMAIN=example.test\nPACKS=coder\nAI_DEFAULT_PROVIDER=openrouter\n"
        b"AI_DEFAULT_MODEL=example/model\n"
    )
    oversized = original + b"#" + b"x" * _MAX_ENV_BYTES
    env_file.write_bytes(oversized)
    env_file.chmod(0o600)

    with pytest.raises(EnvPreparationError):
        resolve_proof_transport(env_file)

    assert env_file.read_bytes() == oversized


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "special-mode"])
def test_prepare_rejects_unauthorized_env_file_types_and_modes(tmp_path: Path, kind: str) -> None:
    env_file = tmp_path / "install.env"
    target = tmp_path / "target.env"
    original = _partial_nvidia_env(target)
    if kind == "symlink":
        env_file.symlink_to(target)
    elif kind == "directory":
        env_file.mkdir()
    elif kind == "fifo":
        os.mkfifo(env_file, mode=0o600)
    else:
        env_file.write_bytes(original)
        env_file.chmod(0o1600)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")

    with pytest.raises(EnvPreparationError):
        prepare_proof_env(
            env_file=env_file,
            backup_path=backup,
            guard_path=guard,
            claim_token=claim.token,
        )

    assert target.read_bytes() == original
    assert not backup.exists()


def test_restore_rejects_oversized_backup_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    prepared = prepare_proof_env(
        env_file=env_file, backup_path=backup, guard_path=guard, claim_token=claim.token
    )
    proof = env_file.read_bytes()
    oversized = original + b"#" + b"x" * _MAX_ENV_BYTES
    backup.write_bytes(oversized)
    identity = _file_identity(backup)
    original_read = os.read
    backup_reads = 0

    def record_read(descriptor: int, size: int) -> bytes:
        nonlocal backup_reads
        if _descriptor_matches(descriptor, identity):
            backup_reads += 1
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", record_read)

    with pytest.raises(EnvPreparationError):
        restore_proof_env(prepared=prepared, guard_path=guard)

    assert backup_reads == 0
    assert env_file.read_bytes() == proof
    assert backup.read_bytes() == oversized


@pytest.mark.parametrize("kind", ["wrong-mode", "special-mode", "symlink", "directory", "fifo"])
def test_restore_rejects_unauthorized_backup_file_types_and_modes(
    tmp_path: Path, kind: str
) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    prepared = prepare_proof_env(
        env_file=env_file, backup_path=backup, guard_path=guard, claim_token=claim.token
    )
    proof = env_file.read_bytes()
    if kind == "wrong-mode":
        backup.chmod(0o640)
    elif kind == "special-mode":
        backup.chmod(0o1600)
    else:
        backup.unlink()
        if kind == "symlink":
            target = tmp_path / "backup-target.env"
            target.write_bytes(original)
            target.chmod(0o600)
            backup.symlink_to(target)
        elif kind == "directory":
            backup.mkdir()
        else:
            os.mkfifo(backup, mode=0o600)

    with pytest.raises(EnvPreparationError):
        restore_proof_env(prepared=prepared, guard_path=guard)

    assert env_file.read_bytes() == proof


def test_prepare_rejects_env_growth_after_read_without_overwriting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    identity = _file_identity(env_file)
    original_read = os.read
    growth = b"# concurrent growth\n"
    grew = False

    def grow_after_read(descriptor: int, size: int) -> bytes:
        nonlocal grew
        chunk = original_read(descriptor, size)
        if not grew and _descriptor_matches(descriptor, identity):
            grew = True
            with env_file.open("ab") as stream:
                stream.write(growth)
        return chunk

    monkeypatch.setattr(os, "read", grow_after_read)

    with pytest.raises(EnvPreparationError):
        prepare_proof_env(
            env_file=env_file,
            backup_path=backup,
            guard_path=guard,
            claim_token=claim.token,
        )

    assert env_file.read_bytes() == original + growth
    assert not backup.exists()


def test_prepare_rejects_env_path_replacement_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    replacement = tmp_path / "replacement.env"
    replacement.write_bytes(original)
    replacement.chmod(0o600)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    identity = _file_identity(env_file)
    original_read = os.read
    replaced = False

    def replace_after_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(descriptor, size)
        if not replaced and _descriptor_matches(descriptor, identity):
            replaced = True
            os.replace(replacement, env_file)
        return chunk

    monkeypatch.setattr(os, "read", replace_after_read)

    with pytest.raises(EnvPreparationError):
        prepare_proof_env(
            env_file=env_file,
            backup_path=backup,
            guard_path=guard,
            claim_token=claim.token,
        )

    assert env_file.read_bytes() == original
    assert not backup.exists()


def test_restore_rejects_current_env_replacement_before_backup_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    prepared = prepare_proof_env(
        env_file=env_file, backup_path=backup, guard_path=guard, claim_token=claim.token
    )
    proof = env_file.read_bytes()
    replacement = tmp_path / "replacement.env"
    replacement.write_bytes(proof)
    replacement.chmod(0o600)
    identity = _file_identity(env_file)
    original_read = os.read
    replaced = False

    def replace_after_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(descriptor, size)
        if not replaced and _descriptor_matches(descriptor, identity):
            replaced = True
            os.replace(replacement, env_file)
        return chunk

    monkeypatch.setattr(os, "read", replace_after_read)

    with pytest.raises(EnvPreparationError):
        restore_proof_env(prepared=prepared, guard_path=guard)

    assert env_file.read_bytes() == proof
    assert backup.read_bytes() == original


def test_restore_rejects_backup_growth_after_read_without_unlinking_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    prepared = prepare_proof_env(
        env_file=env_file, backup_path=backup, guard_path=guard, claim_token=claim.token
    )
    proof = env_file.read_bytes()
    identity = _file_identity(backup)
    original_read = os.read
    growth = b"# concurrent growth\n"
    grew = False

    def grow_after_read(descriptor: int, size: int) -> bytes:
        nonlocal grew
        chunk = original_read(descriptor, size)
        if not grew and _descriptor_matches(descriptor, identity):
            grew = True
            with backup.open("ab") as stream:
                stream.write(growth)
        return chunk

    monkeypatch.setattr(os, "read", grow_after_read)

    with pytest.raises(EnvPreparationError):
        restore_proof_env(prepared=prepared, guard_path=guard)

    assert env_file.read_bytes() == proof
    assert backup.read_bytes() == original + growth


def test_restore_rejects_backup_path_replacement_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")
    prepared = prepare_proof_env(
        env_file=env_file, backup_path=backup, guard_path=guard, claim_token=claim.token
    )
    proof = env_file.read_bytes()
    replacement = tmp_path / "replacement.backup"
    replacement.write_bytes(original)
    replacement.chmod(0o600)
    identity = _file_identity(backup)
    original_read = os.read
    replaced = False

    def replace_after_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(descriptor, size)
        if not replaced and _descriptor_matches(descriptor, identity):
            replaced = True
            os.replace(replacement, backup)
        return chunk

    monkeypatch.setattr(os, "read", replace_after_read)

    with pytest.raises(EnvPreparationError):
        restore_proof_env(prepared=prepared, guard_path=guard)

    assert env_file.read_bytes() == proof
    assert backup.read_bytes() == original


@pytest.mark.parametrize("failure", ["write", "fsync", "parser", "system-exit"])
def test_validation_temp_is_removed_on_catchable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    content = _partial_nvidia_env(tmp_path / "source.env")

    def fail_write(_descriptor: int, _content: bytes) -> Never:
        raise OSError("write failed")

    def fail_fsync(_descriptor: int) -> Never:
        raise OSError("fsync failed")

    def fail_parser(_path: Path) -> Never:
        raise StateValidationError("parser failed")

    def exit_parser(_path: Path) -> Never:
        raise SystemExit("parser exited")

    if failure == "write":
        monkeypatch.setattr(model_sync_artifacts, "_write_all", fail_write)
        expected: type[BaseException] = OSError
    elif failure == "fsync":
        monkeypatch.setattr(os, "fsync", fail_fsync)
        expected = OSError
    elif failure == "parser":
        monkeypatch.setattr(model_sync_env, "parse_env_file", fail_parser)
        expected = StateValidationError
    else:
        monkeypatch.setattr(model_sync_env, "parse_env_file", exit_parser)
        expected = SystemExit

    with pytest.raises(expected):
        model_sync_env._validate_bytes(content, tmp_path)

    assert list(tmp_path.glob(".model-sync-validate-*")) == []


@pytest.mark.parametrize("failure", ["write", "fsync", "parser", "system-exit"])
def test_namespace_temp_is_removed_on_catchable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    env_file = tmp_path / "install.env"
    _partial_nvidia_env(env_file)

    def keep_original(original: bytes, _parent: Path) -> bytes:
        return original

    def fail_write(_descriptor: int, _content: bytes) -> Never:
        raise OSError("write failed")

    def fail_fsync(_descriptor: int) -> Never:
        raise OSError("fsync failed")

    def fail_parser(_path: Path) -> Never:
        raise StateValidationError("parser failed")

    def exit_parser(_path: Path) -> Never:
        raise SystemExit("parser exited")

    monkeypatch.setattr(model_sync_env, "_proof_bytes", keep_original)
    if failure == "write":
        monkeypatch.setattr(model_sync_artifacts, "_write_all", fail_write)
        expected: type[BaseException] = OSError
    elif failure == "fsync":
        monkeypatch.setattr(os, "fsync", fail_fsync)
        expected = OSError
    elif failure == "parser":
        monkeypatch.setattr(model_sync_env, "parse_env_file", fail_parser)
        expected = EnvPreparationError
    else:
        monkeypatch.setattr(model_sync_env, "parse_env_file", exit_parser)
        expected = SystemExit

    with pytest.raises(expected):
        resolve_proof_namespace(env_file)

    assert list(tmp_path.glob(".model-sync-resolve-*")) == []


@pytest.mark.parametrize("temporary_prefix", ["validate", "resolve"])
def test_temp_cleanup_failure_does_not_mask_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    temporary_prefix: str,
) -> None:
    env_file = tmp_path / "install.env"
    content = _partial_nvidia_env(env_file)
    original_unlink = Path.unlink

    def keep_original(original: bytes, _parent: Path) -> bytes:
        return original

    def exit_parser(_path: Path) -> Never:
        raise SystemExit("primary failure")

    def reject_cleanup(path: Path, *, missing_ok: bool = False) -> None:
        if path.name.startswith(f".model-sync-{temporary_prefix}-"):
            raise OSError("cleanup failed")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(model_sync_env, "parse_env_file", exit_parser)
    monkeypatch.setattr(Path, "unlink", reject_cleanup)
    if temporary_prefix == "resolve":
        monkeypatch.setattr(model_sync_env, "_proof_bytes", keep_original)

    with pytest.raises(SystemExit, match="primary failure"):
        if temporary_prefix == "validate":
            model_sync_env._validate_bytes(content, tmp_path)
        else:
            resolve_proof_namespace(env_file)
