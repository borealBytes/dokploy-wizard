from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.dokploy.compose_noop import apply_compose_noop_guard
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    ComposeArtifactHashState,
    load_state_dir,
    write_applied_checkpoint,
)
from dokploy_wizard.verification import ServiceVerificationResult

from .fake_dokploy import FakeDokployApiClient


@dataclass(frozen=True)
class _Locator:
    project_id: str
    environment_id: str
    compose_id: str


def test_matching_healthy_compose_skips_mutation(tmp_path: Path) -> None:
    service_name = "wizard-stack-nextcloud"
    compose_file = "services:\r\n  app:   \r\n    image: nextcloud:latest   \r\n"
    _write_hash_checkpoint(
        tmp_path,
        service_key=service_name,
        rendered_compose="services:\n  app:\n    image: nextcloud:latest\n",
    )
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name=service_name,
        compose_id="cmp-nextcloud",
        project_name="wizard-stack",
        compose_file=compose_file,
    )
    locator = _Locator(project_id="proj-1", environment_id="env-1", compose_id="cmp-nextcloud")

    result = apply_compose_noop_guard(
        rendered_compose=compose_file,
        service_key=service_name,
        state_dir=tmp_path,
        client=client,
        locator=locator,
        compose_id=locator.compose_id,
        title="dokploy-wizard nextcloud reconcile",
        description="Update Nextcloud + OnlyOffice compose app",
        verify_current=lambda: True,
        locator_factory=lambda compose_id: _Locator(
            project_id=locator.project_id,
            environment_id=locator.environment_id,
            compose_id=compose_id,
        ),
    )

    assert result == type(result)(locator=locator, status="already_present")
    client.assert_unchanged_service(service_name)


def test_matching_unhealthy_compose_redeploys(tmp_path: Path) -> None:
    service_name = "wizard-stack-openclaw"
    compose_file = "services:\n  app:\n    image: ghcr.io/borealbytes/openclaw:latest\n"
    _write_hash_checkpoint(tmp_path, service_key=service_name, rendered_compose=compose_file)
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name=service_name,
        compose_id="cmp-openclaw",
        project_name="wizard-stack",
        compose_file=compose_file,
    )
    locator = _Locator(project_id="proj-1", environment_id="env-1", compose_id="cmp-openclaw")

    result = apply_compose_noop_guard(
        rendered_compose=compose_file,
        service_key=service_name,
        state_dir=tmp_path,
        client=client,
        locator=locator,
        compose_id=locator.compose_id,
        title="dokploy-wizard openclaw reconcile",
        description="Update openclaw compose app",
        verify_current=lambda: ServiceVerificationResult(
            service_name=service_name,
            tier="app",
            status="fail",
            detail="Container health check failed.",
        ),
        locator_factory=lambda compose_id: _Locator(
            project_id=locator.project_id,
            environment_id=locator.environment_id,
            compose_id=compose_id,
        ),
    )

    assert result.status == "applied"
    assert result.locator == locator
    client.assert_single_update_deploy_pair(service_name)


def test_changed_hash_triggers_deploy(tmp_path: Path) -> None:
    service_name = "wizard-stack-coder"
    _write_hash_checkpoint(
        tmp_path,
        service_key=service_name,
        rendered_compose="services:\n  coder:\n    image: ghcr.io/coder/coder:old\n",
    )
    compose_file = "services:\n  coder:\n    image: ghcr.io/coder/coder:new\n"
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name=service_name,
        compose_id="cmp-coder",
        project_name="wizard-stack",
    )
    locator = _Locator(project_id="proj-1", environment_id="env-1", compose_id="cmp-coder")

    result = apply_compose_noop_guard(
        rendered_compose=compose_file,
        service_key=service_name,
        state_dir=tmp_path,
        client=client,
        locator=locator,
        compose_id=locator.compose_id,
        title="dokploy-wizard coder reconcile",
        description="Update Coder compose app",
        verify_current=lambda: True,
        locator_factory=lambda compose_id: _Locator(
            project_id=locator.project_id,
            environment_id=locator.environment_id,
            compose_id=compose_id,
        ),
    )

    assert result.status == "applied"
    client.assert_single_update_deploy_pair(service_name)


def test_missing_hash_triggers_deploy_and_persists_new_hash(tmp_path: Path) -> None:
    service_name = "wizard-stack-shared"
    _write_empty_checkpoint(tmp_path)
    compose_file = "services:\n  postgres:\n    image: postgres:16\n"
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name=service_name,
        compose_id="cmp-shared",
        project_name="wizard-stack",
    )
    locator = _Locator(project_id="proj-1", environment_id="env-1", compose_id="cmp-shared")

    result = apply_compose_noop_guard(
        rendered_compose=compose_file,
        service_key=service_name,
        state_dir=tmp_path,
        client=client,
        locator=locator,
        compose_id=locator.compose_id,
        title="dokploy-wizard shared core reconcile",
        description="Update shared core compose app",
        verify_current=lambda: True,
        locator_factory=lambda compose_id: _Locator(
            project_id=locator.project_id,
            environment_id=locator.environment_id,
            compose_id=compose_id,
        ),
    )

    assert result.status == "applied"
    client.assert_single_update_deploy_pair(service_name)
    applied_state = load_state_dir(tmp_path).applied_state
    assert applied_state is not None
    assert applied_state.compose_artifact_hashes[service_name] == ComposeArtifactHashState.from_rendered_compose(
        service_id=service_name,
        rendered_compose=compose_file,
    )


def _write_empty_checkpoint(state_dir: Path) -> None:
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint="fingerprint",
            completed_steps=("shared_core",),
        ),
    )


def _write_hash_checkpoint(state_dir: Path, *, service_key: str, rendered_compose: str) -> None:
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint="fingerprint",
            completed_steps=("shared_core",),
            compose_artifact_hashes={
                service_key: ComposeArtifactHashState.from_rendered_compose(
                    service_id=service_key,
                    rendered_compose=rendered_compose,
                )
            },
        ),
    )
