# ruff: noqa: E501
"""Cloudflare networking planner and reconciler."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from dokploy_wizard.networking.cloudflare import (
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareBackend,
    CloudflareCertificatePack,
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
    PlannedTunnelConnector,
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

_NON_PUBLIC_HOSTNAME_KEYS = {"openclaw-internal"}
_LITELLM_ADMIN_ACCESS_KEY = "litellm-admin"
_LITELLM_ADMIN_SUBDOMAIN_ENV_KEY = "LITELLM_ADMIN_SUBDOMAIN"
_LITELLM_ADMIN_DEFAULT_SUBDOMAIN = "litellm"
_LITELLM_INTERNAL_PORT = 4000
_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


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
    connector_backend: Any | None = None,
) -> NetworkingPhase:
    credentials = _resolve_credentials(raw_env, desired_state, backend)
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

    nested_coder_wildcard = _nested_coder_wildcard_hostname(desired_state)
    if nested_coder_wildcard is not None:
        notes.extend(
            _resolve_nested_coder_wildcard_certificate(
                dry_run=dry_run,
                zone_id=credentials.zone_id,
                root_domain=desired_state.root_domain,
                coder_hostname=desired_state.hostnames.get("coder"),
                wildcard_hostname=nested_coder_wildcard,
                backend=backend,
            )
        )

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
        hostnames=tuple(sorted(_public_hostnames(desired_state).values())),
        ownership_ledger=ownership_ledger,
        backend=backend,
    )
    notes.extend(dns_notes)

    ingress_rules = _build_tunnel_ingress(desired_state)
    notes.append(
        "Planned "
        f"{len(ingress_rules) - 1} Cloudflare Tunnel ingress route(s) "
        "with a terminal 404 fallback."
    )
    if not dry_run:
        backend.update_tunnel_configuration(
            credentials.account_id,
            tunnel.tunnel_id,
            ingress_rules,
        )

    connector = _resolve_connector(
        dry_run=dry_run,
        desired_state=desired_state,
        account_id=credentials.account_id,
        tunnel=tunnel,
        backend=backend,
        connector_backend=connector_backend,
    )
    if connector is not None:
        notes.append(
            f"Cloudflare Tunnel connector {connector.action} for {connector.resource_name}."
        )
        if connector.passed:
            notes.append(f"Dokploy is publicly reachable at {connector.public_url}.")

    outcome = "plan_only" if dry_run else _derive_outcome(tunnel_action, dns_records)
    return NetworkingPhase(
        result=NetworkingResult(
            outcome=outcome,
            account_id=credentials.account_id,
            zone_id=credentials.zone_id,
            validation_checks=validation_checks,
            tunnel=planned_tunnel,
            dns_records=dns_records,
            connector=connector,
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
    credentials = _resolve_credentials(raw_env, desired_state, backend)
    emails = _access_target_emails(raw_env=raw_env, desired_state=desired_state)
    target_hostnames = _access_target_hostnames(raw_env=raw_env, desired_state=desired_state)
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
    for pack_name, hostname in target_hostnames:
        app, app_action = _resolve_access_application(
            dry_run=dry_run,
            account_id=credentials.account_id,
            pack_name=pack_name,
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
            pack_name=pack_name,
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
    notes = [
        "Cloudflare Access self-hosted applications are applied to advisor hostnames and the LiteLLM admin hostname."
    ]
    litellm_hostname = dict(target_hostnames).get(_LITELLM_ADMIN_ACCESS_KEY)
    if litellm_hostname is not None:
        notes.extend(_litellm_access_notes(desired_state=desired_state, hostname=litellm_hostname))
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
            notes=tuple(notes),
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
    raw_env: RawEnvInput, desired_state: DesiredState, backend: CloudflareBackend
) -> CloudflareCredentials:
    values = raw_env.values
    account_id = _require_env_value(values, "CLOUDFLARE_ACCOUNT_ID")
    zone_id = values.get("CLOUDFLARE_ZONE_ID")
    if zone_id is None:
        zone_id = backend.resolve_zone_id(account_id, desired_state.root_domain)
        if zone_id is None:
            raise CloudflareError(
                "Cloudflare could not find a matching zone for the root domain. "
                "Use the root domain managed in Cloudflare or provide an explicit Zone ID."
            )
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
    pack_name: str,
    hostname: str,
    provider_id: str,
    ownership_ledger: OwnershipLedger,
    backend: CloudflareBackend,
) -> tuple[CloudflareAccessApplication, str]:
    app_name = f"{_access_display_name(pack_name)} protected"
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
    pack_name: str,
    hostname: str,
    app_id: str,
    emails: tuple[str, ...],
    ownership_ledger: OwnershipLedger,
    backend: CloudflareBackend,
) -> tuple[CloudflareAccessPolicy, str]:
    policy_name = f"Allow {_access_display_name(pack_name)}"
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


def _access_display_name(pack_name: str) -> str:
    if pack_name == "openclaw":
        return "Nexa Claw"
    if pack_name == "my-farm-advisor":
        return "Nexa Farm"
    if pack_name == _LITELLM_ADMIN_ACCESS_KEY:
        return "LiteLLM Admin"
    return pack_name


def _access_target_hostnames(
    *, raw_env: RawEnvInput, desired_state: DesiredState
) -> tuple[tuple[str, str], ...]:
    target_hostnames: list[tuple[str, str]] = [
        (key, desired_state.hostnames[key])
        for key in ("openclaw", "my-farm-advisor")
        if key in desired_state.enabled_packs and key in desired_state.hostnames
    ]
    litellm_hostname = resolve_litellm_admin_hostname(raw_env=raw_env, desired_state=desired_state)
    if litellm_hostname is not None:
        target_hostnames.append(
            (
                _LITELLM_ADMIN_ACCESS_KEY,
                litellm_hostname,
            )
        )
    return tuple(target_hostnames)


def resolve_litellm_admin_hostname(*, raw_env: RawEnvInput, desired_state: DesiredState) -> str | None:
    if desired_state.shared_core.litellm is None:
        return None
    return _shared_service_admin_hostname(raw_env=raw_env, desired_state=desired_state)


def _shared_service_admin_hostname(*, raw_env: RawEnvInput, desired_state: DesiredState) -> str:
    subdomain = (
        raw_env.values.get(_LITELLM_ADMIN_SUBDOMAIN_ENV_KEY, _LITELLM_ADMIN_DEFAULT_SUBDOMAIN)
        .strip()
        .lower()
    )
    if not subdomain:
        subdomain = _LITELLM_ADMIN_DEFAULT_SUBDOMAIN
    if _DNS_LABEL_PATTERN.fullmatch(subdomain) is None:
        raise CloudflareError(
            f"{_LITELLM_ADMIN_SUBDOMAIN_ENV_KEY} must be a single DNS label containing only lowercase letters, digits, or hyphens."
        )
    hostname = f"{subdomain}.{desired_state.root_domain}"
    if hostname in set(desired_state.hostnames.values()):
        raise CloudflareError(
            f"LiteLLM admin hostname '{hostname}' collides with an existing desired hostname. Choose a different {_LITELLM_ADMIN_SUBDOMAIN_ENV_KEY}."
        )
    return hostname


def _access_target_emails(*, raw_env: RawEnvInput, desired_state: DesiredState) -> tuple[str, ...]:
    if desired_state.cloudflare_access_otp_emails:
        return desired_state.cloudflare_access_otp_emails
    if desired_state.shared_core.litellm is None:
        return ()
    admin_email = raw_env.values.get("DOKPLOY_ADMIN_EMAIL", "").strip().lower()
    if admin_email and "@" in admin_email:
        return (admin_email,)
    return ()


def _litellm_access_notes(*, desired_state: DesiredState, hostname: str) -> tuple[str, ...]:
    if desired_state.shared_core.litellm is None:
        return ()
    internal_url = (
        f"http://{desired_state.shared_core.litellm.service_name}:{_LITELLM_INTERNAL_PORT}"
    )
    admin_url = f"https://{hostname}"
    return (
        f"LiteLLM internal containers should keep using '{internal_url}'.",
        f"LiteLLM admin access is planned separately at '{admin_url}' behind Cloudflare Access.",
        "LiteLLM admin QA must treat 302/401/403 as protected success and must never accept an unauthenticated 200.",
        "LiteLLM admin DNS and tunnel ingress must remain separate from the internal service URL until Access protection is in place.",
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


def _build_tunnel_ingress(desired_state: DesiredState) -> tuple[dict[str, object], ...]:
    public_hostnames = _public_hostnames(desired_state)
    ingress: list[dict[str, object]] = [
        {
            "hostname": public_hostnames["dokploy"],
            "service": "https://localhost:443",
            "originRequest": {"noTLSVerify": True},
        },
    ]
    seen_hosts = {public_hostnames["dokploy"]}
    wildcard_hostnames_present = False
    for key, hostname in public_hostnames.items():
        if hostname in seen_hosts:
            continue
        if key == "dokploy":
            continue
        if hostname.startswith("*."):
            wildcard_hostnames_present = True
            seen_hosts.add(hostname)
            continue
        ingress.append(
            {
                "hostname": hostname,
                "service": "https://localhost:443",
                "originRequest": {"noTLSVerify": True},
            }
        )
        seen_hosts.add(hostname)
    if wildcard_hostnames_present:
        ingress.append(
            {
                "service": "https://localhost:443",
                "originRequest": {"noTLSVerify": True},
            }
        )
    else:
        ingress.append({"service": "http_status:404"})
    return tuple(ingress)


def _nested_coder_wildcard_hostname(desired_state: DesiredState) -> str | None:
    if "coder" not in desired_state.enabled_packs:
        return None
    wildcard_hostname = desired_state.hostnames.get("coder-wildcard")
    if wildcard_hostname is None or not wildcard_hostname.startswith("*."):
        return None
    if wildcard_hostname == f"*.{desired_state.root_domain}":
        return None
    return wildcard_hostname


def _resolve_nested_coder_wildcard_certificate(
    *,
    dry_run: bool,
    zone_id: str,
    root_domain: str,
    coder_hostname: str | None,
    wildcard_hostname: str,
    backend: CloudflareBackend,
) -> tuple[str, ...]:
    if coder_hostname is None:
        raise CloudflareError("Coder hostname is required when configuring wildcard app routing.")
    try:
        existing_pack = _find_certificate_pack_for_hostname(
            backend.list_certificate_packs(zone_id), wildcard_hostname
        )
    except CloudflareError as exc:
        raise CloudflareError(
            "Nested Coder wildcard routing requires Cloudflare edge certificate access for "
            f"'{wildcard_hostname}'. Ensure the token includes "
            "'Zone -> SSL and Certificates -> Edit' "
            "and that Advanced Certificate Manager is enabled. "
            f"Underlying error: {exc}"
        ) from exc
    if existing_pack is not None:
        return (
            "Cloudflare edge certificate "
            f"'{existing_pack.pack_id}' "
            f"({existing_pack.pack_type}, {existing_pack.status}) already covers "
            f"'{wildcard_hostname}'.",
        )
    if dry_run:
        return (
            f"Would order a Cloudflare advanced edge certificate for '{wildcard_hostname}'.",
        )
    try:
        created_pack = backend.order_advanced_certificate_pack(
            zone_id,
            hosts=(root_domain, coder_hostname, wildcard_hostname),
        )
    except CloudflareError as exc:
        raise CloudflareError(
            "Unable to order the Cloudflare advanced edge certificate required for nested "
            f"Coder app hosts under '{wildcard_hostname}'. Ensure Advanced Certificate "
            "Manager is enabled and the "
            "token includes 'Zone -> SSL and Certificates -> Edit'. "
            f"Underlying error: {exc}"
        ) from exc
    note = (
        f"Ordered Cloudflare advanced edge certificate '{created_pack.pack_id}' for "
        f"'{wildcard_hostname}' "
        f"with status '{created_pack.status}'."
    )
    if created_pack.status != "active":
        note = f"{note} Public TLS may take a few minutes to become active."
    return (note,)


def _find_certificate_pack_for_hostname(
    certificate_packs: tuple[CloudflareCertificatePack, ...], hostname: str
) -> CloudflareCertificatePack | None:
    for pack in certificate_packs:
        if hostname in pack.hosts:
            return pack
    return None


def _public_hostnames(desired_state: DesiredState) -> dict[str, str]:
    return {
        key: hostname
        for key, hostname in desired_state.hostnames.items()
        if key not in _NON_PUBLIC_HOSTNAME_KEYS
    }


def _resolve_connector(
    *,
    dry_run: bool,
    desired_state: DesiredState,
    account_id: str,
    tunnel: CloudflareTunnel,
    backend: CloudflareBackend,
    connector_backend: Any | None,
) -> PlannedTunnelConnector | None:
    if connector_backend is None:
        return None
    resource_name = f"{desired_state.stack_name}-cloudflared"
    existing = connector_backend.find_service_by_name(resource_name)
    action = "reuse_existing" if existing is not None else "create"
    if dry_run:
        return PlannedTunnelConnector(
            action=action,
            resource_id=None if existing is None else existing.resource_id,
            resource_name=resource_name,
            public_url=desired_state.dokploy_url,
            passed=None,
        )

    tunnel_token = backend.get_tunnel_token(account_id, tunnel.tunnel_id)
    service = connector_backend.create_service(
        resource_name=resource_name,
        tunnel_token=tunnel_token,
    )
    passed = connector_backend.check_health(service=service, url=desired_state.dokploy_url)
    if not passed:
        raise CloudflareError(
            "Dokploy public URL did not become reachable after starting the "
            f"Cloudflare Tunnel connector: {desired_state.dokploy_url}"
        )
    return PlannedTunnelConnector(
        action=action,
        resource_id=service.resource_id,
        resource_name=service.resource_name,
        public_url=desired_state.dokploy_url,
        passed=True,
    )


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
