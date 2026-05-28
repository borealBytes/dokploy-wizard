from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.dokploy.compose_noop import (
    apply_compose_noop_guard,
    persist_compose_artifact_hash,
)
from dokploy_wizard.dokploy.env_spec import DokployEnvSpec, DokployEnvVar, RenderedCompose
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
    expected_hash_state = ComposeArtifactHashState.from_rendered_compose(
        service_id=service_name,
        rendered_compose=compose_file,
    )
    assert applied_state.compose_artifact_hashes[service_name] == expected_hash_state


def test_matching_rendered_compose_and_env_metadata_skips_mutation(tmp_path: Path) -> None:
    service_name = "wizard-stack-openclaw"
    rendered = _rendered_env_compose(value="initial-secret")
    _write_empty_checkpoint(tmp_path)
    persist_compose_artifact_hash(
        state_dir=tmp_path,
        service_key=service_name,
        rendered_compose=rendered,
    )
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name=service_name,
        compose_id="cmp-openclaw",
        project_name="wizard-stack",
        compose_file=rendered.compose_file,
    )
    locator = _Locator(project_id="proj-1", environment_id="env-1", compose_id="cmp-openclaw")

    result = apply_compose_noop_guard(
        rendered_compose=rendered,
        service_key=service_name,
        state_dir=tmp_path,
        client=client,
        locator=locator,
        compose_id=locator.compose_id,
        title="dokploy-wizard openclaw reconcile",
        description="Update OpenClaw compose app",
        verify_current=lambda: True,
        locator_factory=lambda compose_id: _Locator(
            project_id=locator.project_id,
            environment_id=locator.environment_id,
            compose_id=compose_id,
        ),
    )

    assert result.status == "already_present"
    client.assert_unchanged_service(service_name)


def test_legacy_compose_hash_without_env_metadata_reconciles_rendered_env(
    tmp_path: Path,
) -> None:
    service_name = "wizard-stack-openclaw"
    rendered = _rendered_env_compose(value="current-secret")
    _write_hash_checkpoint(
        tmp_path,
        service_key=service_name,
        rendered_compose=rendered.compose_file,
    )
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name=service_name,
        compose_id="cmp-openclaw",
        project_name="wizard-stack",
        compose_file=rendered.compose_file,
    )
    locator = _Locator(project_id="proj-1", environment_id="env-1", compose_id="cmp-openclaw")

    result = apply_compose_noop_guard(
        rendered_compose=rendered,
        service_key=service_name,
        state_dir=tmp_path,
        client=client,
        locator=locator,
        compose_id=locator.compose_id,
        title="dokploy-wizard openclaw reconcile",
        description="Update OpenClaw compose app",
        verify_current=lambda: True,
        locator_factory=lambda compose_id: _Locator(
            project_id=locator.project_id,
            environment_id=locator.environment_id,
            compose_id=compose_id,
        ),
    )

    assert result.status == "applied"
    client.assert_single_update_deploy_pair(service_name)
    assert "OPENCLAW_GATEWAY_TOKEN=current-secret" in client.compose_env_by_name[service_name]


def test_env_metadata_change_triggers_reconciliation_without_compose_change(
    tmp_path: Path,
) -> None:
    service_name = "wizard-stack-openclaw"
    original = _rendered_env_compose(value="stable-secret", source="operator-input")
    changed = _rendered_env_compose(value="stable-secret", source="generated-runtime")
    _write_empty_checkpoint(tmp_path)
    persist_compose_artifact_hash(
        state_dir=tmp_path,
        service_key=service_name,
        rendered_compose=original,
    )
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name=service_name,
        compose_id="cmp-openclaw",
        project_name="wizard-stack",
        compose_file=changed.compose_file,
    )
    locator = _Locator(project_id="proj-1", environment_id="env-1", compose_id="cmp-openclaw")

    result = apply_compose_noop_guard(
        rendered_compose=changed,
        service_key=service_name,
        state_dir=tmp_path,
        client=client,
        locator=locator,
        compose_id=locator.compose_id,
        title="dokploy-wizard openclaw reconcile",
        description="Update OpenClaw compose app",
        verify_current=lambda: True,
        locator_factory=lambda compose_id: _Locator(
            project_id=locator.project_id,
            environment_id=locator.environment_id,
            compose_id=compose_id,
        ),
    )

    assert result.status == "applied"
    client.assert_single_update_deploy_pair(service_name)


def test_env_value_change_reconciles_without_persisting_raw_values(tmp_path: Path) -> None:
    service_name = "wizard-stack-openclaw"
    old_secret = "old-runtime-secret"
    new_secret = "new-runtime-secret"
    original = _rendered_env_compose(value=old_secret)
    changed = _rendered_env_compose(value=new_secret)
    _write_empty_checkpoint(tmp_path)
    persist_compose_artifact_hash(
        state_dir=tmp_path,
        service_key=service_name,
        rendered_compose=original,
    )
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name=service_name,
        compose_id="cmp-openclaw",
        project_name="wizard-stack",
        compose_file=changed.compose_file,
    )
    locator = _Locator(project_id="proj-1", environment_id="env-1", compose_id="cmp-openclaw")

    result = apply_compose_noop_guard(
        rendered_compose=changed,
        service_key=service_name,
        state_dir=tmp_path,
        client=client,
        locator=locator,
        compose_id=locator.compose_id,
        title="dokploy-wizard openclaw reconcile",
        description="Update OpenClaw compose app",
        verify_current=lambda: True,
        locator_factory=lambda compose_id: _Locator(
            project_id=locator.project_id,
            environment_id=locator.environment_id,
            compose_id=compose_id,
        ),
    )

    serialized_state = (tmp_path / "applied-state.json").read_text(encoding="utf-8")
    assert result.status == "applied"
    assert f"OPENCLAW_GATEWAY_TOKEN={new_secret}" in client.compose_env_by_name[service_name]
    assert old_secret not in serialized_state
    assert new_secret not in serialized_state
    assert "redacted_fingerprint" in serialized_state


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


def _rendered_env_compose(
    *, value: str, source: str = "operator-input"
) -> RenderedCompose:
    placeholder = "${OPENCLAW_GATEWAY_TOKEN:?OPENCLAW_GATEWAY_TOKEN is required}"
    return RenderedCompose(
        compose_file=(
            "services:\n"
            "  app:\n"
            "    image: ghcr.io/borealbytes/openclaw:latest\n"
            "    environment:\n"
            f"      OPENCLAW_GATEWAY_TOKEN: \"{placeholder}\"\n"
        ),
        env_specs=(
            DokployEnvSpec(
                variable=DokployEnvVar(
                    name="OPENCLAW_GATEWAY_TOKEN",
                    value=value,
                    sensitive=True,
                    source=source,
                ),
                owner="openclaw",
                target_services=("app",),
                placeholder=placeholder,
            ),
        ),
    )
