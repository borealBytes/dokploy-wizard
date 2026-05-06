"""Shared compose apply helpers with state-backed no-op skipping."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, Protocol, TypeVar

from dokploy_wizard.dokploy.client import DokployComposeRecord, DokployDeployResult
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    ComposeArtifactHashState,
    load_state_dir,
    write_applied_checkpoint,
)
from dokploy_wizard.verification import ServiceVerificationResult

LocatorT = TypeVar("LocatorT")
VerificationOutcome = bool | ServiceVerificationResult


class ComposeMutationApi(Protocol):
    def update_compose(self, *, compose_id: str, compose_file: str) -> DokployComposeRecord: ...

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult: ...


@dataclass(frozen=True)
class ComposeApplyResult(Generic[LocatorT]):
    locator: LocatorT
    status: Literal["already_present", "applied"]


def apply_compose_noop_guard(
    *,
    rendered_compose: str,
    service_key: str,
    state_dir: Path,
    client: ComposeMutationApi,
    locator: LocatorT,
    compose_id: str,
    title: str | None,
    description: str | None,
    verify_current: Callable[[], VerificationOutcome],
    locator_factory: Callable[[str], LocatorT],
) -> ComposeApplyResult[LocatorT]:
    """Skip compose mutation only when the rendered hash matches and verification passes."""

    rendered_hash = ComposeArtifactHashState.from_rendered_compose(
        service_id=service_key,
        rendered_compose=rendered_compose,
    )
    stored_hash = load_compose_artifact_hash(state_dir=state_dir, service_key=service_key)

    if stored_hash == rendered_hash and _verification_passed(verify_current()):
        return ComposeApplyResult(locator=locator, status="already_present")

    updated = client.update_compose(compose_id=compose_id, compose_file=rendered_compose)
    deployment = client.deploy_compose(
        compose_id=updated.compose_id,
        title=title,
        description=description,
    )
    if not deployment.success:
        msg = f"Dokploy deploy for compose service '{service_key}' did not report success."
        raise RuntimeError(msg)

    persist_compose_artifact_hash(
        state_dir=state_dir,
        service_key=service_key,
        rendered_compose=rendered_compose,
    )
    return ComposeApplyResult(
        locator=locator_factory(updated.compose_id),
        status="applied",
    )


def load_compose_artifact_hash(
    *, state_dir: Path, service_key: str
) -> ComposeArtifactHashState | None:
    applied_state = load_state_dir(state_dir).applied_state
    if applied_state is None:
        return None
    return applied_state.compose_artifact_hashes.get(service_key)


def persist_compose_artifact_hash(
    *, state_dir: Path, service_key: str, rendered_compose: str
) -> ComposeArtifactHashState:
    rendered_hash = ComposeArtifactHashState.from_rendered_compose(
        service_id=service_key,
        rendered_compose=rendered_compose,
    )
    applied_state = _require_applied_state(state_dir)
    updated_hashes = dict(applied_state.compose_artifact_hashes)
    updated_hashes[service_key] = rendered_hash
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=applied_state.format_version,
            desired_state_fingerprint=applied_state.desired_state_fingerprint,
            completed_steps=applied_state.completed_steps,
            compose_artifact_hashes=updated_hashes,
            lifecycle_checkpoint_contract_version=(
                applied_state.lifecycle_checkpoint_contract_version
            ),
        ),
    )
    return rendered_hash


def _verification_passed(result: VerificationOutcome) -> bool:
    if isinstance(result, ServiceVerificationResult):
        return result.passed
    return result


def _require_applied_state(state_dir: Path) -> AppliedStateCheckpoint:
    applied_state = load_state_dir(state_dir).applied_state
    if applied_state is None:
        msg = (
            "Compose artifact hash persistence requires an applied-state checkpoint "
            f"in '{state_dir}'."
        )
        raise RuntimeError(msg)
    return applied_state
