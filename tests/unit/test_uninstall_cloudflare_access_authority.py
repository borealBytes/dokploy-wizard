from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.networking.cloudflare import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
)
from dokploy_wizard.state import OwnedResource, RawEnvInput
from dokploy_wizard.state.uninstall_authority import UninstallAuthorityStore
from dokploy_wizard.uninstall.cloudflare_access_teardown import CloudflareAccessUninstallTeardown
from dokploy_wizard.uninstall.planner import PlannedDeletion
from dokploy_wizard.uninstall.providers import (
    UninstallProviderClients,
    application_fingerprint,
    identity_provider_fingerprint,
)

_OWNER = "3b8e1e83-0e57-4d66-a65e-1edbf2aac838"


@dataclass
class AccessApplicationClient:
    value: CloudflareAccessApplication | None
    calls: list[str]

    def get_access_application(
        self, account_id: str, app_id: str
    ) -> CloudflareAccessApplication | None:
        assert (account_id, app_id) == ("account-1", "app-1")
        self.calls.append("get")
        return self.value

    def delete_access_application(self, account_id: str, app_id: str) -> None:
        assert (account_id, app_id) == ("account-1", "app-1")
        self.calls.append("delete")
        self.value = None


@dataclass
class IdentityProviderClient:
    value: CloudflareAccessIdentityProvider | None
    calls: list[str]

    def get_access_identity_provider(
        self, account_id: str, provider_id: str
    ) -> CloudflareAccessIdentityProvider | None:
        assert (account_id, provider_id) == ("account-1", "otp-1")
        self.calls.append("get")
        return self.value

    def delete_access_identity_provider(self, account_id: str, provider_id: str) -> None:
        assert (account_id, provider_id) == ("account-1", "otp-1")
        self.calls.append("delete")
        self.value = None


def test_access_application_deletion_requires_reread_bound_authority(tmp_path: Path) -> None:
    resource = OwnedResource("cloudflare_access_application", "app-1", "account:account-1:app")
    application = CloudflareAccessApplication(
        app_id="app-1",
        name="Wizard app",
        domain="app.example.test",
        app_type="self_hosted",
        allowed_identity_provider_ids=("otp-1",),
    )
    store = UninstallAuthorityStore(tmp_path)
    store.record_created(
        resource=resource,
        owner_id=_OWNER,
        provider="cloudflare_access_application",
        physical_target_id=application.app_id,
        parent_target_id="account-1",
        expected_fingerprint=application_fingerprint(application),
    )
    client = AccessApplicationClient(application, [])
    teardown = CloudflareAccessUninstallTeardown(
        RawEnvInput(1, {"CLOUDFLARE_API_TOKEN": "test"}),
        tmp_path,
        UninstallProviderClients(cloudflare_access_application=client),
    )

    teardown.delete_application(PlannedDeletion(resource, "cloudflare_access", "retain_safe"))

    assert client.calls == ["get", "delete", "get"]
    assert store.load_deletion(resource) is not None


def test_otp_identity_provider_deletion_requires_reread_bound_authority(tmp_path: Path) -> None:
    resource = OwnedResource("cloudflare_access_otp_provider", "otp-1", "account:account-1:otp")
    provider = CloudflareAccessIdentityProvider("otp-1", "Wizard OTP", "otp")
    store = UninstallAuthorityStore(tmp_path)
    store.record_created(
        resource=resource,
        owner_id=_OWNER,
        provider="cloudflare_access_identity_provider",
        physical_target_id=provider.provider_id,
        parent_target_id="account-1",
        expected_fingerprint=identity_provider_fingerprint(provider),
    )
    client = IdentityProviderClient(provider, [])
    teardown = CloudflareAccessUninstallTeardown(
        RawEnvInput(1, {"CLOUDFLARE_API_TOKEN": "test"}),
        tmp_path,
        UninstallProviderClients(cloudflare_access_identity_provider=client),
    )

    teardown.delete_identity_provider(PlannedDeletion(resource, "cloudflare_access", "retain_safe"))

    assert client.calls == ["get", "delete", "get"]
    assert store.load_deletion(resource) is not None
