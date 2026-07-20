"""Guarded preparation and restoration of the temporary model-sync env copy."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dokploy_wizard.proof.model_sync_results import ProofTransport
from dokploy_wizard.proof.model_sync_state import (
    AbortGuardError,
    atomic_write_bytes,
    read_abort_guard,
    sha256_bytes,
)
from dokploy_wizard.state import StateValidationError, parse_env_file, resolve_desired_state

_FILE_MODE: Final = 0o600
_DIRECTORY_MODE: Final = 0o700
_NVIDIA_KEYS: Final = frozenset({
    "LITELLM_NVIDIA_API_KEY", "LITELLM_NVIDIA_BASE_URL", "LITELLM_NVIDIA_MODELS"
})
@dataclass(frozen=True, slots=True)
class EnvPreparationError(RuntimeError):
    detail: str

    def __str__(self) -> str:
        return self.detail
@dataclass(frozen=True, slots=True)
class PreparedEnv:
    env_file: Path
    backup_path: Path
    original_sha256: str
    proof_sha256: str
    mode: int


@dataclass(frozen=True, slots=True)
class ProofNamespace:
    """Exact non-secret resource names derived from the proof environment."""

    stack_name: str
    docker: tuple[str, ...]
    dokploy: tuple[str, ...]
    cloudflare: tuple[str, ...]
    tailscale: tuple[str, ...]
    coder_templates: tuple[str, ...]

    def to_dict(self) -> dict[str, list[str] | str]:
        """Return the remote probe contract without retaining raw env values."""
        return {
            "cloudflare": list(self.cloudflare),
            "coder": list(self.coder_templates),
            "docker": list(self.docker),
            "dokploy": list(self.dokploy),
            "stack_name": self.stack_name,
            "tailscale": list(self.tailscale),
        }


def resolve_proof_transport(env_file: Path) -> ProofTransport:
    """Load only collector credentials without serializing them into proof namespaces."""
    raw_env = parse_env_file(env_file)
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


def prepare_proof_env(*, env_file: Path, backup_path: Path, guard_path: Path) -> PreparedEnv:
    """Prepare an NVIDIA-free proof env only while a durable guard is armed."""
    _require_armed_guard(guard_path)
    original = _read_env_bytes(env_file)
    mode = env_file.stat().st_mode & 0o777
    proof = _proof_bytes(original, env_file.parent)
    original_sha256 = sha256_bytes(original)
    proof_sha256 = sha256_bytes(proof)
    if proof == original:
        return PreparedEnv(env_file, backup_path, original_sha256, proof_sha256, mode)
    _validate_backup(env_file=env_file, backup_path=backup_path, original=original, proof=proof)
    if not backup_path.exists():
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.parent.chmod(_DIRECTORY_MODE)
        atomic_write_bytes(backup_path, original, mode=_FILE_MODE)
    atomic_write_bytes(env_file, proof, mode=_FILE_MODE)
    return PreparedEnv(env_file, backup_path, original_sha256, proof_sha256, mode)


def resolve_proof_namespace(env_file: Path) -> ProofNamespace:
    """Resolve exact proof namespaces without replacing or persisting the operator env."""
    original = _read_env_bytes(env_file)
    proof = _proof_bytes(original, env_file.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".model-sync-resolve-", dir=env_file.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, _FILE_MODE)
        _write_all(descriptor, proof)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        desired = resolve_desired_state(parse_env_file(temporary))
    except StateValidationError as error:
        raise EnvPreparationError("proof env cannot resolve its resource namespace") from error
    finally:
        temporary.unlink(missing_ok=True)
    stack = desired.stack_name
    shared = desired.shared_core
    docker = [stack, f"{stack}-coder", f"{stack}-cloudflared", shared.network_name]
    docker.extend(
        service.service_name
        for service in (shared.litellm, shared.postgres, shared.redis, shared.mail_relay)
        if service is not None
    )
    templates = (
        "ubuntu-vscode", "ubuntu-vscode-opencode-web", "ubuntu-vscode-openwork",
        "ubuntu-vscode-kdense-byok", "ubuntu-vscode-hermes", "ubuntu-vscode-pi-web",
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
    current = _read_env_bytes(prepared.env_file)
    current_sha256 = sha256_bytes(current)
    if current_sha256 not in {prepared.original_sha256, prepared.proof_sha256}:
        raise EnvPreparationError("proof env has an unknown current hash")
    if not prepared.backup_path.exists():
        if current_sha256 != prepared.original_sha256:
            raise EnvPreparationError("proof env requires its original external backup")
        return
    backup = _read_env_bytes(prepared.backup_path)
    if sha256_bytes(backup) != prepared.original_sha256:
        raise EnvPreparationError("external backup does not match the recorded original hash")
    atomic_write_bytes(prepared.env_file, backup, mode=prepared.mode)
    prepared.backup_path.unlink()
    _remove_empty_backup_parent(prepared.backup_path.parent)


def _proof_bytes(original: bytes, parent: Path) -> bytes:
    try:
        _validate_bytes(original, parent)
    except StateValidationError as original_error:
        keys = _configured_nvidia_keys(original)
        if not keys or keys == _NVIDIA_KEYS:
            message = "env does not contain an approved partial NVIDIA block"
            raise EnvPreparationError(message) from original_error
        proof = _strip_nvidia_block(original)
        try:
            _validate_bytes(proof, parent)
        except StateValidationError as proof_error:
            message = "proof env remains invalid after NVIDIA block removal"
            raise EnvPreparationError(message) from proof_error
        return proof
    return original


def _validate_backup(*, env_file: Path, backup_path: Path, original: bytes, proof: bytes) -> None:
    if not backup_path.exists():
        return
    backup = _read_env_bytes(backup_path)
    if backup != original:
        raise EnvPreparationError("existing external backup has an unknown hash")
    current = _read_env_bytes(env_file)
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
    descriptor, temporary_name = tempfile.mkstemp(prefix=".model-sync-validate-", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, _FILE_MODE)
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        resolve_desired_state(parse_env_file(temporary))
    finally:
        temporary.unlink(missing_ok=True)


def _read_env_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise EnvPreparationError("proof env or backup is unreadable") from error


def _require_armed_guard(guard_path: Path) -> None:
    try:
        guard = read_abort_guard(guard_path)
    except AbortGuardError as error:
        raise EnvPreparationError("abort guard must be armed before env mutation") from error
    if guard.state != "armed":
        raise EnvPreparationError("abort guard must be armed before env mutation")


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        view = view[os.write(descriptor, view) :]


def _remove_empty_backup_parent(parent: Path) -> None:
    try:
        parent.rmdir()
    except OSError:
        return
