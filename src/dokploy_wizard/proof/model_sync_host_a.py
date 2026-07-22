# ruff: noqa: E501, E701, E702, I001
from __future__ import annotations

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.proof import BaselineAttestation, canonical_json_bytes
from dokploy_wizard.proof.model_sync_artifacts import CaptureSchemaError, JsonValue, ProtectedManifestEntry, sha256_bytes, validate_protected_manifest_bytes, write_or_verify_exact_bytes
from dokploy_wizard.proof.model_sync_baseline import CapturedBaseline
from dokploy_wizard.proof.model_sync_env import EnvPreparationError, PreparedEnv, restore_proof_env
from dokploy_wizard.proof.model_sync_remote import RemoteProbe
from dokploy_wizard.proof.model_sync_results import build_result, result_bytes_from_attestation
from dokploy_wizard.proof.model_sync_state import AbortGuard, AbortGuardError, arm_abort_guard, begin_rollback, claim_abort_guard, claim_rollback_guard, complete_abort_guard, process_identity_matches, reclaim_finalize_intent, recover_dead_abort_claim, record_finalize_intent, read_abort_guard, reset_abort_guard, transfer_abort_guard_to_plan


@dataclass(frozen=True, slots=True)
class GuardClaim:
    token: str
    pid: int
    start_time_ticks: str


@dataclass(frozen=True, slots=True)
class ProofRecoveryPaths:
    env_file: Path
    backup_path: Path
    guard_path: Path
    artifact_dir: Path
    output: Path
    repository_root: Path


@dataclass(frozen=True, slots=True)
class ProofRecovery:
    paths: ProofRecoveryPaths
    claim: GuardClaim | None
    terminal: bool
    resumable: bool


@dataclass(frozen=True, slots=True)
class BaselineArtifactInputs:
    repository_root: Path
    artifact_dir: Path
    output: Path
    source_base_commit: str
    proof_commit: str
    prepared: PreparedEnv
    guard_path: Path
    claim: GuardClaim
    host_a: RemoteProbe
    host_b: RemoteProbe
    baseline: CapturedBaseline


def claim_plan_guard(*, guard_path: Path, pid: int, start_time_ticks: str) -> GuardClaim:
    token = secrets.token_urlsafe(24)
    claim_abort_guard(guard_path, pid=pid, start_time_ticks=start_time_ticks, claim_token=token)
    return GuardClaim(token, pid, start_time_ticks)


def begin_proof_recovery(*, paths: ProofRecoveryPaths, pid: int, start_time_ticks: str) -> ProofRecovery:
    """Reconcile disk state before issuing one new process ownership claim."""
    _protected_bytes(paths)
    if not os.path.lexists(paths.guard_path):
        _assert_no_generated_outputs(paths)
        arm_abort_guard(paths.guard_path)
    guard = read_abort_guard(paths.guard_path)
    match guard.phase:
        case "complete":
            _verify_attestation(paths, guard, require_result=True)
            return ProofRecovery(paths, None, True, False)
        case "ready":
            _assert_no_generated_outputs(paths)
        case "finalize_intent":
            if guard.pid is not None and guard.start_time_ticks is not None and process_identity_matches(guard.pid, guard.start_time_ticks):
                raise AbortGuardError("existing abort guard is claimed by a live process")
            token = secrets.token_urlsafe(24)
            reclaim_finalize_intent(paths.guard_path, pid=pid, start_time_ticks=start_time_ticks, claim_token=token, process_identity=process_identity_matches)
            claim = GuardClaim(token, pid, start_time_ticks)
            recovery = ProofRecovery(paths, claim, False, _is_resumable(paths))
            if recovery.resumable:
                return recovery
            recover_interrupted_proof(recovery)
        case "claimed" | "env_intent" | "proof_active" | "rollback":
            _recover_incomplete_guard(paths, pid, start_time_ticks, guard)
        case unexpected:
            raise AbortGuardError(f"unsupported abort guard phase: {unexpected}")
    return ProofRecovery(paths, claim_plan_guard(guard_path=paths.guard_path, pid=pid, start_time_ticks=start_time_ticks), False, False)


def recover_interrupted_proof(recovery: ProofRecovery) -> None:
    """Converge a nonterminal guard to exact rollback or durable fail-closed evidence."""
    if recovery.claim is None:
        return
    guard = read_abort_guard(recovery.paths.guard_path)
    if guard.phase == "complete":
        return
    if guard.phase == "ready" and guard.claimant_kind == "plan":
        return
    if guard.phase != "rollback":
        begin_rollback(recovery.paths.guard_path, claim_token=recovery.claim.token)
    try:
        current = read_abort_guard(recovery.paths.guard_path)
        if current.env_receipt is not None:
            receipt = current.env_receipt
            restore_proof_env(prepared=PreparedEnv(Path(receipt.env_path), Path(receipt.backup_path), receipt.original_sha256, receipt.proof_sha256, receipt.mode), guard_path=recovery.paths.guard_path)
        if current.attestation is None:
            _assert_no_generated_outputs(recovery.paths)
        else:
            _remove_authorized_outputs(recovery.paths, current.attestation)
    except (AbortGuardError, CaptureSchemaError, EnvPreparationError, OSError):
        transfer_abort_guard_to_plan(recovery.paths.guard_path, claim_token=recovery.claim.token)
        raise
    transfer_abort_guard_to_plan(recovery.paths.guard_path, claim_token=recovery.claim.token)
    reset_abort_guard(recovery.paths.guard_path)


def complete_resumable_finalization(recovery: ProofRecovery) -> None:
    if recovery.claim is None or not recovery.resumable:
        raise AbortGuardError("no resumable finalization is active")
    guard = read_abort_guard(recovery.paths.guard_path)
    if guard.attestation is None:
        raise AbortGuardError("finalization intent has no attestation")
    write_or_verify_exact_bytes(recovery.paths.output, result_bytes_from_attestation(guard.attestation))
    _verify_attestation(recovery.paths, read_abort_guard(recovery.paths.guard_path), require_result=True)
    complete_abort_guard(recovery.paths.guard_path, claim_token=recovery.claim.token)


def _recover_incomplete_guard(paths: ProofRecoveryPaths, pid: int, start_time_ticks: str, guard: AbortGuard) -> None:
    if guard.claimant_kind == "process":
        if guard.pid is not None and guard.start_time_ticks is not None and process_identity_matches(guard.pid, guard.start_time_ticks):
            raise AbortGuardError("existing abort guard is claimed by a live process")
        recover_dead_abort_claim(paths.guard_path, process_identity=process_identity_matches)
    rollback = read_abort_guard(paths.guard_path)
    if rollback.phase != "rollback" or rollback.claimant_kind != "plan":
        raise AbortGuardError("incomplete guard cannot be reconciled")
    token = secrets.token_urlsafe(24)
    claim_rollback_guard(paths.guard_path, pid=pid, start_time_ticks=start_time_ticks, claim_token=token)
    recover_interrupted_proof(ProofRecovery(paths, GuardClaim(token, pid, start_time_ticks), False, False))


def finalize_baseline_artifacts(inputs: BaselineArtifactInputs) -> None:
    paths = ProofRecoveryPaths(inputs.prepared.env_file, inputs.prepared.backup_path, inputs.guard_path, inputs.artifact_dir, inputs.output, inputs.repository_root)
    _assert_no_generated_outputs(paths)
    manifest = _protected_bytes(paths)
    payloads = {
        "baseline.json": canonical_json_bytes(inputs.baseline.payload) + b"\n",
        "host-a-preflight.json": canonical_json_bytes(inputs.host_a.to_dict()) + b"\n",
        "host-b-preflight.json": canonical_json_bytes(inputs.host_b.to_dict()) + b"\n",
    }
    guard = read_abort_guard(inputs.guard_path)
    if guard.env_receipt is None:
        raise AbortGuardError("proof-active guard lacks env receipt")
    body = build_result(_result_values(inputs, manifest, payloads, "f" * 64))
    attestation = BaselineAttestation(guard.guard_id, str(inputs.guard_path.resolve()), str(inputs.artifact_dir.resolve()), str(inputs.output.resolve()), guard.env_receipt, {**{name: sha256_bytes(value) for name, value in payloads.items()}, "protected-artifacts-before.txt": sha256_bytes(manifest)}, {key: value for key, value in body.items() if key != "abort_guard_sha256"})
    record_finalize_intent(inputs.guard_path, claim_token=inputs.claim.token, attestation=attestation)
    for name in sorted(payloads):
        write_or_verify_exact_bytes(inputs.artifact_dir / name, payloads[name])
    write_or_verify_exact_bytes(inputs.output, result_bytes_from_attestation(attestation))
    _verify_attestation(paths, read_abort_guard(inputs.guard_path), require_result=True)
    complete_abort_guard(inputs.guard_path, claim_token=inputs.claim.token)


def _result_values(inputs: BaselineArtifactInputs, manifest: bytes, payloads: dict[str, bytes], guard_hash: str) -> dict[str, JsonValue]:
    return {"schema_version": 1, "source_base_commit": inputs.source_base_commit, "proof_commit": inputs.proof_commit, "coder_image_digest": inputs.baseline.images["coder"], "litellm_image_digest": inputs.baseline.images["litellm"], "shared_core_image_digests": {"pgvector": inputs.baseline.images["pgvector"], "redis": inputs.baseline.images["redis"], "postfix": inputs.baseline.images["postfix"], "litellm": inputs.baseline.images["litellm"]}, "env_original_sha256": inputs.prepared.original_sha256, "env_proof_sha256": inputs.prepared.proof_sha256, "env_mode": inputs.prepared.mode, "external_backup_path": str(inputs.prepared.backup_path.resolve()), "abort_guard_path": str(inputs.guard_path.resolve()), "abort_guard_sha256": guard_hash, "host_a_preflight_sha256": sha256_bytes(payloads["host-a-preflight.json"]), "host_b_preflight_sha256": sha256_bytes(payloads["host-b-preflight.json"]), "host_identities_distinct": True, "host_architectures_equal": True, "baseline_sha256": sha256_bytes(payloads["baseline.json"]), "protected_artifacts_before_path": str((inputs.artifact_dir / "protected-artifacts-before.txt").resolve()), "protected_artifacts_before_sha256": sha256_bytes(manifest), "coder_secret_inventory_sha256": inputs.baseline.coder_secret_inventory_sha256, "legacy_workspace_managed_fingerprints_sha256": inputs.baseline.legacy_workspace_managed_fingerprints_sha256}


def _is_resumable(paths: ProofRecoveryPaths) -> bool:
    try:
        _verify_attestation(paths, read_abort_guard(paths.guard_path), require_result=False)
    except (AbortGuardError, CaptureSchemaError, EnvPreparationError, OSError):
        return False
    return True


def _assert_no_generated_outputs(paths: ProofRecoveryPaths) -> None:
    if any(os.path.lexists(path) for path in (*_output_paths(paths).values(), paths.output)):
        raise AbortGuardError("generated output exists without an authorizing attestation")


def _remove_authorized_outputs(paths: ProofRecoveryPaths, attestation: BaselineAttestation) -> None:
    expected = {**attestation.output_sha256, "result.json": sha256_bytes(result_bytes_from_attestation(attestation))}
    for name, path in {**_output_paths(paths), "result.json": paths.output}.items():
        if not os.path.lexists(path):
            continue
        if _sha_regular(path, 0o600) != expected[name]:
            raise AbortGuardError("generated output bytes are not authorized for rollback")
        os.unlink(path)
        _fsync_parent(path.parent)


def _verify_attestation(paths: ProofRecoveryPaths, guard: AbortGuard, *, require_result: bool) -> None:
    attestation, receipt = guard.attestation, guard.env_receipt
    if attestation is None or receipt is None or guard.phase not in {"finalize_intent", "complete"} or attestation.guard_path != str(paths.guard_path.resolve()) or attestation.artifact_dir != str(paths.artifact_dir.resolve()) or attestation.result_path != str(paths.output.resolve()) or receipt.env_path != str(paths.env_file.resolve()) or receipt.backup_path != str(paths.backup_path.resolve()):
        raise AbortGuardError("attestation paths do not bind the current recovery paths")
    proof_mode = receipt.mode if receipt.original_sha256 == receipt.proof_sha256 else 0o600
    if _sha_regular(paths.env_file, proof_mode) != receipt.proof_sha256 or _sha_regular(paths.backup_path, 0o600) != receipt.original_sha256:
        raise AbortGuardError("proof env or backup drifted from its receipt")
    manifest = _protected_bytes(paths)
    if attestation.output_sha256["protected-artifacts-before.txt"] != sha256_bytes(manifest):
        raise AbortGuardError("protected manifest drifted from attestation")
    expected = attestation.output_sha256
    for name, path in _output_paths(paths).items():
        if _sha_regular(path, 0o600) != expected[name]:
            raise AbortGuardError("attested output drifted")
    if require_result:
        if _regular_bytes(paths.output, 0o600) != result_bytes_from_attestation(attestation):
            raise AbortGuardError("result bytes do not match attestation")
    elif os.path.lexists(paths.output) and _regular_bytes(paths.output, 0o600) != result_bytes_from_attestation(attestation):
        raise AbortGuardError("result bytes do not match attestation")


def _output_paths(paths: ProofRecoveryPaths) -> dict[str, Path]:
    return {"baseline.json": paths.artifact_dir / "baseline.json", "host-a-preflight.json": paths.artifact_dir / "host-a-preflight.json", "host-b-preflight.json": paths.artifact_dir / "host-b-preflight.json"}


def _protected_bytes(paths: ProofRecoveryPaths) -> bytes:
    manifest_path, receipt_path = paths.artifact_dir / "protected-artifacts-before.txt", paths.artifact_dir / "protected-artifacts-before.sha256"
    manifest = _stable_manifest_bytes(manifest_path)
    entries = validate_protected_manifest_bytes(manifest)
    _verify_protected_artifacts(paths.repository_root, entries)
    if _stable_manifest_bytes(receipt_path) != f"{sha256_bytes(manifest)}  protected-artifacts-before.txt\n".encode():
        raise AbortGuardError("pre-existing protected manifest receipt is invalid")
    return manifest


def _stable_manifest_bytes(path: Path) -> bytes:
    try:
        before = os.lstat(path)
        content = path.read_bytes()
        after = os.lstat(path)
    except OSError as error:
        raise AbortGuardError("pre-existing protected manifest is unreadable") from error
    if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o777 != 0o600 or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
        raise AbortGuardError("pre-existing protected manifest is not stable")
    return content


def _regular_bytes(path: Path, mode: int) -> bytes:
    try:
        before = os.lstat(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise AbortGuardError("proof file is unreadable") from error
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o777 != mode or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise AbortGuardError("proof file must be a mode-0600 regular file")
        return os.read(descriptor, max(1, after.st_size + 1))
    finally:
        os.close(descriptor)


def _sha_regular(path: Path, mode: int) -> str:
    return sha256_bytes(_regular_bytes(path, mode))


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_directory(component: str, parent: int) -> tuple[int, tuple[int, int, int]]:
    descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    metadata = os.fstat(descriptor)
    return descriptor, (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _verify_protected_artifacts(root: Path, entries: tuple[ProtectedManifestEntry, ...]) -> None:
    if len(entries) > 1_024:
        raise AbortGuardError("protected artifact entry limit exceeded")
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise AbortGuardError("protected artifact root is unavailable") from error
    try:
        for entry in entries:
            parent, identities = os.dup(root_fd), []
            try:
                for component in entry.path.parts[:-1]:
                    child, identity = _open_directory(component, parent); identities.append(identity); os.close(parent); parent = child
                descriptor = os.open(entry.path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
                try:
                    before, digest, size = os.fstat(descriptor), hashlib.sha256(), 0
                    if not stat.S_ISREG(before.st_mode) or before.st_size > 16 * 1024 * 1024:
                        raise AbortGuardError("protected artifact is not a bounded regular file")
                    while chunk := os.read(descriptor, 65_536): digest.update(chunk); size += len(chunk)
                    after = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
                check = os.dup(root_fd)
                try:
                    for component, expected in zip(entry.path.parts[:-1], identities, strict=True):
                        child, actual = _open_directory(component, check); os.close(check); check = child
                        if actual != expected: raise AbortGuardError("protected artifact directory identity changed")
                    named = os.stat(entry.path.parts[-1], dir_fd=check, follow_symlinks=False)
                finally:
                    os.close(check)
            except OSError as error:
                raise AbortGuardError("protected artifact traversal failed") from error
            finally:
                os.close(parent)
            if (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino) or size != before.st_size or digest.hexdigest() != entry.sha256:
                raise AbortGuardError("protected artifact verification failed")
    finally:
        os.close(root_fd)
