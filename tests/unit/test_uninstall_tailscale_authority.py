from __future__ import annotations

from pathlib import Path

from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    OwnedResource,
    OwnershipLedger,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.tailscale import TailscaleManagedResource
from dokploy_wizard.uninstall.executor import ShellUninstallBackend, execute_uninstall_plan
from dokploy_wizard.uninstall.planner import PlannedDeletion, UninstallPlan
from dokploy_wizard.uninstall.providers import (
    UninstallProviderClients,
    tailscale_fingerprint,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
_OWNER_ID = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"


class TailscaleClient:
    def __init__(self, node: TailscaleManagedResource) -> None:
        self._node: TailscaleManagedResource | None = node
        self.calls: list[str] = []

    def get_node(self, resource_id: str) -> TailscaleManagedResource | None:
        assert resource_id == "node-1"
        self.calls.append("get_node")
        return self._node

    def disconnect(self) -> None:
        self.calls.append("disconnect")
        self._node = None


def test_default_backend_deletes_receipt_authorized_tailscale_node_after_absence_reread(
    tmp_path: Path,
) -> None:
    raw = parse_env_file(_FIXTURES / "nextcloud.env")
    desired = resolve_desired_state(raw)
    resource = OwnedResource(
        resource_type="tailscale_node",
        resource_id="node-1",
        scope="stack:nextcloud-stack:tailscale",
    )
    ledger = OwnershipLedger(format_version=1, resources=(resource,))
    node = TailscaleManagedResource(
        action="create",
        resource_id="node-1",
        resource_name="node-1",
    )
    authorities = UninstallAuthorityStore(tmp_path)
    authorities.record_created(
        resource=resource,
        owner_id=_OWNER_ID,
        provider="tailscale",
        physical_target_id=node.resource_id,
        parent_target_id=resource.scope,
        expected_fingerprint=tailscale_fingerprint(node),
    )
    write_target_state(tmp_path, raw, desired)
    write_applied_checkpoint(
        tmp_path,
        AppliedStateCheckpoint(
            format_version=desired.format_version,
            desired_state_fingerprint=desired.fingerprint(),
            completed_steps=("preflight",),
            runtime_images=desired.runtime_images,
        ),
    )
    write_ownership_ledger(tmp_path, ledger)
    plan = UninstallPlan(
        mode="retain",
        environment=desired.stack_name,
        deletions=(
            PlannedDeletion(resource=resource, phase="tailscale", policy="retain_safe"),
        ),
        retained_resources=(),
        warnings=(),
    )
    tailscale = TailscaleClient(node)
    backend = ShellUninstallBackend(
        raw,
        state_dir=tmp_path,
        providers=UninstallProviderClients(tailscale=tailscale),
    )

    result = execute_uninstall_plan(
        state_dir=tmp_path,
        raw_input=raw,
        desired_state=desired,
        ownership_ledger=ledger,
        plan=plan,
        backend=backend,
        dry_run=False,
    )

    assert tailscale.calls == ["get_node", "disconnect", "get_node"]
    assert result.deleted_resources == plan.deletions
    assert result.state_cleared is True
    assert authorities.load_deletion(resource) is not None
