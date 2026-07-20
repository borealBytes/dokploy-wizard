from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dokploy_wizard.proof.model_sync_env import (
    EnvPreparationError,
    prepare_proof_env,
    resolve_proof_namespace,
    resolve_proof_transport,
    restore_proof_env,
)
from dokploy_wizard.proof.model_sync_host_a import claim_plan_guard, recover_failed_proof
from dokploy_wizard.proof.model_sync_state import arm_abort_guard, read_abort_guard


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
        prepare_proof_env(env_file=env_file, backup_path=backup, guard_path=guard)

    assert env_file.read_bytes() == original
    assert not backup.exists()

    arm_abort_guard(guard)
    prepared = prepare_proof_env(env_file=env_file, backup_path=backup, guard_path=guard)

    assert prepared.original_sha256 == hashlib.sha256(original).hexdigest()
    assert prepared.proof_sha256 != prepared.original_sha256
    assert backup.read_bytes() == original
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert "LITELLM_NVIDIA_" not in env_file.read_text(encoding="utf-8")


def test_env_prepare_rejects_unknown_current_hash_without_restoring(tmp_path: Path) -> None:
    env_file = tmp_path / "install.env"
    original = _partial_nvidia_env(env_file)
    backup = tmp_path / "secrets" / "install.env.backup"
    guard = tmp_path / "abort-guard.json"
    arm_abort_guard(guard)
    prepared = prepare_proof_env(env_file=env_file, backup_path=backup, guard_path=guard)
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
    prepared = prepare_proof_env(env_file=env_file, backup_path=backup, guard_path=guard)

    restore_proof_env(prepared=prepared, guard_path=guard)

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
    prepared = prepare_proof_env(env_file=env_file, backup_path=backup, guard_path=guard)
    claim = claim_plan_guard(guard_path=guard, pid=12, start_time_ticks="34")

    recover_failed_proof(prepared=prepared, guard_path=guard, claim=claim)
    recover_failed_proof(prepared=prepared, guard_path=guard, claim=claim)

    assert env_file.read_bytes() == original
    assert read_abort_guard(guard).claimant_kind == "plan"


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
