# mypy: ignore-errors
# ruff: noqa: E501
# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dokploy_wizard.cli import run_install_flow, run_modify_flow
from dokploy_wizard.packs.surfsense import (
    SURFSENSE_DATA_RESOURCE_TYPE,
    SURFSENSE_SERVICE_RESOURCE_TYPE,
    SurfSenseBootstrapState,
    SurfSenseResourceRecord,
)
from dokploy_wizard.state import RawEnvInput, load_state_dir
from tests.integration.test_openclaw_pack import (
    FakeCloudflareBackend,
    FakeDokployBackend,
    FakeHeadscaleBackend,
    FakeMatrixBackend,
    FakeSharedCoreBackend,
    _base_install_values,
)


@dataclass
class RecordingSurfSenseBackend:
    existing_service: SurfSenseResourceRecord | None = None
    existing_data: SurfSenseResourceRecord | None = None
    health_ok: bool = True
    create_service_calls: list[dict[str, Any]] = field(default_factory=list)
    update_service_calls: list[dict[str, Any]] = field(default_factory=list)
    create_data_calls: list[str] = field(default_factory=list)
    health_urls: list[str] = field(default_factory=list)
    bootstrap_calls: int = 0

    def get_service(self, resource_id: str) -> SurfSenseResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> SurfSenseResourceRecord | None:
        if (
            self.existing_service is not None
            and self.existing_service.resource_name == resource_name
        ):
            return self.existing_service
        return None

    def create_service(self, **kwargs: Any) -> SurfSenseResourceRecord:
        self.create_service_calls.append(dict(kwargs))
        self.existing_service = SurfSenseResourceRecord(
            resource_id="surfsense-service-1",
            resource_name=str(kwargs["resource_name"]),
        )
        return self.existing_service

    def update_service(self, **kwargs: Any) -> SurfSenseResourceRecord:
        self.update_service_calls.append(dict(kwargs))
        self.existing_service = SurfSenseResourceRecord(
            resource_id=str(kwargs["resource_id"]),
            resource_name=str(kwargs["resource_name"]),
        )
        return self.existing_service

    def get_persistent_data(self, resource_id: str) -> SurfSenseResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_id == resource_id:
            return self.existing_data
        return None

    def find_persistent_data_by_name(self, resource_name: str) -> SurfSenseResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_name == resource_name:
            return self.existing_data
        return None

    def create_persistent_data(self, resource_name: str) -> SurfSenseResourceRecord:
        self.create_data_calls.append(resource_name)
        self.existing_data = SurfSenseResourceRecord(
            resource_id="surfsense-data-1",
            resource_name=resource_name,
        )
        return self.existing_data

    def check_health(self, *, service: SurfSenseResourceRecord, url: str) -> bool:
        del service
        self.health_urls.append(url)
        return self.health_ok

    def check_internal_health(self, *, service: SurfSenseResourceRecord, url: str) -> bool:
        del service, url
        return self.health_ok

    def ensure_application_ready(self) -> tuple[SurfSenseBootstrapState, tuple[str, ...]]:
        self.bootstrap_calls += 1
        return SurfSenseBootstrapState(created=True, verified_existing=False), (
            "SurfSense bootstrap completed through fake integration backend.",
        )


def _surfsense_raw_env(**overrides: str) -> RawEnvInput:
    values = _base_install_values(
        PACKS="surfsense",
        LITELLM_OPENROUTER_API_KEY="SECRET_TEST_OPENROUTER_PROVIDER_KEY",
        OPENROUTER_API_KEY="SECRET_TEST_OPENROUTER_PROVIDER_KEY",
        LITELLM_LOCAL_API_KEY="SECRET_TEST_LOCAL_PROVIDER_KEY",
        AI_DEFAULT_API_KEY="SECRET_TEST_AI_DEFAULT_PROVIDER_KEY",
    )
    values.update(overrides)
    return RawEnvInput(
        format_version=1,
        values=values,
    )


def test_install_fresh_surfsense_persists_state_and_keeps_cloudflare_access_app_login_only(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "surfsense.env"
    networking_backend = FakeCloudflareBackend()
    shared_core_backend = FakeSharedCoreBackend()
    surfsense_backend = RecordingSurfSenseBackend()

    summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=_surfsense_raw_env(),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=FakeHeadscaleBackend(),
        matrix_backend=FakeMatrixBackend(),
        surfsense_backend=surfsense_backend,
    )
    loaded_state = load_state_dir(state_dir)

    assert summary["lifecycle"]["phases_to_run"] == [
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "surfsense",
    ]
    assert summary["surfsense"]["outcome"] == "applied"
    assert summary["surfsense"]["frontend_hostname"] == "surfsense.example.com"
    assert summary["surfsense"]["api_hostname"] == "surfsense-api.example.com"
    assert summary["surfsense"]["zero_hostname"] == "surfsense-zero.example.com"
    assert summary["surfsense"]["bootstrap_state"] == {
        "created": True,
        "verified_existing": False,
    }
    assert summary["surfsense"]["config"]["endpoints"] == {
        "frontend_url": "https://surfsense.example.com",
        "api_url": "https://surfsense-api.example.com",
        "zero_url": "https://surfsense-zero.example.com",
    }
    assert surfsense_backend.create_data_calls == ["wizard-stack-surfsense-data"]
    assert surfsense_backend.create_service_calls[0]["resource_name"] == "wizard-stack-surfsense"
    assert surfsense_backend.create_service_calls[0]["frontend_hostname"] == "surfsense.example.com"
    assert surfsense_backend.create_service_calls[0]["api_hostname"] == "surfsense-api.example.com"
    assert surfsense_backend.create_service_calls[0]["zero_hostname"] == "surfsense-zero.example.com"
    assert surfsense_backend.create_service_calls[0]["postgres_service_name"] == "wizard-stack-shared-postgres"
    assert surfsense_backend.create_service_calls[0]["redis_service_name"] == "wizard-stack-shared-redis"
    assert surfsense_backend.health_urls == ["https://surfsense-api.example.com/ready"]
    assert surfsense_backend.bootstrap_calls == 1

    assert networking_backend.access_apps == {}
    assert not any("surfsense" in hostname for hostname in networking_backend.access_apps)
    assert {
        "surfsense.example.com",
        "surfsense-api.example.com",
        "surfsense-zero.example.com",
    } <= set(networking_backend.dns_records)

    assert loaded_state.applied_state is not None
    assert loaded_state.applied_state.completed_steps == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "surfsense",
    )
    assert loaded_state.ownership_ledger is not None
    assert {
        (resource.resource_type, resource.scope)
        for resource in loaded_state.ownership_ledger.resources
        if resource.resource_type in {SURFSENSE_SERVICE_RESOURCE_TYPE, SURFSENSE_DATA_RESOURCE_TYPE}
    } == {
        (SURFSENSE_SERVICE_RESOURCE_TYPE, "stack:wizard-stack:surfsense:service"),
        (SURFSENSE_DATA_RESOURCE_TYPE, "stack:wizard-stack:surfsense:data"),
    }


def test_modify_removing_surfsense_deletes_runtime_and_retains_data(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "surfsense-remove.env"
    networking_backend = FakeCloudflareBackend()
    shared_core_backend = FakeSharedCoreBackend()
    surfsense_backend = RecordingSurfSenseBackend()

    run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=_surfsense_raw_env(),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=FakeHeadscaleBackend(),
        matrix_backend=FakeMatrixBackend(),
        surfsense_backend=surfsense_backend,
    )

    summary = run_modify_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=_surfsense_raw_env(PACKS=""),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=FakeHeadscaleBackend(),
        matrix_backend=FakeMatrixBackend(),
        surfsense_backend=surfsense_backend,
    )
    loaded_state = load_state_dir(state_dir)
    deleted_types = {
        item["resource_type"]
        for item in summary["disable_teardown"]["executed"]["deleted_resources"]
    }

    assert summary["lifecycle"]["mode"] == "modify"
    assert summary["lifecycle"]["phases_to_run"] == ["networking", "shared_core"]
    assert deleted_types == {"cloudflare_dns_record", SURFSENSE_SERVICE_RESOURCE_TYPE}
    assert loaded_state.ownership_ledger is not None
    assert not any(
        resource.resource_type == SURFSENSE_SERVICE_RESOURCE_TYPE
        for resource in loaded_state.ownership_ledger.resources
    )
    assert any(
        resource.resource_type == SURFSENSE_DATA_RESOURCE_TYPE
        for resource in loaded_state.ownership_ledger.resources
    )
    assert surfsense_backend.create_service_calls[0]["resource_name"] == "wizard-stack-surfsense"
    assert surfsense_backend.update_service_calls == []


def test_modify_surfsense_version_change_reconciles_existing_runtime(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "surfsense-version.env"
    networking_backend = FakeCloudflareBackend()
    shared_core_backend = FakeSharedCoreBackend()
    surfsense_backend = RecordingSurfSenseBackend()

    run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=_surfsense_raw_env(SURFSENSE_VERSION="0.0.25"),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=FakeHeadscaleBackend(),
        matrix_backend=FakeMatrixBackend(),
        surfsense_backend=surfsense_backend,
    )

    summary = run_modify_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=_surfsense_raw_env(SURFSENSE_VERSION="0.0.26"),
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=shared_core_backend,
        headscale_backend=FakeHeadscaleBackend(),
        matrix_backend=FakeMatrixBackend(),
        surfsense_backend=surfsense_backend,
    )

    assert summary["lifecycle"]["mode"] == "modify"
    assert summary["lifecycle"]["phases_to_run"] == ["surfsense"]
    assert surfsense_backend.update_service_calls[-1]["resource_name"] == "wizard-stack-surfsense"
