"""Shared compose apply helpers with state-backed no-op skipping."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Literal, Protocol, TypeVar, cast

from dokploy_wizard.dokploy.client import DokployComposeRecord, DokployDeployResult
from dokploy_wizard.dokploy.env_spec import DokployEnvReconciler, RenderedCompose
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
    def update_compose(
        self, *, compose_id: str, compose_file: str | None = None, env: str | None = None
    ) -> DokployComposeRecord: ...

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult: ...


@dataclass(frozen=True)
class ComposeApplyResult(Generic[LocatorT]):
    locator: LocatorT
    status: Literal["already_present", "applied"]


def apply_compose_noop_guard(
    *,
    rendered_compose: str | RenderedCompose,
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

    compose_file = _compose_file_text(rendered_compose)
    rendered_hash = ComposeArtifactHashState.from_rendered_compose(
        service_id=service_key,
        rendered_compose=compose_file,
    )
    stored_hash = load_compose_artifact_hash(state_dir=state_dir, service_key=service_key)

    if stored_hash == rendered_hash and _verification_passed(verify_current()):
        return ComposeApplyResult(locator=locator, status="already_present")

    updated = apply_rendered_compose_to_existing(
        client=client,
        compose_id=compose_id,
        rendered_compose=rendered_compose,
    )
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
        rendered_compose=compose_file,
    )
    return ComposeApplyResult(
        locator=locator_factory(updated.compose_id),
        status="applied",
    )


def apply_rendered_compose_to_existing(
    *,
    client: ComposeMutationApi,
    compose_id: str,
    rendered_compose: str | RenderedCompose,
) -> DokployComposeRecord:
    if isinstance(rendered_compose, RenderedCompose):
        env_payload = DokployEnvReconciler(client=cast(Any, client)).build_env_payload(
            rendered_compose
        )
        if env_payload:
            _update_compose_env_if_supported(client, compose_id=compose_id, env_payload=env_payload)
        return client.update_compose(
            compose_id=compose_id,
            compose_file=rendered_compose.compose_file,
        )
    return client.update_compose(compose_id=compose_id, compose_file=rendered_compose)


def _compose_file_text(rendered_compose: str | RenderedCompose) -> str:
    if isinstance(rendered_compose, RenderedCompose):
        return rendered_compose.compose_file
    return rendered_compose


def _update_compose_env_if_supported(
    client: ComposeMutationApi, *, compose_id: str, env_payload: str
) -> None:
    update_compose = cast(Any, client).update_compose
    try:
        update_compose(compose_id=compose_id, env=env_payload)
    except TypeError as error:
        message = str(error)
        if "env" not in message and "compose_file" not in message:
            raise


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
