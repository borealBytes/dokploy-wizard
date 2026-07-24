from __future__ import annotations

from collections.abc import Callable
from typing import assert_never

from dokploy_wizard.networking.planner import (
    _reconcile_task1_tunnel_configuration,
    _resolve_access_application,
    _resolve_access_policy,
    _resolve_dns_records,
    _resolve_tunnel,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_journal import (
    JournalOperationKind,
    Task1CloudflareJournal,
)
from dokploy_wizard.state import OwnershipLedger
from tests.unit._model_sync_task1_cloudflare_journal_mutation_backend import _MutationBackend


def _run_access_application(journal: Task1CloudflareJournal, backend: _MutationBackend) -> None:
    _resolve_access_application(
        dry_run=False,
        account_id="account",
        pack_name="openclaw",
        hostname="app.example.com",
        provider_id="otp-provider",
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
        create_only=True,
        journal=journal,
    )


def _run_access_policy(journal: Task1CloudflareJournal, backend: _MutationBackend) -> None:
    _resolve_access_policy(
        dry_run=False,
        account_id="account",
        pack_name="openclaw",
        hostname="app.example.com",
        app_id="app-1",
        emails=("owner@example.com",),
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
        create_only=True,
        journal=journal,
    )


def _run_dns_record(journal: Task1CloudflareJournal, backend: _MutationBackend) -> None:
    _resolve_dns_records(
        dry_run=False,
        account_id="account",
        zone_id="zone",
        dns_target="tunnel-1.cfargotunnel.com",
        hostnames=("dns.example.com",),
        degradable_conflict_hostnames=set(),
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
        create_only=True,
        journal=journal,
    )


def _run_tunnel_configuration(journal: Task1CloudflareJournal, backend: _MutationBackend) -> None:
    _reconcile_task1_tunnel_configuration(
        journal=journal,
        backend=backend,
        account_id="account",
        tunnel_id="tunnel-1",
        ingress=({"service": "http_status:404"},),
    )


def _run_for(
    kind: JournalOperationKind,
) -> Callable[[Task1CloudflareJournal, _MutationBackend], None]:
    match kind:
        case JournalOperationKind.ACCESS_APPLICATION:
            return _run_access_application
        case JournalOperationKind.ACCESS_POLICY:
            return _run_access_policy
        case JournalOperationKind.DNS_RECORD:
            return _run_dns_record
        case JournalOperationKind.TUNNEL_CONFIGURATION:
            return _run_tunnel_configuration
        case JournalOperationKind.TUNNEL:
            return _run_tunnel
        case unreachable:
            assert_never(unreachable)


def _run_tunnel(journal: Task1CloudflareJournal, backend: _MutationBackend) -> None:
    _resolve_tunnel(
        dry_run=False,
        account_id="account",
        tunnel_name="task1-tunnel",
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
        create_only=True,
        journal=journal,
    )
