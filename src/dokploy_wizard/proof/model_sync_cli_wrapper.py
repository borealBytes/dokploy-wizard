"""Bounded remote-proof wrapper command construction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from dokploy_wizard.dokploy.cloudflared import CloudflaredFailureCategory
from dokploy_wizard.proof.model_sync_task1_remote_receipt_schema_types import (
    Task1RemoteProofExpectation,
    Task1RemoteReceiptError,
)
from dokploy_wizard.proof.model_sync_task1_remote_receipt_validation import (
    require_terminal_receipt,
)


class BoundedProcessRunner(Protocol):
    """The bounded subprocess executor used by the proof wrapper."""

    def __call__(
        self,
        command: list[str],
        *,
        stdin: bytes,
        output_limit: int,
        timeout_seconds: int,
        label: str,
        nonzero_error_factory: Callable[[bytes], RuntimeError] | None = None,
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ProofWrapperInvocation:
    """Inputs needed to run one remote proof wrapper invocation."""

    wrapper: Path
    host: str
    password: str
    env_file: Path
    task1_proof_context: Path | None
    task1_expectation: Task1RemoteProofExpectation | None = None


def run_proof_wrapper(
    invocation: ProofWrapperInvocation,
    runner: BoundedProcessRunner,
) -> None:
    """Run the bounded remote proof wrapper using an optional Task 1 context."""
    command = [
        str(invocation.wrapper),
        "proof",
        "--host",
        invocation.host,
        "--password-stdin",
        "--env-file",
        str(invocation.env_file),
    ]
    if invocation.task1_proof_context is not None:
        command.extend(["--task1-proof-context", str(invocation.task1_proof_context)])
    if (invocation.task1_proof_context is None) != (invocation.task1_expectation is None):
        raise Task1RemoteReceiptError("Task 1 remote proof expectation is missing")
    output = runner(
        command,
        stdin=(invocation.password + "\n").encode(),
        output_limit=2 * 1024 * 1024,
        timeout_seconds=3600,
        label="remote proof wrapper",
        nonzero_error_factory=(
            None if invocation.task1_expectation is None else _classify_task1_nonzero
        ),
    )
    if invocation.task1_expectation is not None:
        require_terminal_receipt(output, invocation.task1_expectation)


def _classify_task1_nonzero(stderr: bytes) -> RuntimeError:
    category_matches = {
        match.decode("ascii")
        for match in re.findall(rb"TASK1_REMOTE_ERROR_CATEGORY=([a-z_]+\.[a-z_]+)", stderr)
    }
    allowed_categories = {str(category) for category in CloudflaredFailureCategory}
    if len(category_matches) == 1 and category_matches <= allowed_categories:
        category = category_matches.pop()
        return Task1RemoteReceiptError(f"Task 1 remote failure category: {category}")
    status = re.search(rb"Dokploy API request failed with status ([1-5][0-9]{2}):", stderr)
    if status is not None:
        field = re.search(rb"invalid field: ([A-Za-z][A-Za-z0-9_]*)", stderr)
        code = status.group(1).decode("ascii")
        if field is not None:
            name = field.group(1).decode("ascii")
            return Task1RemoteReceiptError(f"Dokploy API status {code} rejected field {name}")
        return Task1RemoteReceiptError(f"Dokploy API request failed with status {code}")
    if b"Dokploy public URL did not become reachable" in stderr:
        return Task1RemoteReceiptError("Cloudflare connector health check failed")
    if b"Dokploy API request failed:" in stderr:
        return Task1RemoteReceiptError("Dokploy API transport failed")
    fixed_markers = (
        "Task 1 remote proof archive is absent or unsafe",
        "Task 1 remote proof upload environment is absent or unsafe",
        "Task 1 remote proof context is absent or unsafe",
        "Task 1 remote proof archive hash mismatched after upload",
        "Task 1 remote proof upload hash mismatched after upload",
        "Task 1 remote proof receipt replay is not allowed",
        "Task 1 remote proof commit is invalid",
        "Dokploy project.all response must be a list.",
        "Dokploy project.create response must be an object.",
        "Dokploy project.create response must contain project and environment objects.",
        "Dokploy project summary must be an object.",
        "Dokploy project summary environments must be a list.",
        "Dokploy environment summary must be an object.",
        "Dokploy environment compose list must be a list.",
        "Dokploy environment isDefault must be a boolean.",
        "Dokploy compose summary must be an object.",
        "Dokploy compose status must be a string or null.",
        "Dokploy compose.create response must be an object.",
        "Dokploy compose.create serviceName must be a string or null.",
        "Dokploy compose.create timezone must be a string or null.",
        "Dokploy compose.create enabled must be a boolean.",
        "Dokploy compose.update response must be an object.",
        "Dokploy compose.update serviceName must be a string or null.",
        "Dokploy compose.update timezone must be a string or null.",
        "Dokploy compose.update enabled must be a boolean.",
        "Dokploy compose.deploy response must be true or an object.",
        "Dokploy compose.deploy response must include boolean success.",
        "Dokploy compose.deploy response message must be a string.",
        "Dokploy compose.deploy response composeId must be a string.",
        "Dokploy API response must decode to a JSON object or array.",
        "Dokploy API field 'projectId' must be a non-empty string.",
        "Dokploy API field 'environmentId' must be a non-empty string.",
        "Dokploy API field 'composeId' must be a non-empty string.",
        "Cloudflare connector service name does not match the active Dokploy plan.",
    )
    stage_markers = tuple(
        marker
        for stage in ("install", "verify", "inspect", "collect")
        for marker in (
            f"Task 1 remote proof state after {stage} is absent or unsafe",
            f"Task 1 remote proof state is invalid after {stage}",
            f"Task 1 remote proof Cloudflare journal after {stage} is absent or unsafe",
            f"Task 1 remote proof Cloudflare journal context mismatched after {stage}",
        )
    )
    for marker in (*fixed_markers, *stage_markers):
        if marker.encode() in stderr:
            return Task1RemoteReceiptError(marker)
    return Task1RemoteReceiptError("Task 1 remote proof wrapper failed before terminal receipt")
