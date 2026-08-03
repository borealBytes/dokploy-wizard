from __future__ import annotations

from pathlib import Path

from dokploy_wizard.networking.cloudflare import (
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
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
from dokploy_wizard.uninstall.executor import ShellUninstallBackend, execute_uninstall_plan
from dokploy_wizard.uninstall.planner import PlannedDeletion, UninstallPlan
from dokploy_wizard.uninstall.providers import (
    UninstallProviderClients,
    dns_fingerprint,
    policy_fingerprint,
    tunnel_fingerprint,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
_OWNER_ID = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"


class CloudflareTunnelClient:
    def __init__(self, tunnel: CloudflareTunnel) -> None:
        self._tunnel: CloudflareTunnel | None = tunnel
        self.calls: list[str] = []

    def get_tunnel(self, account_id: str, tunnel_id: str) -> CloudflareTunnel | None:
        assert account_id == "account-123"
        assert tunnel_id == "tunnel-1"
        self.calls.append("get_tunnel")
        return self._tunnel

    def delete_tunnel(self, account_id: str, tunnel_id: str) -> None:
        assert account_id == "account-123"
        assert tunnel_id == "tunnel-1"
        self.calls.append("delete_tunnel")
        self._tunnel = None


class CloudflareDnsClient:
    def __init__(self, record: CloudflareDnsRecord) -> None:
        self._record: CloudflareDnsRecord | None = record
        self.calls: list[str] = []

    def get_dns_record(self, zone_id: str, record_id: str) -> CloudflareDnsRecord | None:
        assert zone_id == "zone-123"
        assert record_id == "dns-1"
        self.calls.append("get_dns_record")
        return self._record

    def delete_dns_record(self, zone_id: str, record_id: str) -> None:
        assert zone_id == "zone-123"
        assert record_id == "dns-1"
        self.calls.append("delete_dns_record")
        self._record = None


class CloudflarePolicyClient:
    def __init__(self, policy: CloudflareAccessPolicy) -> None:
        self._policy: CloudflareAccessPolicy | None = policy
        self.calls: list[str] = []

    def get_access_policy(
        self, account_id: str, app_id: str, policy_id: str
    ) -> CloudflareAccessPolicy | None:
        assert account_id == "account-123"
        assert app_id == "app-1"
        assert policy_id == "policy-1"
        self.calls.append("get_access_policy")
        return self._policy

    def delete_access_policy(self, account_id: str, app_id: str, policy_id: str) -> None:
        assert account_id == "account-123"
        assert app_id == "app-1"
        assert policy_id == "policy-1"
        self.calls.append("delete_access_policy")
        self._policy = None


def test_default_backend_deletes_receipt_authorized_cloudflare_tunnel_after_absence_reread(
    tmp_path: Path,
) -> None:
    raw = parse_env_file(_FIXTURES / "nextcloud.env")
    desired = resolve_desired_state(raw)
    resource = OwnedResource(
        resource_type="cloudflare_tunnel",
        resource_id="tunnel-1",
        scope="account:account-123",
    )
    ledger = OwnershipLedger(format_version=1, resources=(resource,))
    tunnel = CloudflareTunnel(tunnel_id="tunnel-1", name="wizard-tunnel")
    authorities = UninstallAuthorityStore(tmp_path)
    authorities.record_created(
        resource=resource,
        owner_id=_OWNER_ID,
        provider="cloudflare",
        physical_target_id=tunnel.tunnel_id,
        parent_target_id="account-123",
        expected_fingerprint=tunnel_fingerprint(tunnel),
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
            PlannedDeletion(resource=resource, phase="networking", policy="retain_safe"),
        ),
        retained_resources=(),
        warnings=(),
    )
    cloudflare = CloudflareTunnelClient(tunnel)
    backend = ShellUninstallBackend(
        raw,
        state_dir=tmp_path,
        providers=UninstallProviderClients(cloudflare=cloudflare),
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

    assert cloudflare.calls == ["get_tunnel", "delete_tunnel", "get_tunnel"]
    assert result.deleted_resources == plan.deletions
    assert result.state_cleared is True
    assert authorities.load_deletion(resource) is not None


def test_default_backend_deletes_receipt_authorized_cloudflare_policy_after_absence_reread(
    tmp_path: Path,
) -> None:
    raw = parse_env_file(_FIXTURES / "nextcloud.env")
    desired = resolve_desired_state(raw)
    resource = OwnedResource(
        resource_type="cloudflare_access_policy",
        resource_id="policy-1",
        scope="account:account-123:access-policy:openclaw.example.com",
    )
    ledger = OwnershipLedger(format_version=1, resources=(resource,))
    policy = CloudflareAccessPolicy(
        policy_id="policy-1",
        app_id="app-1",
        name="OpenClaw protected",
        decision="allow",
        emails=("operator@example.com",),
    )
    authorities = UninstallAuthorityStore(tmp_path)
    authorities.record_created(
        resource=resource,
        owner_id=_OWNER_ID,
        provider="cloudflare_access_policy",
        physical_target_id=policy.policy_id,
        parent_target_id=policy.app_id,
        expected_fingerprint=policy_fingerprint(policy),
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
            PlannedDeletion(resource=resource, phase="cloudflare_access", policy="retain_safe"),
        ),
        retained_resources=(),
        warnings=(),
    )
    cloudflare = CloudflarePolicyClient(policy)
    backend = ShellUninstallBackend(
        raw,
        state_dir=tmp_path,
        providers=UninstallProviderClients(cloudflare_access_policy=cloudflare),
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

    assert cloudflare.calls == [
        "get_access_policy",
        "delete_access_policy",
        "get_access_policy",
    ]
    assert result.deleted_resources == plan.deletions
    assert result.state_cleared is True
    assert authorities.load_deletion(resource) is not None


def test_default_backend_deletes_receipt_authorized_cloudflare_dns_after_absence_reread(
    tmp_path: Path,
) -> None:
    raw = parse_env_file(_FIXTURES / "nextcloud.env")
    desired = resolve_desired_state(raw)
    resource = OwnedResource(
        resource_type="cloudflare_dns_record",
        resource_id="dns-1",
        scope="zone:zone-123:nextcloud.example.com",
    )
    ledger = OwnershipLedger(format_version=1, resources=(resource,))
    record = CloudflareDnsRecord(
        record_id="dns-1",
        name="nextcloud.example.com",
        record_type="CNAME",
        content="tunnel-1.cfargotunnel.com",
        proxied=True,
    )
    authorities = UninstallAuthorityStore(tmp_path)
    authorities.record_created(
        resource=resource,
        owner_id=_OWNER_ID,
        provider="cloudflare_dns",
        physical_target_id=record.record_id,
        parent_target_id="zone-123",
        expected_fingerprint=dns_fingerprint(record),
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
            PlannedDeletion(resource=resource, phase="networking", policy="retain_safe"),
        ),
        retained_resources=(),
        warnings=(),
    )
    cloudflare = CloudflareDnsClient(record)
    backend = ShellUninstallBackend(
        raw,
        state_dir=tmp_path,
        providers=UninstallProviderClients(cloudflare_dns=cloudflare),
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

    assert cloudflare.calls == ["get_dns_record", "delete_dns_record", "get_dns_record"]
    assert result.deleted_resources == plan.deletions
    assert result.state_cleared is True
    assert authorities.load_deletion(resource) is not None
