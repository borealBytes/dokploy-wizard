"""Cloudflare networking planner and reconciler."""

from __future__ import annotations

from dataclasses import dataclass

from dokploy_wizard.networking.cloudflare import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareBackend,
    CloudflareDnsRecord,
    CloudflareError,
    CloudflareTunnel,
)
from dokploy_wizard.networking.models import (
    AccessPhase,
    AccessResult,
    NetworkingPhase,
    NetworkingResult,
    PlannedAccessApplication,
    PlannedAccessIdentityProvider,
    PlannedAccessPolicy,
    PlannedDnsRecord,
    PlannedTunnel,
)
from dokploy_wizard.state import (
    DesiredState,
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    StateValidationError,
)

TUNNEL_RESOURCE_TYPE = "cloudflare_tunnel"
DNS_RESOURCE_TYPE = "cloudflare_dns_record"
ACCESS_OTP_PROVIDER_RESOURCE_TYPE = "cloudflare_access_otp_provider"
ACCESS_APPLICATION_RESOURCE_TYPE = "cloudflare_access_application"
ACCESS_POLICY_RESOURCE_TYPE = "cloudflare_access_policy"


@dataclass(frozen=True)
class CloudflareCredentials:
    account_id: str
    zone_id: str
    tunnel_name: str


def reconcile_networking(
    *,
    dry_run: bool,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: CloudflareBackend,
) -> NetworkingPhase:
    credentials = _resolve_credentials(raw_env, desired_state)
    backend.validate_account_access(credentials.account_id)
    backend.validate_zone_access(credentials.zone_id)

    validation_checks = (
        "account_cloudflare_tunnel_scope_validated",
        "zone_dns_scope_validated",
    )
    notes = [
        "Cloudflare account scope validated for Cloudflare Tunnel Read/Edit.",
        "Cloudflare zone scope validated for DNS Read/Edit.",
    ]

    tunnel, tunnel_action = _resolve_tunnel(
        dry_run=dry_run,
        account_id=credentials.account_id,
        tunnel_name=credentials.tunnel_name,
        ownership_ledger=ownership_ledger,
        backend=backend,
    )
    dns_target = f"{tunnel.tunnel_id}.cfargotunnel.com"
    planned_tunnel = PlannedTunnel(
        action=tunnel_action,
        tunnel_id=tunnel.tunnel_id,
        tunnel_name=tunnel.name,
        dns_target=dns_target,
    )

    dns_records, dns_resource_ids, dns_notes = _resolve_dns_records(
        dry_run=dry_run,
        zone_id=credentials.zone_id,
        dns_target=dns_target,
        hostnames=tuple(sorted(desired_state.hostnames.values())),
        ownership_ledger=ownership_ledger,
        backend=backend,
    )
    notes.extend(dns_notes)

    outcome = "plan_only" if dry_run else _derive_outcome(tunnel_action, dns_records)
    return NetworkingPhase(
        result=NetworkingResult(
            outcome=outcome,
            account_id=credentials.account_id,
            zone_id=credentials.zone_id,
            validation_checks=validation_checks,
            tunnel=planned_tunnel,
            dns_records=dns_records,
            notes=tuple(notes),
        ),
        tunnel_resource_id=None if dry_run else tunnel.tunnel_id,
        dns_resource_ids={} if dry_run else dns_resource_ids,
    )


def build_networking_ledger(
    *,
    existing_ledger: OwnershipLedger,
    account_id: str,
    zone_id: str,
    tunnel_resource_id: str,
    dns_resource_ids: dict[str, str],
) -> OwnershipLedger:
    resources = [
        resource
        for resource in existing_ledger.resources
        if resource.resource_type not in {TUNNEL_RESOURCE_TYPE, DNS_RESOURCE_TYPE}
    ]
    resources.append(
        OwnedResource(
            resource_type=TUNNEL_RESOURCE_TYPE,
            resource_id=tunnel_resource_id,
            scope=_account_scope(account_id),
        )
    )
    for hostname, resource_id in sorted(dns_resource_ids.items()):
        resources.append(
            OwnedResource(
                resource_type=DNS_RESOURCE_TYPE,
                resource_id=resource_id,
                scope=_dns_scope(zone_id, hostname),
            )
        )
    return OwnershipLedger(
        format_version=existing_ledger.format_version, resources=tuple(resources)
    )


def reconcile_cloudflare_access(
    *,
    dry_run: bool,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: CloudflareBackend,
) -> AccessPhase:
    credentials = _resolve_credentials(raw_env, desired_state)
    emails = desired_state.cloudflare_access_otp_emails
    target_hostnames = tuple(
        desired_state.hostnames[key]
        for key in ("openclaw", "my-farm-advisor")
        if key in desired_state.enabled_packs and key in desired_state.hostnames
    )
    if not emails or not target_hostnames:
        return AccessPhase(
            result=AccessResult(
                outcome="skipped",
                account_id=credentials.account_id,
                otp_provider=None,
                applications=(),
                policies=(),
                notes=("Cloudflare Access hardening is not enabled for advisor hostnames.",),
            ),
            provider_resource_id=None,
            application_resource_ids={},
            policy_resource_ids={},
        )

    provider, provider_action = _resolve_access_identity_provider(
        dry_run=dry_run,
        account_id=credentials.account_id,
        ownership_ledger=ownership_ledger,
        backend=backend,
    )
    apps: list[PlannedAccessApplication] = []
    app_ids: dict[str, str] = {}
    policies: list[PlannedAccessPolicy] = []
    policy_ids: dict[str, str] = {}
    for hostname in target_hostnames:
        app, app_action = _resolve_access_application(
            dry_run=dry_run,
            account_id=credentials.account_id,
            hostname=hostname,
            provider_id=provider.provider_id,
            ownership_ledger=ownership_ledger,
            backend=backend,
        )
        apps.append(
            PlannedAccessApplication(action=app_action, hostname=hostname, app_id=app.app_id)
        )
        if not dry_run:
            app_ids[hostname] = app.app_id
        policy, policy_action = _resolve_access_policy(
            dry_run=dry_run,
            account_id=credentials.account_id,
            hostname=hostname,
            app_id=app.app_id,
            emails=emails,
            ownership_ledger=ownership_ledger,
            backend=backend,
        )
        policies.append(
            PlannedAccessPolicy(
                action=policy_action,
                hostname=hostname,
                policy_id=policy.policy_id,
                emails=policy.emails,
            )
        )
        if not dry_run:
            policy_ids[hostname] = policy.policy_id

    actions = {
        provider_action,
        *(item.action for item in apps),
        *(item.action for item in policies),
    }
    outcome = "plan_only" if dry_run else ("applied" if "create" in actions else "already_present")
    return AccessPhase(
        result=AccessResult(
            outcome=outcome,
            account_id=credentials.account_id,
            otp_provider=PlannedAccessIdentityProvider(
                action=provider_action,
                provider_id=provider.provider_id,
                name=provider.name,
            ),
            applications=tuple(apps),
            policies=tuple(policies),
            notes=(
                "Cloudflare Access self-hosted applications are applied only to advisor hostnames.",
            ),
        ),
        provider_resource_id=None if dry_run else provider.provider_id,
        application_resource_ids=app_ids,
        policy_resource_ids=policy_ids,
    )


def build_access_ledger(
    *,
    existing_ledger: OwnershipLedger,
    account_id: str,
    provider_resource_id: str | None,
    application_resource_ids: dict[str, str],
    policy_resource_ids: dict[str, str],
) -> OwnershipLedger:
    resources = [
        resource
        for resource in existing_ledger.resources
        if resource.resource_type
        not in {
            ACCESS_OTP_PROVIDER_RESOURCE_TYPE,
            ACCESS_APPLICATION_RESOURCE_TYPE,
            ACCESS_POLICY_RESOURCE_TYPE,
        }
    ]
    if provider_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=ACCESS_OTP_PROVIDER_RESOURCE_TYPE,
                resource_id=provider_resource_id,
                scope=_access_provider_scope(account_id),
            )
        )
    for hostname, resource_id in sorted(application_resource_ids.items()):
        resources.append(
            OwnedResource(
                resource_type=ACCESS_APPLICATION_RESOURCE_TYPE,
                resource_id=resource_id,
                scope=_access_application_scope(account_id, hostname),
            )
        )
    for hostname, resource_id in sorted(policy_resource_ids.items()):
        resources.append(
            OwnedResource(
                resource_type=ACCESS_POLICY_RESOURCE_TYPE,
                resource_id=resource_id,
                scope=_access_policy_scope(account_id, hostname),
            )
        )
    return OwnershipLedger(
        format_version=existing_ledger.format_version, resources=tuple(resources)
    )


def _resolve_credentials(
    raw_env: RawEnvInput, desired_state: DesiredState
) -> CloudflareCredentials:
    values = raw_env.values
    account_id = _require_env_value(values, "CLOUDFLARE_ACCOUNT_ID")
    zone_id = _require_env_value(values, "CLOUDFLARE_ZONE_ID")
    tunnel_name = values.get("CLOUDFLARE_TUNNEL_NAME", f"{desired_state.stack_name}-tunnel")
    return CloudflareCredentials(account_id=account_id, zone_id=zone_id, tunnel_name=tunnel_name)


def _resolve_tunnel(
    *,
    dry_run: bool,
    account_id: str,
    tunnel_name: str,
    ownership_ledger: OwnershipLedger,
    backend: CloudflareBackend,
) -> tuple[CloudflareTunnel, str]:
    ledger_tunnel = _find_owned_tunnel(ownership_ledger, account_id)
    if ledger_tunnel is not None:
        tunnel = backend.get_tunnel(account_id, ledger_tunnel.resource_id)
        if tunnel is None:
            raise CloudflareError(
                "Ownership ledger says the Cloudflare tunnel exists, but the account-scoped "
                "validation endpoint did not find it."
            )
        if tunnel.name != tunnel_name:
            raise CloudflareError(
                "Ownership ledger tunnel exists, but its name no longer matches the desired "
                "Cloudflare tunnel intent."
            )
        return tunnel, "reuse_owned"

    tunnel = backend.find_tunnel_by_name(account_id, tunnel_name)
    if tunnel is not None:
        return tunnel, "reuse_existing"
    if dry_run:
        return CloudflareTunnel(tunnel_id="planned-tunnel", name=tunnel_name), "create"
    return backend.create_tunnel(account_id, tunnel_name), "create"


def _resolve_access_identity_provider(
    *,
    dry_run: bool,
    account_id: str,
    ownership_ledger: OwnershipLedger,
    backend: CloudflareBackend,
) -> tuple[CloudflareAccessIdentityProvider, str]:
    provider_name = "One-time PIN login"
    owned_provider = _find_owned_access_resource(
        ownership_ledger,
        resource_type=ACCESS_OTP_PROVIDER_RESOURCE_TYPE,
        scope=_access_provider_scope(account_id),
    )
    if owned_provider is not None:
        provider = backend.get_access_identity_provider(account_id, owned_provider.resource_id)
        if provider is None:
            raise CloudflareError(
                "Ownership ledger says the Cloudflare Access OTP provider exists, "
                "but the account-scoped endpoint did not find it."
            )
        if provider.name != provider_name or provider.provider_type != "onetimepin":
            raise CloudflareError(
                "Ownership ledger Access OTP provider no longer matches the desired configuration."
            )
        return provider, "reuse_owned"
    provider = backend.find_access_identity_provider_by_name(account_id, provider_name)
    if provider is not None:
        if provider.provider_type != "onetimepin":
            raise CloudflareError(
                "Cloudflare Access identity provider name collision detected for the OTP provider."
            )
        return provider, "reuse_existing"
    if dry_run:
        return (
            CloudflareAccessIdentityProvider(
                provider_id="planned-access-otp-provider",
                name=provider_name,
                provider_type="onetimepin",
            ),
            "create",
        )
    return backend.create_access_identity_provider(account_id, provider_name), "create"


def _resolve_access_application(
    *,
    dry_run: bool,
    account_id: str,
    hostname: str,
    provider_id: str,
    ownership_ledger: OwnershipLedger,
    backend: CloudflareBackend,
) -> tuple[CloudflareAccessApplication, str]:
    app_name = f"{hostname} protected"
    owned_app = _find_owned_access_resource(
        ownership_ledger,
        resource_type=ACCESS_APPLICATION_RESOURCE_TYPE,
        scope=_access_application_scope(account_id, hostname),
    )
    if owned_app is not None:
        app = backend.get_access_application(account_id, owned_app.resource_id)
        if app is None:
            raise CloudflareError(
                f"Ownership ledger says the Access app for '{hostname}' exists, "
                "but Cloudflare did not find it."
            )
        if (
            app.domain != hostname
            or app.app_type != "self_hosted"
            or provider_id not in app.allowed_identity_provider_ids
        ):
            raise CloudflareError(
                f"Ownership ledger Access app for '{hostname}' no longer matches "
                "the desired self-hosted configuration."
            )
        return app, "reuse_owned"
    app = backend.find_access_application_by_domain(account_id, hostname)
    if app is not None:
        if app.app_type != "self_hosted" or provider_id not in app.allowed_identity_provider_ids:
            raise CloudflareError(f"Cloudflare Access app collision detected for '{hostname}'.")
        return app, "reuse_existing"
    if dry_run:
        return (
            CloudflareAccessApplication(
                app_id=f"planned-access-app-{hostname}",
                name=app_name,
                domain=hostname,
                app_type="self_hosted",
                allowed_identity_provider_ids=(provider_id,),
            ),
            "create",
        )
    return (
        backend.create_access_application(
            account_id,
            name=app_name,
            domain=hostname,
            allowed_identity_provider_ids=(provider_id,),
        ),
        "create",
    )


def _resolve_access_policy(
    *,
    dry_run: bool,
    account_id: str,
    hostname: str,
    app_id: str,
    emails: tuple[str, ...],
    ownership_ledger: OwnershipLedger,
    backend: CloudflareBackend,
) -> tuple[CloudflareAccessPolicy, str]:
    policy_name = f"Allow {hostname}"
    owned_policy = _find_owned_access_resource(
        ownership_ledger,
        resource_type=ACCESS_POLICY_RESOURCE_TYPE,
        scope=_access_policy_scope(account_id, hostname),
    )
    if owned_policy is not None:
        policy = backend.get_access_policy(account_id, app_id, owned_policy.resource_id)
        if policy is None:
            raise CloudflareError(
                f"Ownership ledger says the Access policy for '{hostname}' exists, "
                "but Cloudflare did not find it."
            )
        if policy.decision != "allow" or policy.emails != emails:
            raise CloudflareError(
                f"Ownership ledger Access policy for '{hostname}' no longer matches "
                "the desired email allowlist."
            )
        return policy, "reuse_owned"
    policy = backend.find_access_policy_by_name(account_id, app_id, policy_name)
    if policy is not None:
        if policy.decision != "allow" or policy.emails != emails:
            raise CloudflareError(f"Cloudflare Access policy collision detected for '{hostname}'.")
        return policy, "reuse_existing"
    if dry_run:
        return (
            CloudflareAccessPolicy(
                policy_id=f"planned-access-policy-{hostname}",
                app_id=app_id,
                name=policy_name,
                decision="allow",
                emails=emails,
            ),
            "create",
        )
    return (
        backend.create_access_policy(
            account_id,
            app_id=app_id,
            name=policy_name,
            emails=emails,
        ),
        "create",
    )


def _resolve_dns_records(
    *,
    dry_run: bool,
    zone_id: str,
    dns_target: str,
    hostnames: tuple[str, ...],
    ownership_ledger: OwnershipLedger,
    backend: CloudflareBackend,
) -> tuple[tuple[PlannedDnsRecord, ...], dict[str, str], tuple[str, ...]]:
    planned_records: list[PlannedDnsRecord] = []
    resource_ids: dict[str, str] = {}
    notes: list[str] = []

    for hostname in hostnames:
        owned_record = _find_owned_dns_record(ownership_ledger, zone_id, hostname)
        if owned_record is not None:
            exact_records = backend.list_dns_records(
                zone_id,
                hostname=hostname,
                record_type="CNAME",
                content=dns_target,
            )
            matching_record = next(
                (
                    record
                    for record in exact_records
                    if record.record_id == owned_record.resource_id
                ),
                None,
            )
            if matching_record is None or not matching_record.proxied:
                raise CloudflareError(
                    f"Ownership ledger says DNS record '{hostname}' exists, but Cloudflare no "
                    "longer agrees with the zone-scoped record state."
                )
            planned_records.append(
                PlannedDnsRecord(
                    action="reuse_owned",
                    hostname=hostname,
                    record_id=matching_record.record_id,
                    content=matching_record.content,
                    proxied=matching_record.proxied,
                )
            )
            resource_ids[hostname] = matching_record.record_id
            continue

        existing_records = backend.list_dns_records(
            zone_id,
            hostname=hostname,
            record_type="CNAME",
            content=None,
        )
        compatible_record = _select_compatible_record(existing_records, dns_target)
        if compatible_record is not None:
            planned_records.append(
                PlannedDnsRecord(
                    action="reuse_existing",
                    hostname=hostname,
                    record_id=compatible_record.record_id,
                    content=compatible_record.content,
                    proxied=compatible_record.proxied,
                )
            )
            resource_ids[hostname] = compatible_record.record_id
            continue
        if existing_records:
            raise CloudflareError(
                f"Cloudflare already has a conflicting CNAME record for '{hostname}' in the "
                "configured zone."
            )
        if dry_run:
            planned_records.append(
                PlannedDnsRecord(
                    action="create",
                    hostname=hostname,
                    record_id=f"planned-{hostname}",
                    content=dns_target,
                    proxied=True,
                )
            )
            continue
        created_record = backend.create_dns_record(
            zone_id,
            hostname=hostname,
            content=dns_target,
            proxied=True,
        )
        planned_records.append(
            PlannedDnsRecord(
                action="create",
                hostname=hostname,
                record_id=created_record.record_id,
                content=created_record.content,
                proxied=created_record.proxied,
            )
        )
        resource_ids[hostname] = created_record.record_id

    notes.append(
        f"Planned {len(planned_records)} Cloudflare DNS CNAME record(s) from desired hostnames."
    )
    return tuple(planned_records), resource_ids, tuple(notes)


def _derive_outcome(tunnel_action: str, dns_records: tuple[PlannedDnsRecord, ...]) -> str:
    actions = {tunnel_action, *(record.action for record in dns_records)}
    if "create" in actions:
        return "applied"
    return "already_present"


def _select_compatible_record(
    records: tuple[CloudflareDnsRecord, ...], dns_target: str
) -> CloudflareDnsRecord | None:
    for record in records:
        if record.content == dns_target and record.proxied and record.record_type == "CNAME":
            return record
    return None


def _find_owned_tunnel(ownership_ledger: OwnershipLedger, account_id: str) -> OwnedResource | None:
    matches = [
        resource
        for resource in ownership_ledger.resources
        if resource.resource_type == TUNNEL_RESOURCE_TYPE
        and resource.scope == _account_scope(account_id)
    ]
    if len(matches) > 1:
        raise CloudflareError(
            "Ownership ledger contains multiple Cloudflare tunnels for one account."
        )
    return matches[0] if matches else None


def _find_owned_dns_record(
    ownership_ledger: OwnershipLedger, zone_id: str, hostname: str
) -> OwnedResource | None:
    matches = [
        resource
        for resource in ownership_ledger.resources
        if resource.resource_type == DNS_RESOURCE_TYPE
        and resource.scope == _dns_scope(zone_id, hostname)
    ]
    if len(matches) > 1:
        raise CloudflareError(
            f"Ownership ledger contains multiple Cloudflare DNS records for '{hostname}'."
        )
    return matches[0] if matches else None


def _find_owned_access_resource(
    ownership_ledger: OwnershipLedger, *, resource_type: str, scope: str
) -> OwnedResource | None:
    matches = [
        resource
        for resource in ownership_ledger.resources
        if resource.resource_type == resource_type and resource.scope == scope
    ]
    if len(matches) > 1:
        raise CloudflareError(
            f"Ownership ledger contains multiple Cloudflare Access resources for scope '{scope}'."
        )
    return matches[0] if matches else None


def _account_scope(account_id: str) -> str:
    return f"account:{account_id}"


def _dns_scope(zone_id: str, hostname: str) -> str:
    return f"zone:{zone_id}:{hostname}"


def _access_provider_scope(account_id: str) -> str:
    return f"account:{account_id}:access-otp-provider"


def _access_application_scope(account_id: str, hostname: str) -> str:
    return f"account:{account_id}:access-app:{hostname.lower()}"


def _access_policy_scope(account_id: str, hostname: str) -> str:
    return f"account:{account_id}:access-policy:{hostname.lower()}"


def _require_env_value(values: dict[str, str], key: str) -> str:
    value = values.get(key)
    if value is None or value == "":
        raise StateValidationError(f"Missing required env key '{key}'.")
    return value
