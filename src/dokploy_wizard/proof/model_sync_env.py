"""Guarded preparation and restoration of the temporary model-sync env copy."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Final

from dokploy_wizard.proof import (
    EnvPreparationError,
    EnvReceipt,
    PreparedEnv,
    ProofNamespace,
    read_bounded_regular_bytes,
)
from dokploy_wizard.proof.model_sync_artifacts import (
    _fsync_directory,
    _write_all,
    atomic_write_bytes,
    sha256_bytes,
)
from dokploy_wizard.proof.model_sync_results import ProofTransport
from dokploy_wizard.proof.model_sync_state import (
    AbortGuardError,
    read_abort_guard,
    record_env_intent,
    record_proof_active,
)
from dokploy_wizard.state import (
    RawEnvInput,
    StateValidationError,
    parse_env_file,
    resolve_desired_state,
)

__all__ = ("EnvPreparationError", "PreparedEnv", "ProofNamespace")

_FILE_MODE: Final = 0o600
_DIRECTORY_MODE: Final = 0o700
_MAX_ENV_BYTES: Final = 256 * 1024
_NVIDIA_KEYS: Final = frozenset(
    {"LITELLM_NVIDIA_API_KEY", "LITELLM_NVIDIA_BASE_URL", "LITELLM_NVIDIA_MODELS"}
)


def resolve_proof_transport(env_file: Path) -> ProofTransport:
    """Load only collector credentials without serializing them into proof namespaces."""
    content, _mode = _read_regular_bytes(env_file, None)
    raw_env = _parse_bytes(content, env_file.parent, ".model-sync-transport-")
    values = raw_env.values
    desired = resolve_desired_state(raw_env)
    return ProofTransport(
        cloudflare_account_id=values.get("CLOUDFLARE_ACCOUNT_ID") or None,
        cloudflare_zone_id=values.get("CLOUDFLARE_ZONE_ID") or None,
        cloudflare_zone_name=desired.root_domain,
        cloudflare_token=values.get("CLOUDFLARE_API_TOKEN") or None,
        dokploy_api_url=values.get("DOKPLOY_API_URL") or None,
        dokploy_api_key=values.get("DOKPLOY_API_KEY") or None,
        coder_email=values.get("DOKPLOY_ADMIN_EMAIL") or None,
        coder_hostname=desired.hostnames.get("coder"),
        coder_password=values.get("DOKPLOY_ADMIN_PASSWORD") or None,
        tailscale_required=desired.tailscale_hostname is not None,
    )


def prepare_proof_env(
    *, env_file: Path, backup_path: Path, guard_path: Path, claim_token: str
) -> PreparedEnv:
    """Prepare an NVIDIA-free proof env only while a durable guard is armed."""
    _require_armed_guard(guard_path)
    original, mode = _read_regular_bytes(env_file, None)
    proof = _proof_bytes(original, env_file.parent)
    original_sha256 = sha256_bytes(original)
    proof_sha256 = sha256_bytes(proof)
    receipt = EnvReceipt(
        str(env_file.resolve()),
        str(backup_path.resolve()),
        original_sha256,
        proof_sha256,
        mode,
    )
    record_env_intent(guard_path, claim_token=claim_token, receipt=receipt)
    _validate_backup(
        env_file=env_file,
        backup_path=backup_path,
        original=original,
        proof=proof,
    )
    if not backup_path.exists():
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.parent.chmod(_DIRECTORY_MODE)
        atomic_write_bytes(backup_path, original, mode=_FILE_MODE)
    if proof != original:
        atomic_write_bytes(env_file, proof, mode=_FILE_MODE)
    record_proof_active(guard_path, claim_token=claim_token)
    return PreparedEnv(env_file, backup_path, original_sha256, proof_sha256, mode)


def resolve_proof_namespace(env_file: Path) -> ProofNamespace:
    """Resolve exact proof namespaces without replacing or persisting the operator env."""
    original, _mode = _read_regular_bytes(env_file, None)
    proof = _proof_bytes(original, env_file.parent)
    try:
        raw_env = _parse_bytes(proof, env_file.parent, ".model-sync-resolve-")
        desired = resolve_desired_state(raw_env)
    except StateValidationError as error:
        raise EnvPreparationError("proof env cannot resolve its resource namespace") from error
    stack = desired.stack_name
    shared = desired.shared_core
    docker = [stack, f"{stack}-coder", f"{stack}-cloudflared", shared.network_name]
    docker.extend(
        service.service_name
        for service in (shared.litellm, shared.postgres, shared.redis, shared.mail_relay)
        if service is not None
    )
    templates = (
        "ubuntu-vscode",
        "ubuntu-vscode-opencode-web",
        "ubuntu-vscode-openwork",
        "ubuntu-vscode-kdense-byok",
        "ubuntu-vscode-hermes",
        "ubuntu-vscode-pi-web",
    )
    return ProofNamespace(
        stack_name=stack,
        docker=tuple(sorted(set(docker))),
        dokploy=tuple(sorted({stack, f"{stack}-coder", f"{stack}-shared"})),
        cloudflare=tuple(sorted({stack, f"{stack}-cloudflared", *desired.hostnames.values()})),
        tailscale=() if desired.tailscale_hostname is None else (desired.tailscale_hostname,),
        coder_templates=templates,
    )


def restore_proof_env(*, prepared: PreparedEnv, guard_path: Path) -> None:
    """Restore only exact recognized proof bytes and retain durable plan ownership."""
    _require_armed_guard(guard_path)
    current, _mode = _read_regular_bytes(prepared.env_file, None)
    current_sha256 = sha256_bytes(current)
    if current_sha256 not in {prepared.original_sha256, prepared.proof_sha256}:
        raise EnvPreparationError("proof env has an unknown current hash")
    if not os.path.lexists(prepared.backup_path):
        if current_sha256 != prepared.original_sha256:
            raise EnvPreparationError("proof env requires its original external backup")
        return
    backup, _mode = _read_regular_bytes(prepared.backup_path, _FILE_MODE)
    if sha256_bytes(backup) != prepared.original_sha256:
        raise EnvPreparationError("external backup does not match the recorded original hash")
    atomic_write_bytes(prepared.env_file, backup, mode=prepared.mode)
    os.unlink(prepared.backup_path)
    _fsync_directory(prepared.backup_path.parent)


def _proof_bytes(original: bytes, parent: Path) -> bytes:
    try:
        _validate_bytes(original, parent)
    except StateValidationError as original_error:
        keys = _configured_nvidia_keys(original)
        if not keys or keys == _NVIDIA_KEYS:
            raise EnvPreparationError(
                "env does not contain an approved partial NVIDIA block"
            ) from original_error
        proof = _strip_nvidia_block(original)
        try:
            _validate_bytes(proof, parent)
        except StateValidationError as proof_error:
            raise EnvPreparationError(
                "proof env remains invalid after NVIDIA block removal"
            ) from proof_error
        return proof
    return original


def _validate_backup(*, env_file: Path, backup_path: Path, original: bytes, proof: bytes) -> None:
    if not os.path.lexists(backup_path):
        return
    backup, _mode = _read_regular_bytes(backup_path, _FILE_MODE)
    if backup != original:
        raise EnvPreparationError("existing external backup has an unknown hash")
    current, _mode = _read_regular_bytes(env_file, None)
    if current not in {original, proof}:
        raise EnvPreparationError("existing proof env has an unknown hash")


def _configured_nvidia_keys(content: bytes) -> frozenset[str]:
    keys = {
        line.split("=", 1)[0].strip()
        for line in content.decode("utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    return frozenset(keys & _NVIDIA_KEYS)


def _strip_nvidia_block(content: bytes) -> bytes:
    kept_lines = [
        line
        for line in content.decode("utf-8").splitlines(keepends=True)
        if line.split("=", 1)[0].strip() not in _NVIDIA_KEYS
    ]
    return "".join(kept_lines).encode("utf-8")


def _validate_bytes(content: bytes, parent: Path) -> None:
    resolve_desired_state(_parse_bytes(content, parent, ".model-sync-validate-"))


def _parse_bytes(content: bytes, parent: Path, prefix: str) -> RawEnvInput:
    descriptor, temporary_name = tempfile.mkstemp(prefix=prefix, dir=parent)
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, _FILE_MODE)
            _write_all(descriptor, content)
            os.fsync(descriptor)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        os.close(descriptor)
        raw_env = parse_env_file(temporary)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except BaseException:
            pass
        raise
    temporary.unlink(missing_ok=True)
    return raw_env


def _read_regular_bytes(path: Path, mode: int | None) -> tuple[bytes, int]:
    try:
        return read_bounded_regular_bytes(path, _MAX_ENV_BYTES, mode)
    except ValueError as error:
        raise EnvPreparationError(str(error)) from error
    except OSError as error:
        raise EnvPreparationError("proof env or backup is unreadable") from error


def _require_armed_guard(guard_path: Path) -> None:
    try:
        guard = read_abort_guard(guard_path)
    except AbortGuardError as error:
        raise EnvPreparationError("abort guard must be armed before env mutation") from error
    if guard.state != "armed":
        raise EnvPreparationError("abort guard must be armed before env mutation")
