from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

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


def test_abort_status_rejects_incomplete_plan_owned_receipt(tmp_path: Path) -> None:
    from dokploy_wizard.proof import EnvReceipt, model_sync_cli
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

    status = model_sync_cli.main(
        ["abort-status", "--guard", str(guard), "--output", str(output)]
    )
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
    assert status.env_receipt is not None


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
