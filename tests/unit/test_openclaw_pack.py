# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass

import pytest

from dokploy_wizard.packs.openclaw import (
    MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
    OPENCLAW_SERVICE_RESOURCE_TYPE,
    OpenClawError,
    OpenClawResourceRecord,
    build_my_farm_advisor_ledger,
    build_openclaw_ledger,
    reconcile_my_farm_advisor,
    reconcile_openclaw,
)
from dokploy_wizard.state import (
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    StateValidationError,
    resolve_desired_state,
)


@dataclass
class FakeOpenClawBackend:
    existing_service: OpenClawResourceRecord | None = None
    health_ok: bool = True
    create_calls: int = 0
    update_calls: int = 0
    last_requested_replicas: int | None = None

    def get_service(self, resource_id: str) -> OpenClawResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> OpenClawResourceRecord | None:
        if (
            self.existing_service is not None
            and self.existing_service.resource_name == resource_name
        ):
            return self.existing_service
        return None

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        template_path: object,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
        secret_refs: tuple[str, ...],
    ) -> OpenClawResourceRecord:
        del hostname, template_path, variant, channels, secret_refs
        self.create_calls += 1
        self.last_requested_replicas = replicas
        self.existing_service = OpenClawResourceRecord(
            resource_id="advisor-service-1",
            resource_name=resource_name,
            replicas=replicas,
        )
        return self.existing_service

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        template_path: object,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
        secret_refs: tuple[str, ...],
    ) -> OpenClawResourceRecord:
        del hostname, template_path, variant, channels, secret_refs
        self.update_calls += 1
        self.last_requested_replicas = replicas
        self.existing_service = OpenClawResourceRecord(
            resource_id=resource_id,
            resource_name=resource_name,
            replicas=replicas,
        )
        return self.existing_service

    def check_health(self, *, service: OpenClawResourceRecord, url: str) -> bool:
        del service, url
        return self.health_ok


def test_reconcile_openclaw_plans_slot_runtime_for_openclaw_variant() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
                "OPENCLAW_REPLICAS": "2",
            },
        )
    )

    phase = reconcile_openclaw(
        dry_run=True,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeOpenClawBackend(),
    )

    assert phase.result.outcome == "plan_only"
    assert phase.result.enabled is True
    assert phase.result.variant == "openclaw"
    assert phase.result.hostname == "openclaw.example.com"
    assert phase.result.channels == ("telegram",)
    assert phase.result.replicas == 2
    assert phase.result.template_path is not None
    assert phase.result.template_path.endswith("templates/packs/openclaw.compose.yaml")
    assert phase.result.service is not None
    assert phase.result.service.resource_name == "wizard-stack-advisor"
    assert phase.result.health_check is not None
    assert phase.result.health_check.passed is None


def test_resolve_desired_state_rejects_matrix_channel_without_matrix_pack() -> None:
    with pytest.raises(StateValidationError, match="Matrix pack"):
        resolve_desired_state(
            RawEnvInput(
                format_version=1,
                values={
                    "STACK_NAME": "wizard-stack",
                    "ROOT_DOMAIN": "example.com",
                    "ENABLE_OPENCLAW": "true",
                    "OPENCLAW_CHANNELS": "matrix",
                },
            )
        )


def test_resolve_desired_state_rejects_unsupported_advisor_channel() -> None:
    with pytest.raises(StateValidationError, match="Unsupported my-farm-advisor channel"):
        resolve_desired_state(
            RawEnvInput(
                format_version=1,
                values={
                    "STACK_NAME": "wizard-stack",
                    "ROOT_DOMAIN": "example.com",
                    "ENABLE_MY_FARM_ADVISOR": "true",
                    "MY_FARM_ADVISOR_CHANNELS": "email",
                },
            )
        )


def test_resolve_desired_state_rejects_openclaw_replicas_without_advisor_pack() -> None:
    with pytest.raises(StateValidationError, match="OPENCLAW_REPLICAS requires"):
        resolve_desired_state(
            RawEnvInput(
                format_version=1,
                values={
                    "STACK_NAME": "wizard-stack",
                    "ROOT_DOMAIN": "example.com",
                    "OPENCLAW_REPLICAS": "2",
                },
            )
        )


def test_reconcile_my_farm_advisor_plans_runtime_independently() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MY_FARM_ADVISOR": "true",
                "ENABLE_MATRIX": "true",
                "MY_FARM_ADVISOR_CHANNELS": "telegram,matrix",
            },
        )
    )

    phase = reconcile_my_farm_advisor(
        dry_run=True,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeOpenClawBackend(),
    )

    assert phase.result.variant == "my-farm-advisor"
    assert phase.result.hostname == "farm.example.com"
    assert phase.result.channels == ("matrix", "telegram")
    assert phase.result.template_path is not None
    assert phase.result.template_path.endswith("templates/packs/my-farm-advisor.compose.yaml")
    assert phase.result.service is not None
    assert phase.result.service.resource_name == "wizard-stack-my-farm-advisor"


def test_resolve_desired_state_supports_both_advisor_packs_together() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "ENABLE_MY_FARM_ADVISOR": "true",
                "ENABLE_MATRIX": "true",
                "OPENCLAW_CHANNELS": "telegram",
                "MY_FARM_ADVISOR_CHANNELS": "telegram,matrix",
            },
        )
    )

    assert "openclaw" in desired_state.enabled_packs
    assert "my-farm-advisor" in desired_state.enabled_packs
    assert desired_state.openclaw_channels == ("telegram",)
    assert desired_state.my_farm_advisor_channels == ("matrix", "telegram")


def test_reconcile_openclaw_skips_when_openclaw_is_disabled() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
            },
        )
    )

    phase = reconcile_openclaw(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeOpenClawBackend(),
    )

    assert phase.result.outcome == "skipped"
    assert phase.result.enabled is False
    assert phase.service_resource_id is None


def test_reconcile_openclaw_reuses_owned_service_and_requires_health() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
            },
        )
    )
    backend = FakeOpenClawBackend(
        existing_service=OpenClawResourceRecord(
            resource_id="advisor-service-1",
            resource_name="wizard-stack-advisor",
            replicas=1,
        ),
        health_ok=True,
    )

    phase = reconcile_openclaw(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type=OPENCLAW_SERVICE_RESOURCE_TYPE,
                    resource_id="advisor-service-1",
                    scope="stack:wizard-stack:openclaw",
                ),
            ),
        ),
        backend=backend,
    )

    assert phase.result.outcome == "already_present"
    assert phase.result.service is not None
    assert phase.result.service.action == "reuse_owned"
    assert phase.result.health_check is not None
    assert phase.result.health_check.passed is True
    assert backend.create_calls == 0


def test_reconcile_openclaw_updates_owned_service_when_replicas_change() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
                "OPENCLAW_REPLICAS": "3",
            },
        )
    )
    backend = FakeOpenClawBackend(
        existing_service=OpenClawResourceRecord(
            resource_id="advisor-service-1",
            resource_name="wizard-stack-advisor",
            replicas=1,
        ),
        health_ok=True,
    )

    phase = reconcile_openclaw(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type=OPENCLAW_SERVICE_RESOURCE_TYPE,
                    resource_id="advisor-service-1",
                    scope="stack:wizard-stack:openclaw",
                ),
            ),
        ),
        backend=backend,
    )

    assert phase.result.outcome == "applied"
    assert phase.result.replicas == 3
    assert phase.result.service is not None
    assert phase.result.service.action == "update_owned"
    assert backend.update_calls == 1
    assert backend.last_requested_replicas == 3


def test_reconcile_openclaw_fails_closed_on_unowned_name_collision() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
            },
        )
    )

    with pytest.raises(OpenClawError, match="Refusing to adopt"):
        reconcile_openclaw(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeOpenClawBackend(
                existing_service=OpenClawResourceRecord(
                    resource_id="advisor-service-1",
                    resource_name="wizard-stack-advisor",
                    replicas=1,
                )
            ),
        )


def test_reconcile_openclaw_fails_closed_on_health_check_failure() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_OPENCLAW": "true",
                "OPENCLAW_CHANNELS": "telegram",
            },
        )
    )

    with pytest.raises(OpenClawError, match="health check failed"):
        reconcile_openclaw(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeOpenClawBackend(health_ok=False),
        )


def test_build_openclaw_ledger_persists_pack_specific_scope() -> None:
    updated = build_openclaw_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name="wizard-stack",
        service_resource_id="advisor-service-1",
    )

    assert updated.resources == (
        OwnedResource(
            resource_type=OPENCLAW_SERVICE_RESOURCE_TYPE,
            resource_id="advisor-service-1",
            scope="stack:wizard-stack:openclaw",
        ),
    )


def test_build_my_farm_advisor_ledger_persists_pack_specific_scope() -> None:
    updated = build_my_farm_advisor_ledger(
        existing_ledger=OwnershipLedger(format_version=1, resources=()),
        stack_name="wizard-stack",
        service_resource_id="farm-service-1",
    )

    assert updated.resources == (
        OwnedResource(
            resource_type=MY_FARM_ADVISOR_SERVICE_RESOURCE_TYPE,
            resource_id="farm-service-1",
            scope="stack:wizard-stack:my-farm-advisor",
        ),
    )
