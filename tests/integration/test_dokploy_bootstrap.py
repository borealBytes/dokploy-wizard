# pyright: reportMissingImports=false

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from dokploy_wizard import cli
from dokploy_wizard.cli import run_install_flow
from dokploy_wizard.dokploy import (
    DokployBootstrapAuthError,
    DokployBootstrapAuthResult,
    DokployCoderBackend,
    DokployDocuSealBackend,
    DokployMoodleBackend,
    DokployNextcloudBackend,
    DokployOpenClawBackend,
    DokploySeaweedFsBackend,
    DokploySharedCoreBackend,
)
from dokploy_wizard.dokploy import coder as coder_module
from dokploy_wizard.dokploy import openclaw as openclaw_module
from dokploy_wizard.dokploy import seaweedfs as seaweedfs_module
from dokploy_wizard.dokploy import shared_core as shared_core_module
from dokploy_wizard.networking import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareCertificatePack,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.packs.docuseal import DocuSealBootstrapState
from dokploy_wizard.packs.headscale import HeadscaleResourceRecord
from dokploy_wizard.state import (
    RAW_INPUT_FILE,
    STATE_DOCUMENT_FILES,
    AppliedStateCheckpoint,
    ComposeArtifactHashState,
    RawEnvInput,
    StateValidationError,
    ensure_litellm_generated_keys,
    load_state_dir,
    resolve_desired_state,
    write_applied_checkpoint,
)
from dokploy_wizard.verification import ServiceVerificationResult
from tests.integration.test_nextcloud_pack import NextcloudOccRecorder, RecordingNextcloudApi
from tests.unit.fake_dokploy import FakeDokployApiClient
from tests.unit.test_docuseal_pack import FakeDokployDocuSealApiClient
from tests.unit.test_litellm_shared_core import _FakeLiteLLMAdminApi
from tests.unit.test_moodle_pack import FakeDokployMoodleApiClient

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


@dataclass
class FakeDokployBackend:
    healthy_before_install: bool
    healthy_after_install: bool
    install_calls: int = 0
    ensure_public_route_calls: int = 0

    def is_healthy(self) -> bool:
        if self.install_calls == 0:
            return self.healthy_before_install
        return self.healthy_after_install

    def install(self) -> None:
        self.install_calls += 1

    def ensure_public_route(self) -> None:
        self.ensure_public_route_calls += 1


@dataclass
class FakeCloudflareBackend:
    existing_tunnel: CloudflareTunnel | None = None
    dns_records: dict[str, CloudflareDnsRecord] | None = None
    certificate_packs: dict[str, CloudflareCertificatePack] = field(default_factory=dict)
    ordered_certificate_hosts: list[tuple[str, ...]] = field(default_factory=list)
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

    def get_tunnel_token(self, account_id: str, tunnel_id: str) -> str:
        return f"token-{tunnel_id}"

    def update_tunnel_configuration(
        self, account_id: str, tunnel_id: str, ingress: tuple[dict[str, object], ...]
    ) -> None:
        del account_id, tunnel_id, ingress

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

    def list_certificate_packs(self, zone_id: str) -> tuple[CloudflareCertificatePack, ...]:
        del zone_id
        return tuple(self.certificate_packs.values())

    def order_advanced_certificate_pack(
        self, zone_id: str, *, hosts: tuple[str, ...]
    ) -> CloudflareCertificatePack:
        del zone_id
        self.ordered_certificate_hosts.append(hosts)
        pack = CloudflareCertificatePack(
            pack_id=f"cert-{hosts[-1]}",
            pack_type="advanced",
            status="active",
            hosts=hosts,
        )
        self.certificate_packs[hosts[-1]] = pack
        return pack

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


def _auth_email_only_raw_env() -> RawEnvInput:
    return RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "openmerge",
            "ROOT_DOMAIN": "openmerge.me",
            "DOKPLOY_SUBDOMAIN": "dokploy",
            "DOKPLOY_ADMIN_EMAIL": "clayton@superiorbyteworks.com",
            "ENABLE_HEADSCALE": "true",
            "PACKS": "nextcloud,openclaw,seaweedfs,coder",
            "OPENCLAW_CHANNELS": "telegram",
            "OPENCLAW_PRIMARY_MODEL": "nvidia/moonshotai/kimi-k2.5",
            "OPENCLAW_FALLBACK_MODELS": (
                "openrouter/nvidia/nemotron-3-super-120b-a12b:free,"
                "openrouter/google/gemma-4-31b-it:free"
            ),
            "OPENCLAW_NVIDIA_API_KEY": "nvapi-test-key",
            "OPENCLAW_OPENROUTER_API_KEY": "sk-or-test-key",
            "SEAWEEDFS_ACCESS_KEY": "seaweed-access",
            "SEAWEEDFS_SECRET_KEY": "seaweed-secret",
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
    assert backend.ensure_public_route_calls == 1
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
    assert len(loaded_state.ownership_ledger.resources) == 7
    assert summary["networking"]["outcome"] == "applied"
    assert summary["shared_core"]["outcome"] == "applied"
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
    assert second_backend.ensure_public_route_calls == 2


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
    monkeypatch.setattr(cli, "_qualify_dokploy_mutation_auth", lambda **_: None)
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
    assert sorted(path.name for path in state_dir.iterdir()) == sorted(
        [*STATE_DOCUMENT_FILES, "litellm-generated-keys.json"]
    )
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
            assert key_name.startswith("dokploy-wizard")
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
    monkeypatch.setattr(cli, "_qualify_dokploy_mutation_auth", lambda **_: None)
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)
    monkeypatch.setattr(cli, "execute_lifecycle_plan", lambda **_: {"state_status": "fresh"})
    monkeypatch.setattr(cli, "DokployBootstrapAuthClient", FakeAuthClient)
    monkeypatch.setattr(
        cli,
        "DokployApiClient",
        lambda *, api_url, api_key, **kwargs: type(
            "_ValidDokployClient",
            (),
            {
                "__init__": lambda self: None,
                "list_projects": lambda self: (),
                "ai_providers_all": lambda self: (),
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


def test_install_auth_rejects_missing_admin_password_for_env_file_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "install.env"
    raw_env = _auth_email_only_raw_env()

    monkeypatch.setattr(cli, "collect_host_facts", lambda _: object())
    monkeypatch.setattr(
        cli,
        "_prepare_install_host_prerequisites",
        lambda **kwargs: (kwargs["host_facts"], {}),
    )
    monkeypatch.setattr(cli, "run_preflight", lambda *_: object())
    monkeypatch.setattr(cli, "validate_preserved_phases", lambda **_: None)

    with pytest.raises(StateValidationError, match="DOKPLOY_ADMIN_PASSWORD"):
        run_install_flow(
            env_file=env_file,
            state_dir=state_dir,
            dry_run=False,
            raw_env=raw_env,
            bootstrap_backend=FakeDokployBackend(True, True),
            networking_backend=FakeCloudflareBackend(),
        )


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


@dataclass
class _FullStackClients:
    shared_core: FakeDokployApiClient
    nextcloud: RecordingNextcloudApi
    moodle: FakeDokployMoodleApiClient
    docuseal: FakeDokployDocuSealApiClient
    seaweedfs: FakeDokployApiClient
    coder: FakeDokployApiClient
    openclaw: FakeDokployApiClient


@dataclass
class _FullStackBackends:
    shared_core: DokploySharedCoreBackend
    nextcloud: DokployNextcloudBackend
    moodle: DokployMoodleBackend
    docuseal: DokployDocuSealBackend
    seaweedfs: DokploySeaweedFsBackend
    coder: DokployCoderBackend
    openclaw: DokployOpenClawBackend


def test_full_stack_second_deploy_proof_rerun_skips_targeted_service_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "full-stack.env"
    raw_env = _full_stack_raw_env()
    networking_backend = FakeCloudflareBackend()

    clients, first_backends, service_names = _build_full_stack_backends(
        raw_env=raw_env,
        state_dir=state_dir,
        monkeypatch=monkeypatch,
    )
    first_summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=first_backends.shared_core,
        nextcloud_backend=first_backends.nextcloud,
        seaweedfs_backend=first_backends.seaweedfs,
        coder_backend=first_backends.coder,
        openclaw_backend=first_backends.openclaw,
    )

    assert first_summary["shared_core"]["outcome"] == "applied"
    assert first_summary["nextcloud"]["outcome"] == "applied"
    assert first_summary["moodle"]["outcome"] == "applied"
    assert first_summary["docuseal"]["outcome"] == "applied"
    assert first_summary["seaweedfs"]["outcome"] == "applied"
    assert first_summary["coder"]["outcome"] == "applied"
    assert first_summary["openclaw"]["outcome"] == "applied"
    assert _mutation_counts(clients, service_names) == {
        "shared_core": (1, 1, 1),
        "nextcloud": (1, 2, 1),
        "moodle": (1, 1, 1),
        "docuseal": (1, 1, 1),
        "seaweedfs": (1, 1, 1),
        "coder": (1, 1, 1),
        "openclaw": (1, 1, 1),
    }

    _persist_missing_compose_hashes(
        state_dir=state_dir,
        clients=clients,
        service_names=service_names,
    )
    loaded_state = load_state_dir(state_dir)
    assert loaded_state.applied_state is not None
    assert set(loaded_state.applied_state.compose_artifact_hashes) >= set(
        service_names.values()
    )
    _rewind_applied_steps(
        state_dir,
        completed_steps=("preflight", "dokploy_bootstrap", "networking"),
    )

    second_clients_before = _mutation_counts(clients, service_names)
    _, second_backends, _ = _build_full_stack_backends(
        raw_env=raw_env,
        state_dir=state_dir,
        monkeypatch=monkeypatch,
        clients=clients,
    )
    second_summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=second_backends.shared_core,
        nextcloud_backend=second_backends.nextcloud,
        seaweedfs_backend=second_backends.seaweedfs,
        coder_backend=second_backends.coder,
        openclaw_backend=second_backends.openclaw,
    )
    second_clients_after = _mutation_counts(clients, service_names)

    assert {
        "shared_core",
        "nextcloud",
        "moodle",
        "docuseal",
        "seaweedfs",
        "coder",
        "openclaw",
    }.issubset(set(second_summary["lifecycle"]["phases_to_run"]))
    assert second_summary["shared_core"]["outcome"] == "already_present"
    assert second_summary["nextcloud"]["outcome"] == "already_present"
    assert second_summary["moodle"]["outcome"] == "already_present"
    assert second_summary["docuseal"]["outcome"] == "already_present"
    assert second_summary["seaweedfs"]["outcome"] == "already_present"
    assert second_summary["coder"]["outcome"] == "already_present"
    assert _mutation_deltas(second_clients_before, second_clients_after) == {
        "shared_core": (0, 0, 0),
        "nextcloud": (0, 0, 0),
        "moodle": (0, 0, 0),
        "docuseal": (0, 0, 0),
        "seaweedfs": (0, 0, 0),
        "coder": (0, 0, 0),
        "openclaw": (0, 0, 0),
    }


def test_full_stack_deploy_rerun_only_redeploys_service_with_changed_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "full-stack-modify.env"
    initial_raw_env = _full_stack_raw_env()
    modified_raw_env = _full_stack_raw_env(OPENCLAW_SUBDOMAIN="advisor")
    networking_backend = FakeCloudflareBackend()

    clients, first_backends, service_names = _build_full_stack_backends(
        raw_env=initial_raw_env,
        state_dir=state_dir,
        monkeypatch=monkeypatch,
    )
    run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=initial_raw_env,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=first_backends.shared_core,
        nextcloud_backend=first_backends.nextcloud,
        seaweedfs_backend=first_backends.seaweedfs,
        coder_backend=first_backends.coder,
        openclaw_backend=first_backends.openclaw,
    )

    _persist_missing_compose_hashes(
        state_dir=state_dir,
        clients=clients,
        service_names=service_names,
    )
    mutation_counts_before = _mutation_counts(clients, service_names)
    _, modify_backends, _ = _build_full_stack_backends(
        raw_env=modified_raw_env,
        state_dir=state_dir,
        monkeypatch=monkeypatch,
        clients=clients,
    )
    monkeypatch.setattr(
        "dokploy_wizard.dokploy.shared_core._can_connect_as_allocation",
        lambda container_name, allocation: True,
    )
    modify_summary = cli.run_modify_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=modified_raw_env,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=modify_backends.shared_core,
        nextcloud_backend=modify_backends.nextcloud,
        seaweedfs_backend=modify_backends.seaweedfs,
        coder_backend=modify_backends.coder,
        openclaw_backend=modify_backends.openclaw,
    )
    mutation_counts_after = _mutation_counts(clients, service_names)

    assert "openclaw" in modify_summary["lifecycle"]["phases_to_run"]
    assert modify_summary["shared_core"]["outcome"] == "already_present"
    assert modify_summary["nextcloud"]["outcome"] == "already_present"
    assert modify_summary["moodle"]["outcome"] == "already_present"
    assert modify_summary["docuseal"]["outcome"] == "already_present"
    assert modify_summary["seaweedfs"]["outcome"] == "already_present"
    assert modify_summary["coder"]["outcome"] == "already_present"
    assert modify_summary["openclaw"]["outcome"] == "applied"
    assert _mutation_deltas(mutation_counts_before, mutation_counts_after) == {
        "shared_core": (0, 0, 0),
        "nextcloud": (0, 0, 0),
        "moodle": (0, 0, 0),
        "docuseal": (0, 0, 0),
        "seaweedfs": (0, 0, 0),
        "coder": (0, 0, 0),
        "openclaw": (0, 1, 1),
    }


def test_full_stack_deploy_rerun_redeploys_unhealthy_service_instead_of_skipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    env_file = tmp_path / "full-stack-unhealthy.env"
    raw_env = _full_stack_raw_env()
    networking_backend = FakeCloudflareBackend()

    clients, first_backends, service_names = _build_full_stack_backends(
        raw_env=raw_env,
        state_dir=state_dir,
        monkeypatch=monkeypatch,
    )
    run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=first_backends.shared_core,
        nextcloud_backend=first_backends.nextcloud,
        seaweedfs_backend=first_backends.seaweedfs,
        coder_backend=first_backends.coder,
        openclaw_backend=first_backends.openclaw,
    )

    _persist_missing_compose_hashes(
        state_dir=state_dir,
        clients=clients,
        service_names=service_names,
    )
    _rewind_applied_steps(
        state_dir,
        completed_steps=("preflight", "dokploy_bootstrap", "networking"),
    )
    mutation_counts_before = _mutation_counts(clients, service_names)
    _, unhealthy_backends, _ = _build_full_stack_backends(
        raw_env=raw_env,
        state_dir=state_dir,
        monkeypatch=monkeypatch,
        clients=clients,
        unhealthy_services={"openclaw"},
    )
    rerun_summary = run_install_flow(
        env_file=env_file,
        state_dir=state_dir,
        dry_run=False,
        raw_env=raw_env,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=networking_backend,
        shared_core_backend=unhealthy_backends.shared_core,
        nextcloud_backend=unhealthy_backends.nextcloud,
        seaweedfs_backend=unhealthy_backends.seaweedfs,
        coder_backend=unhealthy_backends.coder,
        openclaw_backend=unhealthy_backends.openclaw,
    )
    mutation_counts_after = _mutation_counts(clients, service_names)

    assert rerun_summary["openclaw"]["outcome"] == "applied"
    assert _mutation_deltas(mutation_counts_before, mutation_counts_after) == {
        "shared_core": (0, 0, 0),
        "nextcloud": (0, 0, 0),
        "moodle": (0, 0, 0),
        "docuseal": (0, 0, 0),
        "seaweedfs": (0, 0, 0),
        "coder": (0, 0, 0),
        "openclaw": (0, 1, 1),
    }


def _full_stack_raw_env(**overrides: str) -> RawEnvInput:
    values = {
        "STACK_NAME": "wizard-stack",
        "ROOT_DOMAIN": "example.com",
        "HOST_OS_ID": "ubuntu",
        "HOST_OS_VERSION_ID": "24.04",
        "HOST_CPU_COUNT": "4",
        "HOST_MEMORY_GB": "8",
        "HOST_DISK_GB": "100",
        "HOST_DOCKER_INSTALLED": "true",
        "HOST_DOCKER_DAEMON_REACHABLE": "true",
        "HOST_PORT_80_IN_USE": "false",
        "HOST_PORT_443_IN_USE": "false",
        "HOST_PORT_3000_IN_USE": "false",
        "HOST_ENVIRONMENT": "local",
        "DOKPLOY_BOOTSTRAP_HEALTHY": "true",
        "DOKPLOY_API_URL": "https://dokploy.example.com/api",
        "DOKPLOY_API_KEY": "api-key-123",
        "DOKPLOY_ADMIN_EMAIL": "admin@example.com",
        "DOKPLOY_ADMIN_PASSWORD": "secret-123",
        "CLOUDFLARE_API_TOKEN": "token-123",
        "CLOUDFLARE_ACCOUNT_ID": "account-123",
        "CLOUDFLARE_ZONE_ID": "zone-123",
        "CLOUDFLARE_TUNNEL_NAME": "wizard-stack-tunnel",
        "CLOUDFLARE_ACCESS_OTP_EMAILS": "admin@example.com",
        "ENABLE_NEXTCLOUD": "true",
        "ENABLE_MOODLE": "true",
        "ENABLE_DOCUSEAL": "true",
        "ENABLE_CODER": "true",
        "ENABLE_OPENCLAW": "true",
        "ENABLE_SEAWEEDFS": "true",
        "OPENCLAW_CHANNELS": "telegram",
        "AI_DEFAULT_API_KEY": "shared-ai-key",
        "AI_DEFAULT_BASE_URL": "https://models.example.com/v1",
        "SEAWEEDFS_ACCESS_KEY": "seaweed-access",
        "SEAWEEDFS_SECRET_KEY": "seaweed-secret",
    }
    values.update(overrides)
    return RawEnvInput(format_version=1, values=values)


def _build_full_stack_backends(
    *,
    raw_env: RawEnvInput,
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    clients: _FullStackClients | None = None,
    unhealthy_services: set[str] | None = None,
) -> tuple[_FullStackClients, _FullStackBackends, dict[str, str]]:
    desired_state = resolve_desired_state(raw_env)
    generated_keys = ensure_litellm_generated_keys(state_dir)
    unhealthy = unhealthy_services or set()
    service_names = {
        "shared_core": desired_state.shared_core.network_name,
        "nextcloud": f"{desired_state.stack_name}-nextcloud",
        "moodle": f"{desired_state.stack_name}-moodle",
        "docuseal": f"{desired_state.stack_name}-docuseal",
        "seaweedfs": f"{desired_state.stack_name}-seaweedfs",
        "coder": f"{desired_state.stack_name}-coder",
        "openclaw": f"{desired_state.stack_name}-openclaw",
    }
    nextcloud_allocation = _shared_allocation(desired_state=desired_state, pack_name="nextcloud")
    moodle_allocation = _shared_allocation(desired_state=desired_state, pack_name="moodle")
    docuseal_allocation = _shared_allocation(desired_state=desired_state, pack_name="docuseal")
    coder_allocation = _shared_allocation(desired_state=desired_state, pack_name="coder")
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.redis is not None
    assert nextcloud_allocation.postgres is not None
    assert nextcloud_allocation.redis is not None
    assert moodle_allocation.postgres is not None
    assert docuseal_allocation.postgres is not None
    assert coder_allocation.postgres is not None
    assert desired_state.seaweedfs_access_key is not None
    assert desired_state.seaweedfs_secret_key is not None

    if clients is None:
        clients = _FullStackClients(
            shared_core=FakeDokployApiClient(),
            nextcloud=RecordingNextcloudApi(),
            moodle=FakeDokployMoodleApiClient(),
            docuseal=FakeDokployDocuSealApiClient(),
            seaweedfs=FakeDokployApiClient(),
            coder=FakeDokployApiClient(),
            openclaw=FakeDokployApiClient(),
        )

    occ = NextcloudOccRecorder()
    occ.patch(monkeypatch)
    monkeypatch.setattr(coder_module, "_local_https_health_check", lambda url: True)
    monkeypatch.setattr(coder_module, "_public_https_health_check", lambda url: True)
    monkeypatch.setattr(
        coder_module,
        "_wait_for_public_https_health",
        lambda url: True,
    )
    monkeypatch.setattr(
        coder_module,
        "_coder_container_name",
        lambda service_name: f"{desired_state.stack_name}-coder-container",
    )
    monkeypatch.setattr(coder_module, "_wait_for_coder_bootstrap_api_ready", lambda hostname: None)
    monkeypatch.setattr(coder_module, "_coder_first_user_exists", lambda hostname: True)
    monkeypatch.setattr(coder_module, "_coder_login", lambda **kwargs: "session-123")
    monkeypatch.setattr(
        coder_module,
        "_list_templates",
        lambda **kwargs: tuple(
            {"name": template_name} for template_name in coder_module._required_template_names()
        ),
    )
    monkeypatch.setattr(
        coder_module,
        "_list_workspaces",
        lambda **kwargs: (coder_module._default_workspace_name(desired_state.hostnames["coder"]),),
    )
    monkeypatch.setattr(openclaw_module, "_docker_container_is_up", lambda service_name: True)
    monkeypatch.setattr(
        openclaw_module,
        "_wait_for_container_http_health",
        lambda service_name, url, *, app_port: True,
    )
    monkeypatch.setattr(openclaw_module, "_wait_for_local_https_health", lambda url: True)
    monkeypatch.setattr(
        openclaw_module,
        "_control_ui_origin_ready",
        lambda service_name, url: True,
    )
    monkeypatch.setattr(
        shared_core_module,
        "_find_container_name",
        lambda resource_name: f"{resource_name}-container",
    )
    monkeypatch.setattr(
        seaweedfs_module,
        "_docker_container_is_up",
        lambda service_name: (
            service_name != service_names["seaweedfs"] or "seaweedfs" not in unhealthy
        ),
    )

    shared_core_backend = DokploySharedCoreBackend(
        api_url="https://dokploy.example.com/api",
        api_key="api-key-123",
        stack_name=desired_state.stack_name,
        plan=desired_state.shared_core,
        litellm_env=dict(raw_env.values),
        allocation_provisioner=lambda allocations: None,
        litellm_generated_keys=generated_keys,
        litellm_admin_api=_FakeLiteLLMAdminApi({"status": "connected", "db": "connected"}),
        state_dir=state_dir,
        client=clients.shared_core,
    )
    monkeypatch.setattr(
        shared_core_backend,
        "_shared_core_runtime_ready_for_noop",
        lambda: "shared_core" not in unhealthy,
    )
    monkeypatch.setattr(shared_core_backend, "_wait_for_shared_core_containers", lambda: None)

    nextcloud_backend = DokployNextcloudBackend(
        api_url="https://dokploy.example.com/api",
        api_key="api-key-123",
        stack_name=desired_state.stack_name,
        nextcloud_hostname=desired_state.hostnames["nextcloud"],
        onlyoffice_hostname=desired_state.hostnames["onlyoffice"],
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        redis_service_name=desired_state.shared_core.redis.service_name,
        postgres=nextcloud_allocation.postgres,
        redis=nextcloud_allocation.redis,
        integration_secret_ref=f"{desired_state.stack_name}-nextcloud-onlyoffice-jwt-secret",
        admin_user=raw_env.values["DOKPLOY_ADMIN_EMAIL"],
        admin_password=raw_env.values["DOKPLOY_ADMIN_PASSWORD"],
        openclaw_volume_name=f"{desired_state.stack_name}-openclaw-data",
        state_dir=state_dir,
        client=clients.nextcloud,
    )
    monkeypatch.setattr(nextcloud_backend, "check_health", lambda *, service, url: True)

    moodle_backend = DokployMoodleBackend(
        api_url="https://dokploy.example.com/api",
        api_key="api-key-123",
        stack_name=desired_state.stack_name,
        hostname=desired_state.hostnames["moodle"],
        admin_email=raw_env.values["DOKPLOY_ADMIN_EMAIL"],
        admin_password=raw_env.values["DOKPLOY_ADMIN_PASSWORD"],
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        postgres=moodle_allocation.postgres,
        state_dir=state_dir,
        client=cast(Any, clients.moodle),
    )
    monkeypatch.setattr(
        moodle_backend,
        "ensure_application_ready",
        lambda: ("Moodle already initialized.",),
    )
    monkeypatch.setattr(moodle_backend, "check_health", lambda *, service, url: True)

    docuseal_backend = DokployDocuSealBackend(
        api_url="https://dokploy.example.com/api",
        api_key="api-key-123",
        stack_name=desired_state.stack_name,
        hostname=desired_state.hostnames["docuseal"],
        admin_email=raw_env.values["DOKPLOY_ADMIN_EMAIL"],
        admin_password=raw_env.values["DOKPLOY_ADMIN_PASSWORD"],
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        postgres=docuseal_allocation.postgres,
        state_dir=state_dir,
        client=cast(Any, clients.docuseal),
    )
    monkeypatch.setattr(
        docuseal_backend,
        "ensure_application_ready",
        lambda *, secret_key_base_secret_ref: (
            DocuSealBootstrapState(
                initialized=True,
                secret_key_base_secret_ref=secret_key_base_secret_ref,
            ),
            ("DocuSeal already initialized.",),
        ),
    )
    monkeypatch.setattr(
        docuseal_backend,
        "_verify_current_application",
        lambda: _verification_result(
            service_name=service_names["docuseal"],
            passed="docuseal" not in unhealthy,
            detail="DocuSeal runtime ready.",
        ),
    )
    monkeypatch.setattr(docuseal_backend, "check_health", lambda *, service, url: True)
    monkeypatch.setattr(cli, "_build_moodle_backend", lambda **kwargs: moodle_backend)
    monkeypatch.setattr(cli, "_build_docuseal_backend", lambda **kwargs: docuseal_backend)

    seaweedfs_backend = DokploySeaweedFsBackend(
        api_url="https://dokploy.example.com/api",
        api_key="api-key-123",
        state_dir=state_dir,
        stack_name=desired_state.stack_name,
        hostname=desired_state.hostnames["s3"],
        access_key=desired_state.seaweedfs_access_key,
        secret_key=desired_state.seaweedfs_secret_key,
        client=clients.seaweedfs,
    )
    monkeypatch.setattr(seaweedfs_backend, "check_health", lambda *, service, url: True)

    coder_backend = DokployCoderBackend(
        api_url="https://dokploy.example.com/api",
        api_key="api-key-123",
        stack_name=desired_state.stack_name,
        hostname=desired_state.hostnames["coder"],
        wildcard_hostname=desired_state.hostnames["coder-wildcard"],
        admin_email=raw_env.values["DOKPLOY_ADMIN_EMAIL"],
        admin_password=raw_env.values["DOKPLOY_ADMIN_PASSWORD"],
        postgres_service_name=desired_state.shared_core.postgres.service_name,
        postgres=coder_allocation.postgres,
        hermes_model=raw_env.values.get("HERMES_MODEL", "unsloth-active"),
        ai_default_base_url=raw_env.values["AI_DEFAULT_BASE_URL"],
        ai_default_api_key=generated_keys.virtual_keys["coder-hermes"],
        state_dir=state_dir,
        client=clients.coder,
    )
    monkeypatch.setattr(coder_backend, "ensure_application_ready", lambda: ("Coder ready.",))
    monkeypatch.setattr(
        coder_backend,
        "_verify_current_compose_application",
        lambda: _verification_result(
            service_name=service_names["coder"],
            passed="coder" not in unhealthy,
            detail="Coder bootstrap artifacts ready.",
            tier="bootstrap",
        ),
    )
    monkeypatch.setattr(coder_backend, "check_health", lambda *, service, url: True)

    openclaw_backend = DokployOpenClawBackend(
        api_url="https://dokploy.example.com/api",
        api_key="api-key-123",
        stack_name=desired_state.stack_name,
        openclaw_gateway_password="openclaw-ui-generated",
        openclaw_ai_default_api_key=raw_env.values["AI_DEFAULT_API_KEY"],
        openclaw_ai_default_base_url=raw_env.values["AI_DEFAULT_BASE_URL"],
        state_dir=state_dir,
        client=clients.openclaw,
    )
    monkeypatch.setattr(
        openclaw_backend,
        "_verify_service_runtime",
        lambda *, service_name, variant, url: _verification_result(
            service_name=service_name,
            passed=variant not in unhealthy,
            detail=f"{variant} runtime healthy at {url}",
        ),
    )
    monkeypatch.setattr(openclaw_backend, "check_health", lambda *, service, url: True)

    return clients, _FullStackBackends(
        shared_core=shared_core_backend,
        nextcloud=nextcloud_backend,
        moodle=moodle_backend,
        docuseal=docuseal_backend,
        seaweedfs=seaweedfs_backend,
        coder=coder_backend,
        openclaw=openclaw_backend,
    ), service_names


def _shared_allocation(*, desired_state: Any, pack_name: str) -> Any:
    allocation = next(
        item for item in desired_state.shared_core.allocations if item.pack_name == pack_name
    )
    assert allocation.postgres is not None
    if pack_name == "nextcloud":
        assert allocation.redis is not None
    return allocation


def _rewind_applied_steps(state_dir: Path, *, completed_steps: tuple[str, ...]) -> None:
    loaded_state = load_state_dir(state_dir)
    assert loaded_state.applied_state is not None
    applied_state = loaded_state.applied_state
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=applied_state.format_version,
            desired_state_fingerprint=applied_state.desired_state_fingerprint,
            completed_steps=completed_steps,
            compose_artifact_hashes=dict(applied_state.compose_artifact_hashes),
            lifecycle_checkpoint_contract_version=(
                applied_state.lifecycle_checkpoint_contract_version
            ),
        ),
    )


def _persist_missing_compose_hashes(
    *, state_dir: Path, clients: _FullStackClients, service_names: dict[str, str]
) -> None:
    loaded_state = load_state_dir(state_dir)
    assert loaded_state.applied_state is not None
    applied_state = loaded_state.applied_state
    compose_hashes = dict(applied_state.compose_artifact_hashes)
    rendered_compose_by_service = {
        service_names["shared_core"]: clients.shared_core.compose_files_by_name[
            service_names["shared_core"]
        ],
        service_names["nextcloud"]: next(iter(clients.nextcloud.compose_files_by_id.values())),
        service_names["moodle"]: (
            clients.moodle.last_create_compose_file or clients.moodle.last_update_compose_file
        ),
        service_names["docuseal"]: (
            clients.docuseal.last_create_compose_file
            or clients.docuseal.last_update_compose_file
        ),
        service_names["seaweedfs"]: clients.seaweedfs.compose_files_by_name[
            service_names["seaweedfs"]
        ],
        service_names["coder"]: clients.coder.compose_files_by_name[service_names["coder"]],
        service_names["openclaw"]: clients.openclaw.compose_files_by_name[
            service_names["openclaw"]
        ],
    }
    for service_name, rendered_compose in rendered_compose_by_service.items():
        if service_name in compose_hashes:
            continue
        assert rendered_compose is not None
        compose_hashes[service_name] = ComposeArtifactHashState.from_rendered_compose(
            service_id=service_name,
            rendered_compose=rendered_compose,
        )
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=applied_state.format_version,
            desired_state_fingerprint=applied_state.desired_state_fingerprint,
            completed_steps=applied_state.completed_steps,
            compose_artifact_hashes=compose_hashes,
            lifecycle_checkpoint_contract_version=applied_state.lifecycle_checkpoint_contract_version,
        ),
    )


def _mutation_counts(
    clients: _FullStackClients, service_names: dict[str, str]
) -> dict[str, tuple[int, int, int]]:
    shared_counts = clients.shared_core.mutation_counts(service_names["shared_core"])
    seaweed_counts = clients.seaweedfs.mutation_counts(service_names["seaweedfs"])
    coder_counts = clients.coder.mutation_counts(service_names["coder"])
    openclaw_counts = clients.openclaw.mutation_counts(service_names["openclaw"])
    return {
        "shared_core": (shared_counts.create, shared_counts.update, shared_counts.deploy),
        "nextcloud": (
            clients.nextcloud.create_compose_calls,
            clients.nextcloud.update_compose_calls,
            clients.nextcloud.deploy_calls,
        ),
        "moodle": (
            clients.moodle.create_compose_calls,
            clients.moodle.update_compose_calls,
            clients.moodle.deploy_calls,
        ),
        "docuseal": (
            clients.docuseal.create_compose_calls,
            clients.docuseal.update_compose_calls,
            clients.docuseal.deploy_calls,
        ),
        "seaweedfs": (seaweed_counts.create, seaweed_counts.update, seaweed_counts.deploy),
        "coder": (coder_counts.create, coder_counts.update, coder_counts.deploy),
        "openclaw": (openclaw_counts.create, openclaw_counts.update, openclaw_counts.deploy),
    }


def _mutation_deltas(
    before: dict[str, tuple[int, int, int]], after: dict[str, tuple[int, int, int]]
) -> dict[str, tuple[int, int, int]]:
    return {
        service_name: (
            counts[0] - before[service_name][0],
            counts[1] - before[service_name][1],
            counts[2] - before[service_name][2],
        )
        for service_name, counts in after.items()
    }


def _verification_result(
    *,
    service_name: str,
    passed: bool,
    detail: str,
    tier: Literal["app", "bootstrap", "downstream"] = "app",
) -> ServiceVerificationResult:
    return ServiceVerificationResult(
        service_name=service_name,
        tier=tier,
        status="pass" if passed else "fail",
        detail=detail,
    )
