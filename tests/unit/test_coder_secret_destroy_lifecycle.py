from __future__ import annotations

from pathlib import Path

import pytest

from dokploy_wizard.dokploy.coder_secret_destroy import CoderSecretDestroyer
from dokploy_wizard.packs.coder import CODER_DATA_RESOURCE_TYPE, CODER_SERVICE_RESOURCE_TYPE
from dokploy_wizard.state import DesiredState, OwnedResource, OwnershipLedger, RawEnvInput
from dokploy_wizard.state.env import resolve_desired_state
from dokploy_wizard.uninstall.executor import execute_uninstall_plan
from dokploy_wizard.uninstall.planner import (
    PlannedDeletion,
    build_pack_disable_plan,
    build_uninstall_plan,
)

from .coder_secret_destroy_support import (
    OWNER_ID,
    SECRET_IDENTITIES,
    SecretDestroyClient,
    metadata,
    record_uninstall_deletion,
    seed_uninstall_authority,
    write_completed_secret_receipt,
    write_terminal_workspace_receipt,
)


class LifecycleBackend:
    def __init__(
        self,
        state_dir: Path,
        client: SecretDestroyClient,
        *,
        fail_service_delete_once: bool = False,
    ) -> None:
        self.state_dir = state_dir
        self.client = client
        self.fail_service_delete_once = fail_service_delete_once
        self.events: list[str] = []

    def destroy_coder_secrets(self, *, desired_state: DesiredState) -> None:
        assert desired_state.hostnames["coder"] == "coder.example.test"
        self.events.append("secret-destroy:start")
        CoderSecretDestroyer(self.state_dir, self.client, OWNER_ID).destroy()
        self.events.append("secret-destroy:complete")

    def delete(self, deletion: PlannedDeletion) -> None:
        self.events.append(f"delete:{deletion.resource.resource_type}")
        if (
            deletion.resource.resource_type == CODER_SERVICE_RESOURCE_TYPE
            and self.fail_service_delete_once
        ):
            self.fail_service_delete_once = False
            raise RuntimeError("Coder service delete failed")
        record_uninstall_deletion(self.state_dir, deletion.resource)


def _raw(coder_enabled: bool) -> RawEnvInput:
    values = {"STACK_NAME": "stack", "ROOT_DOMAIN": "example.test"}
    if coder_enabled:
        values["ENABLE_CODER"] = "true"
    return RawEnvInput(format_version=1, values=values)


def _ledger() -> OwnershipLedger:
    return OwnershipLedger(
        format_version=1,
        resources=(
            OwnedResource(CODER_SERVICE_RESOURCE_TYPE, "coder-service", "stack:stack:coder"),
            OwnedResource(CODER_DATA_RESOURCE_TYPE, "coder-data", "stack:stack:coder-data"),
        ),
    )


def _seed_receipts(state_dir: Path) -> None:
    write_completed_secret_receipt(state_dir)
    write_terminal_workspace_receipt(state_dir)
    seed_uninstall_authority(state_dir, _ledger().resources)


def _client() -> SecretDestroyClient:
    return SecretDestroyClient([metadata(identity) for identity in SECRET_IDENTITIES])


def test_retain_uninstall_preserves_all_coder_secrets_and_receipts(tmp_path: Path) -> None:
    raw = _raw(coder_enabled=True)
    desired = resolve_desired_state(raw)
    ledger = _ledger()
    _seed_receipts(tmp_path)
    backend = LifecycleBackend(tmp_path, _client())
    plan = build_uninstall_plan(
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        destroy_data=False,
    )

    execute_uninstall_plan(
        state_dir=tmp_path,
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        plan=plan,
        backend=backend,
        dry_run=False,
    )

    assert backend.events == [f"delete:{CODER_SERVICE_RESOURCE_TYPE}"]
    assert len(backend.client.secrets) == len(SECRET_IDENTITIES)
    assert (tmp_path / "coder-secret-receipts-v1.json").exists()
    assert (tmp_path / "coder-workspace-verification-receipt.json").exists()
    assert not (tmp_path / "coder-service-teardown-receipt-v1.json").exists()


def test_modify_disable_preserves_all_coder_secrets_and_receipts(tmp_path: Path) -> None:
    existing_raw = _raw(coder_enabled=True)
    requested_raw = _raw(coder_enabled=False)
    existing = resolve_desired_state(existing_raw)
    requested = resolve_desired_state(requested_raw)
    ledger = _ledger()
    _seed_receipts(tmp_path)
    backend = LifecycleBackend(tmp_path, _client())
    plan = build_pack_disable_plan(
        existing_desired=existing,
        requested_desired=requested,
        ownership_ledger=ledger,
    )

    execute_uninstall_plan(
        state_dir=tmp_path,
        raw_input=requested_raw,
        desired_state=requested,
        ownership_ledger=ledger,
        plan=plan,
        backend=backend,
        dry_run=False,
    )

    assert backend.events == [f"delete:{CODER_SERVICE_RESOURCE_TYPE}"]
    assert len(backend.client.secrets) == len(SECRET_IDENTITIES)
    assert (tmp_path / "coder-secret-receipts-v1.json").exists()
    assert (tmp_path / "coder-workspace-verification-receipt.json").exists()
    assert not (tmp_path / "coder-service-teardown-receipt-v1.json").exists()


def test_destroy_orders_secret_cleanup_before_coder_control_plane_delete(tmp_path: Path) -> None:
    raw = _raw(coder_enabled=True)
    desired = resolve_desired_state(raw)
    ledger = _ledger()
    _seed_receipts(tmp_path)
    backend = LifecycleBackend(tmp_path, _client())
    plan = build_uninstall_plan(
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        destroy_data=True,
    )

    execute_uninstall_plan(
        state_dir=tmp_path,
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        plan=plan,
        backend=backend,
        dry_run=False,
    )

    assert backend.events == [
        "secret-destroy:start",
        "secret-destroy:complete",
        f"delete:{CODER_SERVICE_RESOURCE_TYPE}",
        f"delete:{CODER_DATA_RESOURCE_TYPE}",
    ]
    assert backend.client.secrets == []
    assert not (tmp_path / "coder-workspace-verification-receipt.json").exists()
    assert not (tmp_path / "coder-secret-receipts-v1.json").exists()
    assert not (tmp_path / "coder-secret-destroy-receipts-v1.json").exists()
    assert not (tmp_path / "coder-service-teardown-receipt-v1.json").exists()


def test_service_delete_failure_preserves_completed_secret_authorization_for_retry(
    tmp_path: Path,
) -> None:
    raw = _raw(coder_enabled=True)
    desired = resolve_desired_state(raw)
    ledger = _ledger()
    _seed_receipts(tmp_path)
    backend = LifecycleBackend(tmp_path, _client(), fail_service_delete_once=True)
    plan = build_uninstall_plan(
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        destroy_data=True,
    )

    with pytest.raises(RuntimeError, match="service delete failed"):
        execute_uninstall_plan(
            state_dir=tmp_path,
            raw_input=raw,
            desired_state=desired,
            ownership_ledger=ledger,
            plan=plan,
            backend=backend,
            dry_run=False,
        )

    assert (tmp_path / "coder-secret-receipts-v1.json").exists()
    assert (tmp_path / "coder-secret-destroy-receipts-v1.json").exists()
    assert (tmp_path / "coder-service-teardown-receipt-v1.json").exists()

    api_events = tuple(backend.client.events)
    backend.client.api_available = False
    execute_uninstall_plan(
        state_dir=tmp_path,
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        plan=plan,
        backend=backend,
        dry_run=False,
    )

    assert tuple(backend.client.events) == api_events
    assert not (tmp_path / "coder-secret-receipts-v1.json").exists()
    assert not (tmp_path / "coder-secret-destroy-receipts-v1.json").exists()
    assert not (tmp_path / "coder-workspace-verification-receipt.json").exists()
    assert not (tmp_path / "coder-service-teardown-receipt-v1.json").exists()
