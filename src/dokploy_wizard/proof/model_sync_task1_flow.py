"""Task 1 external proof-environment preparation and guarded wrapper invocation."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dokploy_wizard import proof
from dokploy_wizard.proof import model_sync_artifacts as artifacts
from dokploy_wizard.proof.model_sync_cli_wrapper import (
    ProofWrapperInvocation,
    run_proof_wrapper,
)
from dokploy_wizard.proof.model_sync_env import (
    EnvPreparationError,
    _parse_bytes,
    _read_regular_bytes,
    _require_armed_guard,
)
from dokploy_wizard.proof.model_sync_namespace import build_proof_namespace
from dokploy_wizard.proof.model_sync_results import run_bounded_process
from dokploy_wizard.proof.model_sync_state import (
    read_abort_guard,
    record_env_intent,
    record_proof_active,
)
from dokploy_wizard.proof.model_sync_task1_context import (
    PreparedTask1ProofContext,
    activate_task1_proof_context,
    derive_task1_proof_context,
    project_task1_desired_state,
)
from dokploy_wizard.proof.model_sync_task1_materialization import (
    Task1PreparationBoundary,
    Task1PreparationHook,
    ignore_task1_preparation_boundary,
    materialize_task1_external_files,
)
from dokploy_wizard.proof.model_sync_task1_partial_cleanup import (
    cleanup_task1_partial_materialization,
)
from dokploy_wizard.proof.model_sync_task1_remote_receipt_schema_types import (
    Task1RemoteProofExpectation,
)
from dokploy_wizard.state import resolve_desired_state

_JOURNAL_ABSENT_MARKER: Final = b"Task 1 Cloudflare cleanup journal is absent"


class Task1CloudflareJournalAbsentError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Task1ProofPreparation:
    """Canonical source receipt paired with a separate upload-only proof environment."""

    prepared_env: proof.PreparedEnv
    external: PreparedTask1ProofContext


@dataclass(frozen=True, slots=True)
class Task1ProofPreparationInputs:
    env_file: Path
    backup_path: Path
    guard_path: Path
    claim_token: str


def prepare_task1_proof_context(
    inputs: Task1ProofPreparationInputs,
    *,
    boundary_hook: Task1PreparationHook = ignore_task1_preparation_boundary,
) -> Task1ProofPreparation:
    """Arm a no-rewrite source receipt and derive proof inputs outside the checkout."""
    _require_armed_guard(inputs.guard_path)
    source_bytes, mode = _read_regular_bytes(inputs.env_file, None)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    prepared_env = proof.PreparedEnv(
        inputs.env_file,
        inputs.backup_path,
        source_sha256,
        source_sha256,
        mode,
        True,
    )
    try:
        raw_env = _parse_bytes(source_bytes, inputs.env_file.parent, ".model-sync-task1-source-")
        token = secrets.token_hex(16)
        external = derive_task1_proof_context(
            source_values=raw_env.values,
            source_bytes=source_bytes,
            source_path=inputs.env_file,
            proof_directory=inputs.backup_path.parent / f"task1-proof-{token}",
            attempt_token=token,
            source_mode=mode,
        )
    except (OSError, ValueError) as error:
        raise EnvPreparationError("Task 1 proof context cannot be derived") from error
    record_env_intent(
        inputs.guard_path,
        claim_token=inputs.claim_token,
        receipt=proof.EnvReceipt(
            str(inputs.env_file.resolve()),
            str(inputs.backup_path.resolve()),
            source_sha256,
            source_sha256,
            mode,
            external.evidence,
        ),
    )
    try:
        boundary_hook(Task1PreparationBoundary.ENV_INTENT_RECORDED)
        materialize_task1_external_files(external.materialization, boundary_hook=boundary_hook)
        _write_or_verify_external_backup(inputs.backup_path, source_bytes)
        boundary_hook(Task1PreparationBoundary.BACKUP_PUBLISHED)
        boundary_hook(Task1PreparationBoundary.BEFORE_PROOF_ACTIVE)
        record_proof_active(inputs.guard_path, claim_token=inputs.claim_token)
        boundary_hook(Task1PreparationBoundary.PROOF_ACTIVE_RECORDED)
    except BaseException as error:
        try:
            if read_abort_guard(inputs.guard_path).phase == "env_intent":
                cleanup_task1_partial_materialization(external.evidence, inputs.backup_path)
        except (OSError, ValueError) as cleanup_error:
            error.add_note(f"Task 1 exact-owned cleanup failed: {cleanup_error}")
        raise
    return Task1ProofPreparation(prepared_env=prepared_env, external=external)


def _write_or_verify_external_backup(path: Path, source_bytes: bytes) -> None:
    try:
        artifacts.write_or_verify_exact_bytes(path, source_bytes)
    except proof.CaptureSchemaError as error:
        raise EnvPreparationError("Task 1 external backup has unknown bytes") from error


def resolve_task1_proof_namespace(preparation: Task1ProofPreparation) -> proof.ProofNamespace:
    """Project only the validated Coder-wildcard/LiteLLM proof hostname changes."""
    external = preparation.external
    content, _mode = _read_regular_bytes(external.uploaded_env_file, 0o600)
    raw_env = _parse_bytes(
        content,
        external.uploaded_env_file.parent,
        ".model-sync-task1-namespace-",
    )
    with activate_task1_proof_context(external.context):
        desired_state = project_task1_desired_state(resolve_desired_state(raw_env))
    return build_proof_namespace(raw_env.values, desired_state)


def run_task1_proof_wrapper(
    *,
    wrapper: Path,
    host: str,
    password: str,
    preparation: Task1ProofPreparation,
    proof_commit: str,
) -> None:
    """Run the remote wrapper with the external environment and exact context binding."""
    external = preparation.external
    run_proof_wrapper(
        ProofWrapperInvocation(
            wrapper=wrapper,
            host=host,
            password=password,
            env_file=external.uploaded_env_file,
            task1_proof_context=external.context_file,
            task1_expectation=Task1RemoteProofExpectation(
                proof_commit=proof_commit,
                context_sha256=hashlib.sha256(external.context.to_bytes()).hexdigest(),
                uploaded_env_sha256=external.context.uploaded_env_sha256,
            ),
        ),
        run_bounded_process,
    )


def cleanup_task1_cloudflare_proof(
    *, wrapper: Path, host: str, password: str, preparation: Task1ProofPreparation
) -> bytes:
    """Remove only journaled Cloudflare resources after post-install evidence is captured."""
    external = preparation.external
    return cleanup_task1_cloudflare_external(
        wrapper=wrapper,
        host=host,
        password=password,
        uploaded_env_file=external.uploaded_env_file,
        context_file=external.context_file,
    )


def cleanup_task1_cloudflare_external(
    *, wrapper: Path, host: str, password: str, uploaded_env_file: Path, context_file: Path
) -> bytes:
    """Run cleanup from exact external context paths recovered from a durable receipt."""
    return run_bounded_process(
        [
            str(wrapper),
            "task1-cleanup",
            "--host",
            host,
            "--password-stdin",
            "--env-file",
            str(uploaded_env_file),
            "--task1-proof-context",
            str(context_file),
        ],
        stdin=(password + "\n").encode(),
        output_limit=2 * 1024 * 1024,
        timeout_seconds=900,
        label="Task 1 Cloudflare cleanup",
        nonzero_error_factory=_cleanup_nonzero_error,
    )


def _cleanup_nonzero_error(stderr: bytes) -> RuntimeError:
    if _JOURNAL_ABSENT_MARKER in stderr:
        return Task1CloudflareJournalAbsentError()
    return RuntimeError("Task 1 Cloudflare cleanup failed")


def verify_task1_source_restored(preparation: Task1ProofPreparation) -> None:
    """Fail finalization unless the canonical source remains exactly receipt-bound."""
    content, _mode = _read_regular_bytes(preparation.prepared_env.env_file, None)
    actual = hashlib.sha256(content).hexdigest()
    expected = preparation.external.context.expected_restored_source_sha256
    if actual != expected:
        raise EnvPreparationError("Task 1 canonical environment restoration hash does not match")
