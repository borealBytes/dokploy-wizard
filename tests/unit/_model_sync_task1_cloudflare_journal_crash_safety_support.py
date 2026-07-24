from __future__ import annotations

from collections.abc import Mapping

from dokploy_wizard.networking.cloudflare import (
    CloudflareAccessApplication,
    CloudflareAccessPolicy,
    CloudflareDnsRecord,
    CloudflareTunnel,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_fingerprints import (
    configuration_fingerprint,
    opaque_hash,
    operation_key,
    snapshot_value_hash,
    tunnel_fingerprint,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationKind,
    Task1CloudflareJournal,
    Task1CloudflareJournalIntent,
)
from tests.unit._model_sync_task1_cloudflare_journal_common import _InjectedCrash


class _ConfigurationBackend:
    def __init__(self, ingress: tuple[Mapping[str, object], ...]) -> None:
        self.ingress: tuple[dict[str, object], ...] = tuple(dict(item) for item in ingress)
        self.updates = 0

    def get_tunnel_configuration(
        self, _account_id: str, _tunnel_id: str
    ) -> tuple[dict[str, object], ...]:
        return self.ingress

    def update_tunnel_configuration(
        self, _account_id: str, _tunnel_id: str, ingress: tuple[dict[str, object], ...]
    ) -> None:
        self.updates += 1
        self.ingress = ingress


class _TunnelCleanupBackend:
    def __init__(self, *, crash_after_delete: bool) -> None:
        self.tunnel: CloudflareTunnel | None = CloudflareTunnel("tunnel-1", "task1-tunnel")
        self.configuration: tuple[dict[str, object], ...] = (
            {"hostname": "task1.example.com", "service": "https://origin"},
        )
        self.crash_after_delete = crash_after_delete

    def get_tunnel(self, _account_id: str, _tunnel_id: str) -> CloudflareTunnel | None:
        return self.tunnel

    def get_tunnel_configuration(
        self, _account_id: str, _tunnel_id: str
    ) -> tuple[dict[str, object], ...]:
        if self.tunnel is None:
            raise AssertionError("configuration must not be read after tunnel deletion")
        return self.configuration

    def delete_tunnel(self, _account_id: str, _tunnel_id: str) -> None:
        self.tunnel = None
        if self.crash_after_delete:
            self.crash_after_delete = False
            raise _InjectedCrash()

    def delete_access_policy(self, _account_id: str, _app_id: str, _policy_id: str) -> None:
        raise AssertionError("unexpected policy cleanup")

    def get_access_policy(
        self, _account_id: str, _app_id: str, _policy_id: str
    ) -> CloudflareAccessPolicy | None:
        raise AssertionError("unexpected policy lookup")

    def delete_access_application(self, _account_id: str, _app_id: str) -> None:
        raise AssertionError("unexpected application cleanup")

    def get_access_application(
        self, _account_id: str, _app_id: str
    ) -> CloudflareAccessApplication | None:
        raise AssertionError("unexpected application lookup")

    def delete_dns_record(self, _zone_id: str, _record_id: str) -> None:
        raise AssertionError("unexpected DNS cleanup")

    def get_dns_record(self, _zone_id: str, _record_id: str) -> CloudflareDnsRecord | None:
        raise AssertionError("unexpected DNS lookup")


def _record_tunnel_and_configuration(
    journal: Task1CloudflareJournal, backend: _TunnelCleanupBackend
) -> None:
    tunnel = backend.tunnel
    if tunnel is None:
        raise AssertionError("test fixture tunnel is absent")
    tunnel_intent = Task1CloudflareJournalIntent(
        logical_key=operation_key(
            JournalOperationKind.TUNNEL,
            {"account_id": "account", "name": "task1-tunnel"},
        ),
        kind=JournalOperationKind.TUNNEL,
        account_id="account",
        zone_id=None,
        parent_id=None,
        expected_name_sha256=snapshot_value_hash("task1-tunnel"),
        expected_domain_sha256=None,
        pre_absence_sha256=opaque_hash("absent"),
        desired_spec_sha256=tunnel_fingerprint(tunnel),
    )
    tunnel_operation = journal.record_intent(tunnel_intent)
    journal.checkpoint_created(
        tunnel_operation,
        response_id="tunnel-1",
        post_fingerprint_sha256=tunnel_fingerprint(tunnel),
    )
    configuration = journal.record_intent(_configuration_intent((), backend.configuration))
    journal.checkpoint_created(
        configuration,
        response_id=None,
        post_fingerprint_sha256=configuration_fingerprint(backend.configuration),
    )


def _configuration_intent(
    pre_image: tuple[Mapping[str, object], ...], desired: tuple[Mapping[str, object], ...]
) -> Task1CloudflareJournalIntent:
    scope = {"account_id": "account", "tunnel_id": "tunnel-1"}
    return Task1CloudflareJournalIntent(
        logical_key=operation_key(JournalOperationKind.TUNNEL_CONFIGURATION, scope),
        kind=JournalOperationKind.TUNNEL_CONFIGURATION,
        account_id="account",
        zone_id=None,
        parent_id="tunnel-1",
        expected_name_sha256=None,
        expected_domain_sha256=None,
        pre_absence_sha256=configuration_fingerprint(pre_image),
        desired_spec_sha256=configuration_fingerprint(desired),
    )
