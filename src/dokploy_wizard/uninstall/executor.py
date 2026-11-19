"""Execution helpers for safe uninstall flows."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from dokploy_wizard.networking.edge_https import EDGE_HTTPS_FILENAME, TRAEFIK_DYNAMIC_DIR
from dokploy_wizard.state import (
    LIFECYCLE_CHECKPOINT_CONTRACT_VERSION,
    AppliedStateCheckpoint,
    DesiredState,
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    clear_state_documents,
    write_applied_checkpoint,
    write_ownership_ledger,
)
from dokploy_wizard.tailscale import TAILSCALE_NODE_RESOURCE_TYPE
from dokploy_wizard.uninstall.planner import (
    PlannedDeletion,
    UninstallPlan,
    compute_remaining_completed_steps,
)


class UninstallExecutionError(RuntimeError):
    """Raised when a resource deletion fails during uninstall."""


class UninstallBackend(Protocol):
    def delete(self, deletion: PlannedDeletion) -> None: ...


ACCESS_APPLICATION_RESOURCE_TYPE = "cloudflare_access_application"
ACCESS_POLICY_RESOURCE_TYPE = "cloudflare_access_policy"
_ACCESS_DELETION_TYPES = frozenset({ACCESS_APPLICATION_RESOURCE_TYPE, ACCESS_POLICY_RESOURCE_TYPE})


def _parse_access_scope(scope: str) -> tuple[str, str] | None:
    """Return (account_id, hostname) from an access ledger scope string.

    Scopes look like ``account:<id>:access-app:<hostname>`` (or
    ``access-policy``); the provider scope carries no hostname and returns None.
    """

    parts = scope.split(":")
    if len(parts) != 4 or parts[0] != "account" or parts[2] not in {"access-app", "access-policy"}:
        return None
    return parts[1], parts[3]


class ShellUninstallBackend:
    """Deterministic default backend for ledger-driven teardown execution."""

    def __init__(
        self,
        raw_input: RawEnvInput,
        cloudflare_backend: Any | None = None,
    ) -> None:
        values = raw_input.values
        self._cloudflare_backend = cloudflare_backend
        self._failing_types = {
            item.strip()
            for item in values.get("UNINSTALL_FAIL_RESOURCE_TYPES", "").split(",")
            if item.strip() != ""
        }
        self._failing_ids = {
            item.strip()
            for item in values.get("UNINSTALL_FAIL_RESOURCE_IDS", "").split(",")
            if item.strip() != ""
        }

    def delete(self, deletion: PlannedDeletion) -> None:
        resource = deletion.resource
        if resource.resource_type == TAILSCALE_NODE_RESOURCE_TYPE:
            result = subprocess.run(
                ["tailscale", "down"],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                msg = "tailscale down failed during uninstall"
                if stderr:
                    msg = f"{msg}: {stderr}"
                raise UninstallExecutionError(msg)
            return
        if resource.resource_type in _ACCESS_DELETION_TYPES:
            self._delete_access_resource(resource)
            return
        if resource.resource_type in self._failing_types:
            raise UninstallExecutionError(
                "Simulated uninstall failure for resource type "
                f"'{deletion.resource.resource_type}'."
            )
        if deletion.resource.resource_id in self._failing_ids:
            raise UninstallExecutionError(
                f"Simulated uninstall failure for resource id '{deletion.resource.resource_id}'."
            )

    def _delete_access_resource(self, resource: OwnedResource) -> None:
        # Disabling Access OTP must take the application and policy down with
        # it; otherwise the hostname stays gated even though the wizard no
        # longer manages or intends the policy.
        if self._cloudflare_backend is None:
            return
        parsed = _parse_access_scope(resource.scope)
        if parsed is None:
            return
        account_id, hostname = parsed
        if resource.resource_type == ACCESS_APPLICATION_RESOURCE_TYPE:
            self._cloudflare_backend.delete_access_application(account_id, resource.resource_id)
            return
        app = self._cloudflare_backend.find_access_application_by_domain(account_id, hostname)
        if app is None:
            # Cloudflare cascades policies with the application.
            return
        self._cloudflare_backend.delete_access_policy(account_id, app.app_id, resource.resource_id)


@dataclass(frozen=True)
class UninstallExecutionResult:
    deleted_resources: tuple[PlannedDeletion, ...]
    remaining_completed_steps: tuple[str, ...]
    state_cleared: bool
    reclaimed_docker_state: dict[str, list[str]] | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "deleted_resources": [item.to_dict() for item in self.deleted_resources],
            "remaining_completed_steps": list(self.remaining_completed_steps),
            "state_cleared": self.state_cleared,
        }
        if self.reclaimed_docker_state is not None:
            payload["reclaimed_docker_state"] = self.reclaimed_docker_state
        return payload


def _docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=False, capture_output=True, text=True)


def _reclaim_docker_state(*, stack_name: str, destroy_volumes: bool) -> dict[str, list[str]]:
    """Reclaim daemon state Dokploy's stack teardown abandons: project networks,
    and (destroy mode) named volumes.

    Measured on the zero-state loop, iteration 4: every compose project leaves
    its `<project>_default` bridge network when its services go away, and
    stack removal spares named volumes (the same leak iteration 2 caught —
    the ledger records them, but Dokploy's own teardown never deletes them).
    The wizard owns these by name: they all carry the stack prefix. After
    four install generations the leaked allocations drained Docker's
    predefined address pool, and the next stack died at deploy with 'all
    predefined address pools have been fully subnetted' (35 networks stood on
    the box, ~31 of them stale wizard-era generations). The sweep is
    best-effort (an in-use resource belongs to a stack that is still tearing
    down; skip, the next pass retries) and never fails a teardown.
    """
    reclaimed: dict[str, list[str]] = {"networks": [], "volumes": [], "skipped": []}
    try:
        networks = _docker(["network", "ls", "-q", "--filter", f"name=^{stack_name}-"])
    except FileNotFoundError:
        return reclaimed
    for network_id in networks.stdout.split():
        if _docker(["network", "rm", network_id]).returncode == 0:
            reclaimed["networks"].append(network_id)
        else:
            reclaimed["skipped"].append(network_id)
    if destroy_volumes:
        volumes = _docker(["volume", "ls", "-q", "--filter", f"name=^{stack_name}-"])
        for volume_name in volumes.stdout.split():
            if _docker(["volume", "rm", volume_name]).returncode == 0:
                reclaimed["volumes"].append(volume_name)
            else:
                reclaimed["skipped"].append(volume_name)
    return reclaimed


def execute_uninstall_plan(
    *,
    state_dir: Path,
    raw_input: RawEnvInput,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    plan: UninstallPlan,
    backend: UninstallBackend,
    dry_run: bool,
    traefik_dynamic_dir: Path | None = None,
) -> UninstallExecutionResult:
    if dry_run:
        return UninstallExecutionResult(
            deleted_resources=plan.deletions,
            remaining_completed_steps=_cap_completed_steps(
                compute_remaining_completed_steps(
                    desired_state=desired_state,
                    raw_input=raw_input,
                    ownership_ledger=ownership_ledger,
                ),
                plan.completed_steps_ceiling,
            ),
            state_cleared=False,
        )

    deleted_resources: list[PlannedDeletion] = []
    current_ledger = ownership_ledger
    remaining_completed_steps = _cap_completed_steps(
        compute_remaining_completed_steps(
            desired_state=desired_state,
            raw_input=raw_input,
            ownership_ledger=current_ledger,
        ),
        plan.completed_steps_ceiling,
    )
    for deletion in plan.deletions:
        backend.delete(deletion)
        deleted_resources.append(deletion)
        current_ledger = OwnershipLedger(
            format_version=current_ledger.format_version,
            resources=tuple(
                resource
                for resource in current_ledger.resources
                if not (
                    resource.resource_type == deletion.resource.resource_type
                    and resource.resource_id == deletion.resource.resource_id
                    and resource.scope == deletion.resource.scope
                )
            ),
        )
        write_ownership_ledger(state_dir, current_ledger)
        remaining_completed_steps = _cap_completed_steps(
            compute_remaining_completed_steps(
                desired_state=desired_state,
                raw_input=raw_input,
                ownership_ledger=current_ledger,
            ),
            plan.completed_steps_ceiling,
        )
        write_applied_checkpoint(
            state_dir,
            AppliedStateCheckpoint(
                format_version=desired_state.format_version,
                desired_state_fingerprint=desired_state.fingerprint(),
                completed_steps=remaining_completed_steps,
                lifecycle_checkpoint_contract_version=LIFECYCLE_CHECKPOINT_CONTRACT_VERSION,
            ),
        )

    # The Traefik HTTPS-edge drop-in is wizard-owned file state on the box.
    # Dokploy's own teardown usually removes the whole dynamic directory, but
    # a stale redirect router must never outlive the wizard, so remove ours.
    edge_dir = traefik_dynamic_dir or TRAEFIK_DYNAMIC_DIR
    try:
        (edge_dir / EDGE_HTTPS_FILENAME).unlink(missing_ok=True)
    except OSError:
        pass

    # Dokploy's stack teardown spares the compose project networks (and in
    # retain terms the named volumes): sweep the stack-prefixed leftovers or
    # every future install generation leaks another daemon allocation. See
    # _reclaim_docker_state for the iteration-4 measurement.
    reclaimed = _reclaim_docker_state(
        stack_name=plan.environment,
        destroy_volumes=plan.mode == "destroy",
    )

    state_cleared = not current_ledger.resources
    if state_cleared:
        clear_state_documents(state_dir)

    return UninstallExecutionResult(
        deleted_resources=tuple(deleted_resources),
        remaining_completed_steps=remaining_completed_steps,
        state_cleared=state_cleared,
        reclaimed_docker_state=reclaimed,
    )


def _cap_completed_steps(
    completed_steps: tuple[str, ...], ceiling: tuple[str, ...] | None
) -> tuple[str, ...]:
    if ceiling is None or len(completed_steps) <= len(ceiling):
        return completed_steps
    return ceiling
