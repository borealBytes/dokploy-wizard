"""Typed, process-bound Task 1 proof context and external upload environment."""

from __future__ import annotations

import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path

from dokploy_wizard.proof import read_bounded_regular_bytes
from dokploy_wizard.proof.model_sync_task1_context_schema import (
    PROOF_CONTROL_KEYS,
    TASK1_PROOF_OVERLAY_KEYS,
    Task1ProofContextError,
    Task1ProofContextV1,
    canonical_json_bytes,
    sha256,
    task1_env_bytes,
    task1_namespace,
    validate_context,
    validate_task1_uploaded_values,
    validate_token,
)
from dokploy_wizard.proof.model_sync_task1_evidence_schema import Task1ProofContextEvidenceV1
from dokploy_wizard.proof.model_sync_task1_materialization import (
    Task1ProofExternalPlan,
    Task1ProofPlanInputs,
    plan_task1_external_files,
    task1_overlay,
)
from dokploy_wizard.state.models import DesiredState, RawEnvInput

__all__ = (
    "TASK1_PROOF_OVERLAY_KEYS",
    "PreparedTask1ProofContext",
    "Task1ProofContextError",
    "Task1ProofContextV1",
    "activate_task1_proof_context",
    "active_task1_proof_context",
    "deactivate_task1_proof_context",
    "derive_task1_proof_context",
    "load_task1_proof_context",
    "project_task1_desired_state",
    "require_task1_proof_context",
    "validate_task1_proof_context_argument",
)
_REMOVED_UPLOAD_KEYS = frozenset(
    {
        "VPS_HOST",
        "VPS_ROOT_PASSWORD",
        "LITELLM_NVIDIA_API_KEY",
        "LITELLM_NVIDIA_BASE_URL",
        "LITELLM_NVIDIA_MODELS",
    }
)
_CONTEXT: ContextVar[Task1ProofContextV1 | None] = ContextVar(
    "model_sync_task1_proof_context", default=None
)


@dataclass(frozen=True, slots=True)
class PreparedTask1ProofContext:
    """External-only Task 1 files restricted to one mode-0700 proof directory."""

    context: Task1ProofContextV1
    source_path: Path
    proof_directory: Path
    uploaded_env_file: Path
    context_file: Path
    seed_file: Path
    receipt_file: Path
    evidence: Task1ProofContextEvidenceV1
    overlay: dict[str, str]
    uploaded_values: dict[str, str]
    materialization: Task1ProofExternalPlan


def derive_task1_proof_context(
    *,
    source_values: Mapping[str, str],
    source_bytes: bytes,
    source_path: Path,
    proof_directory: Path,
    attempt_token: str | None = None,
    source_mode: int = 0o600,
) -> PreparedTask1ProofContext:
    """Create a strict overlay without changing the canonical operator environment."""
    forbidden = set(source_values) & (PROOF_CONTROL_KEYS | {"CODER_WILDCARD_SUBDOMAIN"})
    if forbidden:
        raise Task1ProofContextError(f"Task 1 source environment must not contain {min(forbidden)}")
    token = attempt_token or secrets.token_hex(16)
    validate_token(token)
    root_domain = source_values.get("ROOT_DOMAIN", "")
    if root_domain == "":
        raise Task1ProofContextError("Task 1 proof context requires ROOT_DOMAIN")
    overlay = task1_overlay(token)
    removed = _REMOVED_UPLOAD_KEYS | TASK1_PROOF_OVERLAY_KEYS
    normalized = {key: value for key, value in source_values.items() if key not in removed}
    uploaded = {**normalized, **overlay}
    normalized_bytes, overlay_bytes, uploaded_bytes = (
        task1_env_bytes(normalized),
        task1_env_bytes(overlay),
        task1_env_bytes(uploaded),
    )
    namespace = task1_namespace({"ROOT_DOMAIN": root_domain, **overlay})
    context = Task1ProofContextV1(
        context_id=token,
        source_env_sha256=sha256(source_bytes),
        normalized_env_sha256=sha256(normalized_bytes),
        overlay_env_sha256=sha256(overlay_bytes),
        uploaded_env_sha256=sha256(uploaded_bytes),
        namespace_sha256=sha256(canonical_json_bytes(namespace)),
        expected_restored_source_sha256=sha256(source_bytes),
        source_env_mode=source_mode,
        root_domain=root_domain,
        stack_name=overlay["STACK_NAME"],
        tunnel_name=overlay["CLOUDFLARE_TUNNEL_NAME"],
        dokploy_subdomain=overlay["DOKPLOY_SUBDOMAIN"],
        coder_subdomain=overlay["CODER_SUBDOMAIN"],
        seaweedfs_subdomain=overlay["SEAWEEDFS_SUBDOMAIN"],
        litellm_admin_subdomain=overlay["LITELLM_ADMIN_SUBDOMAIN"],
    )
    validate_context(context)
    external = plan_task1_external_files(
        Task1ProofPlanInputs(context, source_path, proof_directory, uploaded_bytes)
    )
    return PreparedTask1ProofContext(
        context,
        source_path,
        external.proof_directory,
        external.uploaded_env_file,
        external.context_file,
        external.seed_file,
        external.receipt_file,
        external.evidence,
        overlay,
        uploaded,
        external,
    )


def load_task1_proof_context(path: Path, raw_env: RawEnvInput) -> Task1ProofContextV1:
    """Load the exact context whose upload environment is about to be executed."""
    try:
        content, _mode = read_bounded_regular_bytes(path, 256 * 1024, 0o600)
        context = Task1ProofContextV1.from_bytes(content)
    except (OSError, ValueError) as error:
        raise Task1ProofContextError("Task 1 proof context is unreadable") from error
    validate_task1_uploaded_values(context, raw_env.values)
    return context


def validate_task1_proof_context_argument(
    raw_env: RawEnvInput, context_path: Path | None
) -> Task1ProofContextV1 | None:
    """Require an explicit context exactly when proof-only keys are present."""
    has_proof_keys = bool(set(raw_env.values) & PROOF_CONTROL_KEYS)
    if context_path is None:
        if has_proof_keys:
            raise Task1ProofContextError("Task 1 proof keys require --task1-proof-context")
        return None
    if not has_proof_keys:
        raise Task1ProofContextError("--task1-proof-context requires Task 1 proof keys")
    return load_task1_proof_context(context_path, raw_env)


@contextmanager
def activate_task1_proof_context(context: Task1ProofContextV1 | None) -> Iterator[None]:
    """Allow proof-only projection for one validated lifecycle command."""
    if context is None:
        yield
        return
    token = _CONTEXT.set(context)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


@contextmanager
def deactivate_task1_proof_context() -> Iterator[None]:
    """Temporarily resolve proof-derived state without ambient Task 1 context."""

    token = _CONTEXT.set(None)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def require_task1_proof_context(raw_env: RawEnvInput) -> Task1ProofContextV1 | None:
    """Reject proof keys unless a matching explicit CLI context is active."""
    if not (set(raw_env.values) & PROOF_CONTROL_KEYS):
        return None
    context = _CONTEXT.get()
    if context is None:
        raise Task1ProofContextError("Task 1 proof keys require --task1-proof-context")
    validate_task1_uploaded_values(context, raw_env.values)
    return context


def project_task1_desired_state(desired: DesiredState) -> DesiredState:
    """Remove only Coder wildcard routing and add the proof-only LiteLLM admin host."""
    context = _CONTEXT.get()
    if context is None:
        return desired
    if desired.stack_name != context.stack_name or desired.root_domain != context.root_domain:
        raise Task1ProofContextError("Task 1 proof desired state does not match its context")
    hostnames = dict(desired.hostnames)
    if "coder" not in hostnames:
        raise Task1ProofContextError("Task 1 proof context requires the Coder control hostname")
    hostnames.pop("coder-wildcard", None)
    hostnames["litellm-admin"] = context.litellm_admin_hostname
    return replace(desired, hostnames=dict(sorted(hostnames.items())))


def active_task1_proof_context() -> Task1ProofContextV1 | None:
    """Return the validated context for proof-only infrastructure reconciliation."""
    return _CONTEXT.get()
