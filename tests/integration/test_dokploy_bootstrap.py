# pyright: reportMissingImports=false

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dokploy_wizard import cli
from dokploy_wizard.cli import run_install_flow
from dokploy_wizard.dokploy import DokployBootstrapAuthError, DokployBootstrapAuthResult
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.packs.headscale import HeadscaleResourceRecord
from dokploy_wizard.state import (
    RAW_INPUT_FILE,
    STATE_DOCUMENT_FILES,
    RawEnvInput,
    StateValidationError,
    load_state_dir,
)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


@dataclass
class FakeDokployBackend:
    healthy_before_install: bool
    healthy_after_install: bool
    install_calls: int = 0

    def is_healthy(self) -> bool:
        if self.install_calls == 0:
            return self.healthy_before_install
        return self.healthy_after_install

    def install(self) -> None:
        self.install_calls += 1


@dataclass
class FakeCloudflareBackend:
    existing_tunnel: CloudflareTunnel | None = None
    dns_records: dict[str, CloudflareDnsRecord] | None = None
    create_tunnel_calls: int = 0
    create_dns_calls: int = 0
    access_provider: CloudflareAccessIdentityProvider | None = None
    access_apps: dict[str, CloudflareAccessApplication] = field(default_factory=dict)
    access_policies: dict[str, CloudflareAccessPolicy] = field(default_factory=dict)

    def validate_account_access(self, account_id: str) -> None:
        return None

    def validate_zone_access(self, zone_id: str) -> None:
        return None

    def get_tunnel(self, account_id: str, tunnel_id: str) -> CloudflareTunnel | None:
        if self.existing_tunnel is not None and self.existing_tunnel.tunnel_id == tunnel_id:
            return self.existing_tunnel
        return None

    def find_tunnel_by_name(self, account_id: str, tunnel_name: str) -> CloudflareTunnel | None:
        if self.existing_tunnel is not None and self.existing_tunnel.name == tunnel_name:
            return self.existing_tunnel
        return None

    def create_tunnel(self, account_id: str, tunnel_name: str) -> CloudflareTunnel:
        self.create_tunnel_calls += 1
        self.existing_tunnel = CloudflareTunnel(tunnel_id="bootstrap-tunnel", name=tunnel_name)
        return self.existing_tunnel

    def list_dns_records(
        self,
        zone_id: str,
        *,
        hostname: str,
        record_type: str,
        content: str | None,
    ) -> tuple[CloudflareDnsRecord, ...]:
        if self.dns_records is None:
            self.dns_records = {}
        record = self.dns_records.get(hostname)
        if record is None:
            return ()
        if content is not None and record.content != content:
            return ()
        return (record,)

    def create_dns_record(
        self,
        zone_id: str,
        *,
        hostname: str,
        content: str,
        proxied: bool,
    ) -> CloudflareDnsRecord:
        self.create_dns_calls += 1
        if self.dns_records is None:
            self.dns_records = {}
        record = CloudflareDnsRecord(
            record_id=f"dns-{hostname}",
            name=hostname,
            record_type="CNAME",
            content=content,
            proxied=proxied,
        )
        self.dns_records[hostname] = record
        return record

    def get_access_identity_provider(
        self, account_id: str, provider_id: str
    ) -> CloudflareAccessIdentityProvider | None:
        del account_id
        if self.access_provider is not None and self.access_provider.provider_id == provider_id:
            return self.access_provider
        return None

    def find_access_identity_provider_by_name(
        self, account_id: str, name: str
    ) -> CloudflareAccessIdentityProvider | None:
        del account_id
        if self.access_provider is not None and self.access_provider.name == name:
            return self.access_provider
        return None

    def create_access_identity_provider(
        self, account_id: str, name: str
    ) -> CloudflareAccessIdentityProvider:
        del account_id
        self.access_provider = CloudflareAccessIdentityProvider(
            provider_id="otp-provider-1",
            name=name,
            provider_type="onetimepin",
        )
        return self.access_provider

    def get_access_application(
        self, account_id: str, app_id: str
    ) -> CloudflareAccessApplication | None:
        del account_id
        return next((item for item in self.access_apps.values() if item.app_id == app_id), None)

    def find_access_application_by_domain(
        self, account_id: str, domain: str
    ) -> CloudflareAccessApplication | None:
        del account_id
        return self.access_apps.get(domain)

    def create_access_application(
        self,
        account_id: str,
        *,
        name: str,
        domain: str,
        allowed_identity_provider_ids: tuple[str, ...],
    ) -> CloudflareAccessApplication:
        del account_id
        app = CloudflareAccessApplication(
            app_id=f"app-{domain}",
            name=name,
            domain=domain,
            app_type="self_hosted",
            allowed_identity_provider_ids=allowed_identity_provider_ids,
        )
        self.access_apps[domain] = app
        return app

    def get_access_policy(
        self, account_id: str, app_id: str, policy_id: str
    ) -> CloudflareAccessPolicy | None:
        del account_id, policy_id
        return self.access_policies.get(app_id)

    def find_access_policy_by_name(
        self, account_id: str, app_id: str, name: str
    ) -> CloudflareAccessPolicy | None:
        del account_id
        policy = self.access_policies.get(app_id)
        if policy is not None and policy.name == name:
            return policy
        return None

    def create_access_policy(
        self,
        account_id: str,
        *,
        app_id: str,
        name: str,
        emails: tuple[str, ...],
    ) -> CloudflareAccessPolicy:
        del account_id
        policy = CloudflareAccessPolicy(
            policy_id=f"policy-{app_id}",
            app_id=app_id,
            name=name,
            decision="allow",
            emails=emails,
        )
        self.access_policies[app_id] = policy
        return policy


@dataclass
class FakeHeadscaleBackend:
    existing_service: HeadscaleResourceRecord | None = None

    def get_service(self, resource_id: str) -> HeadscaleResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> HeadscaleResourceRecord | None:
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
        secret_refs: tuple[str, ...],
    ) -> HeadscaleResourceRecord:
        del hostname, secret_refs
        self.existing_service = HeadscaleResourceRecord(
            resource_id="headscale-service-1",
            resource_name=resource_name,
        )
        return self.existing_service

    def check_health(self, *, service: HeadscaleResourceRecord, url: str) -> bool:
        del service, url
        return True


def _auth_required_raw_env() -> RawEnvInput:
    return RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "guided-stack",
            "ROOT_DOMAIN": "example.com",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
            "DOKPLOY_ADMIN_PASSWORD": "secret-123",
            "ENABLE_HEADSCALE": "true",
        },
    )


def test_install_dry_run_produces_plan_without_writing_state(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    backend = FakeDokployBackend(healthy_before_install=False, healthy_after_install=False)
    networking_backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="bootstrap-tunnel", name="wizard-stack-tunnel")
    )

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "cloudflare-valid.env",
        state_dir=state_dir,
        dry_run=True,
        bootstrap_backend=backend,
        networking_backend=networking_backend,
    )

    assert summary["bootstrap"]["outcome"] == "plan_only"
    assert summary["networking"]["outcome"] == "plan_only"
    assert summary["state_status"] == "fresh"
    assert not state_dir.exists()
    assert backend.install_calls == 0


def test_install_non_dry_run_persists_scaffold_and_marks_bootstrap_steps(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    backend = FakeDokployBackend(healthy_before_install=False, healthy_after_install=True)
    networking_backend = FakeCloudflareBackend()

    summary = run_install_flow(
        env_file=FIXTURES_DIR / "cloudflare-valid.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=backend,
        networking_backend=networking_backend,
        headscale_backend=FakeHeadscaleBackend(),
    )

    loaded_state = load_state_dir(state_dir)

    assert summary["bootstrap"]["outcome"] == "applied"
    assert backend.install_calls == 1
    assert loaded_state.raw_input is not None
    assert loaded_state.desired_state is not None
    assert loaded_state.applied_state is not None
    assert loaded_state.ownership_ledger is not None
    assert loaded_state.applied_state.completed_steps == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "headscale",
    )
    assert len(loaded_state.ownership_ledger.resources) == 4
    assert summary["networking"]["outcome"] == "applied"
    assert summary["shared_core"]["outcome"] == "not_required"
    assert summary["headscale"]["outcome"] == "applied"


def test_install_reuses_existing_matching_state_and_detects_already_present(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first_backend = FakeDokployBackend(healthy_before_install=False, healthy_after_install=True)
    second_backend = FakeDokployBackend(healthy_before_install=True, healthy_after_install=True)
    first_networking_backend = FakeCloudflareBackend()

    headscale_backend = FakeHeadscaleBackend()
    run_install_flow(
        env_file=FIXTURES_DIR / "cloudflare-valid.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=first_backend,
        networking_backend=first_networking_backend,
        headscale_backend=headscale_backend,
    )
    second_networking_backend = FakeCloudflareBackend(
        existing_tunnel=CloudflareTunnel(tunnel_id="bootstrap-tunnel", name="wizard-stack-tunnel"),
        dns_records={
            "dokploy.example.com": CloudflareDnsRecord(
                record_id="dns-dokploy.example.com",
                name="dokploy.example.com",
                record_type="CNAME",
                content="bootstrap-tunnel.cfargotunnel.com",
                proxied=True,
            ),
            "headscale.example.com": CloudflareDnsRecord(
                record_id="dns-headscale.example.com",
                name="headscale.example.com",
                record_type="CNAME",
                content="bootstrap-tunnel.cfargotunnel.com",
                proxied=True,
            ),
        },
    )
    summary = run_install_flow(
        env_file=FIXTURES_DIR / "cloudflare-valid.env",
        state_dir=state_dir,
        dry_run=False,
        bootstrap_backend=second_backend,
        networking_backend=second_networking_backend,
        headscale_backend=headscale_backend,
    )

    assert summary["state_status"] == "existing"
    assert summary["bootstrap"]["outcome"] == "already_present"
    assert summary["networking"]["outcome"] == "already_present"
    assert summary["headscale"]["outcome"] == "already_present"
    assert second_backend.install_calls == 0


def test_install_auth_failure_leaves_fresh_scaffold_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "install.env"
    raw_env = _auth_required_raw_env()

    class FakeAuthClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:3000"

        def ensure_api_key(
            self, *, admin_email: str, admin_password: str, key_name: str = "dokploy-wizard"
        ) -> DokployBootstrapAuthResult:
            del admin_email, admin_password, key_name
            raise DokployBootstrapAuthError("no working auth endpoint")

    monkeypatch.setattr(cli, "collect_host_facts", lambda _: object())
    monkeypatch.setattr(
        cli,
        "_prepare_install_host_prerequisites",
        lambda **kwargs: (kwargs["host_facts"], {}),
    )
    monkeypatch.setattr(cli, "run_preflight", lambda *_: object())
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(
        cli,
        "execute_lifecycle_plan",
        lambda **_: (_ for _ in ()).throw(AssertionError("execute_lifecycle_plan should not run")),
    )
    monkeypatch.setattr(cli, "DokployBootstrapAuthClient", FakeAuthClient)

    with pytest.raises(DokployBootstrapAuthError, match="no working auth endpoint"):
        run_install_flow(
            env_file=env_file,
            state_dir=state_dir,
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=FakeDokployBackend(True, True),
        )

    loaded_state = load_state_dir(state_dir)

    assert state_dir.exists()
    assert sorted(path.name for path in state_dir.iterdir()) == sorted(STATE_DOCUMENT_FILES)
    assert loaded_state.raw_input is not None
    assert loaded_state.desired_state is not None
    assert loaded_state.applied_state is not None
    assert loaded_state.ownership_ledger is not None
    assert loaded_state.applied_state.completed_steps == ()
    assert "DOKPLOY_API_KEY" not in loaded_state.raw_input.values


def test_install_auth_success_refreshes_persisted_target_state_before_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "install.env"
    raw_env = _auth_required_raw_env()

    class FakeAuthClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:3000"

        def ensure_api_key(
            self, *, admin_email: str, admin_password: str, key_name: str = "dokploy-wizard"
        ) -> DokployBootstrapAuthResult:
            assert admin_email == "admin@example.com"
            assert admin_password == "secret-123"
            assert key_name == "dokploy-wizard"
            return DokployBootstrapAuthResult(
                api_key="dokp-key-123",
                api_url="http://127.0.0.1:3000",
                admin_email=admin_email,
                organization_id="org-1",
                used_sign_up=False,
                auth_path="/api/auth/sign-in/email",
                session_path="/api/user.session",
            )

    monkeypatch.setattr(cli, "collect_host_facts", lambda _: object())
    monkeypatch.setattr(
        cli,
        "_prepare_install_host_prerequisites",
        lambda **kwargs: (kwargs["host_facts"], {}),
    )
    monkeypatch.setattr(cli, "run_preflight", lambda *_: object())
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **_: {"state_status": "fresh"})
    monkeypatch.setattr(cli, "DokployBootstrapAuthClient", FakeAuthClient)
    monkeypatch.setattr(
        cli,
        "DokployApiClient",
        lambda *, api_url, api_key: type(
            "_ValidDokployClient",
            (),
            {
                "__init__": lambda self: None,
                "list_projects": lambda self: (),
            },
        )(),
    )

    summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
    )

    loaded_state = load_state_dir(state_dir)
    persisted_raw_input = json.loads((state_dir / RAW_INPUT_FILE).read_text(encoding="utf-8"))

    assert summary["state_status"] == "fresh"
    assert loaded_state.raw_input is not None
    assert loaded_state.desired_state is not None
    assert loaded_state.applied_state is not None
    assert loaded_state.raw_input.values["DOKPLOY_API_KEY"] == "dokp-key-123"
    assert loaded_state.raw_input.values["DOKPLOY_API_URL"] == "http://127.0.0.1:3000"
    assert loaded_state.desired_state.dokploy_api_url == "http://127.0.0.1:3000"
    assert loaded_state.applied_state.completed_steps == ()
    assert persisted_raw_input["values"]["DOKPLOY_API_KEY"] == "dokp-key-123"
    assert "DOKPLOY_API_KEY=dokp-key-123" in env_file.read_text(encoding="utf-8")


def test_install_rejects_partial_existing_state(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "raw-input.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "values": {
                    "STACK_NAME": "core-low-stack",
                    "ROOT_DOMAIN": "example.com",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        StateValidationError,
        match="expected raw input, desired state, applied state, and ownership ledger",
    ):
        run_install_flow(
            env_file=FIXTURES_DIR / "cloudflare-valid.env",
            state_dir=state_dir,
            dry_run=False,
            bootstrap_backend=FakeDokployBackend(False, True),
            networking_backend=FakeCloudflareBackend(),
        )
