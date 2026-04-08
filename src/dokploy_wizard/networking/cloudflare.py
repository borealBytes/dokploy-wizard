"""Cloudflare backend protocol and default API-backed implementation."""

from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import error, parse, request

from dokploy_wizard.state import RawEnvInput, StateValidationError

API_BASE_URL = "https://api.cloudflare.com/client/v4"


class CloudflareError(RuntimeError):
    """Raised when Cloudflare credential validation or reconciliation fails."""


@dataclass(frozen=True)
class CloudflareTunnel:
    tunnel_id: str
    name: str


@dataclass(frozen=True)
class CloudflareDnsRecord:
    record_id: str
    name: str
    record_type: str
    content: str
    proxied: bool


@dataclass(frozen=True)
class CloudflareAccessIdentityProvider:
    provider_id: str
    name: str
    provider_type: str


@dataclass(frozen=True)
class CloudflareAccessApplication:
    app_id: str
    name: str
    domain: str
    app_type: str
    allowed_identity_provider_ids: tuple[str, ...]


@dataclass(frozen=True)
class CloudflareAccessPolicy:
    policy_id: str
    app_id: str
    name: str
    decision: str
    emails: tuple[str, ...]


class CloudflareBackend(Protocol):
    def validate_account_access(self, account_id: str) -> None: ...

    def resolve_zone_id(self, account_id: str, zone_name: str) -> str | None: ...

    def validate_zone_access(self, zone_id: str) -> None: ...

    def get_tunnel(self, account_id: str, tunnel_id: str) -> CloudflareTunnel | None: ...

    def find_tunnel_by_name(self, account_id: str, tunnel_name: str) -> CloudflareTunnel | None: ...

    def create_tunnel(self, account_id: str, tunnel_name: str) -> CloudflareTunnel: ...

    def list_dns_records(
        self,
        zone_id: str,
        *,
        hostname: str,
        record_type: str,
        content: str | None,
    ) -> tuple[CloudflareDnsRecord, ...]: ...

    def create_dns_record(
        self,
        zone_id: str,
        *,
        hostname: str,
        content: str,
        proxied: bool,
    ) -> CloudflareDnsRecord: ...

    def get_access_identity_provider(
        self, account_id: str, provider_id: str
    ) -> CloudflareAccessIdentityProvider | None: ...

    def find_access_identity_provider_by_name(
        self, account_id: str, name: str
    ) -> CloudflareAccessIdentityProvider | None: ...

    def create_access_identity_provider(
        self, account_id: str, name: str
    ) -> CloudflareAccessIdentityProvider: ...

    def get_access_application(
        self, account_id: str, app_id: str
    ) -> CloudflareAccessApplication | None: ...

    def find_access_application_by_domain(
        self, account_id: str, domain: str
    ) -> CloudflareAccessApplication | None: ...

    def create_access_application(
        self,
        account_id: str,
        *,
        name: str,
        domain: str,
        allowed_identity_provider_ids: tuple[str, ...],
    ) -> CloudflareAccessApplication: ...

    def get_access_policy(
        self, account_id: str, app_id: str, policy_id: str
    ) -> CloudflareAccessPolicy | None: ...

    def find_access_policy_by_name(
        self, account_id: str, app_id: str, name: str
    ) -> CloudflareAccessPolicy | None: ...

    def create_access_policy(
        self,
        account_id: str,
        *,
        app_id: str,
        name: str,
        emails: tuple[str, ...],
    ) -> CloudflareAccessPolicy: ...


class CloudflareApiBackend:
    """Default Cloudflare backend using live API calls or env-driven fixtures."""

    def __init__(self, raw_env: RawEnvInput) -> None:
        values = raw_env.values
        self._token = _require_env_value(values, "CLOUDFLARE_API_TOKEN")
        self._mock_tunnel_name = values.get("CLOUDFLARE_TUNNEL_NAME", "mock-tunnel")
        self._mock_account_ok = _optional_bool(values, "CLOUDFLARE_MOCK_ACCOUNT_OK")
        self._mock_zone_ok = _optional_bool(values, "CLOUDFLARE_MOCK_ZONE_OK")
        self._mock_existing_tunnel_id = values.get("CLOUDFLARE_MOCK_EXISTING_TUNNEL_ID")
        self._mock_existing_hostnames = {
            item.strip().lower()
            for item in values.get("CLOUDFLARE_MOCK_EXISTING_HOSTNAMES", "").split(",")
            if item.strip() != ""
        }
        self._mock_access_app_ids: dict[str, str] = {}
        self._mock_access_policies: dict[str, CloudflareAccessPolicy] = {}

    def validate_account_access(self, account_id: str) -> None:
        if self._mock_account_ok is not None:
            if not self._mock_account_ok:
                msg = (
                    "Cloudflare token cannot access the configured account for Cloudflare Tunnel "
                    "Read/Edit operations."
                )
                raise CloudflareError(msg)
            return
        self._request_json(
            method="GET",
            path=f"/accounts/{account_id}/cfd_tunnel",
            params={"is_deleted": "false", "per_page": "1"},
        )

    def validate_zone_access(self, zone_id: str) -> None:
        if self._mock_zone_ok is not None:
            if not self._mock_zone_ok:
                msg = (
                    "Cloudflare token cannot access the configured zone for DNS Read/Edit "
                    "operations."
                )
                raise CloudflareError(msg)
            return
        self.list_dns_records(
            zone_id, hostname="validation.invalid", record_type="CNAME", content=None
        )

    def resolve_zone_id(self, account_id: str, zone_name: str) -> str | None:
        if self._mock_zone_ok is not None:
            return _mock_zone_id(zone_name)

        payload = self._request_json(
            method="GET",
            path="/zones",
            params={"name": zone_name, "account.id": account_id, "per_page": "100"},
        )
        zones = payload.get("result")
        if not isinstance(zones, list):
            raise CloudflareError("Cloudflare returned an invalid zone list response.")
        exact_matches = [
            item for item in zones if isinstance(item, dict) and item.get("name") == zone_name
        ]
        if not exact_matches:
            return None
        if len(exact_matches) > 1:
            raise CloudflareError(
                "Cloudflare returned multiple matching zones for the requested root domain."
            )
        zone_id = exact_matches[0].get("id")
        if not isinstance(zone_id, str) or zone_id == "":
            raise CloudflareError("Cloudflare zone payload is missing a valid id.")
        return zone_id

    def get_tunnel(self, account_id: str, tunnel_id: str) -> CloudflareTunnel | None:
        if self._mock_account_ok is not None:
            expected_tunnel_id = self._mock_existing_tunnel_id or _mock_tunnel_id(
                self._mock_tunnel_name
            )
            if expected_tunnel_id == tunnel_id:
                return CloudflareTunnel(tunnel_id=tunnel_id, name=self._mock_tunnel_name)
            return None

        try:
            payload = self._request_json(
                method="GET",
                path=f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}",
            )
        except CloudflareError as error_value:
            if "HTTP 404" in str(error_value):
                return None
            raise
        return _parse_tunnel(payload)

    def find_tunnel_by_name(self, account_id: str, tunnel_name: str) -> CloudflareTunnel | None:
        if self._mock_account_ok is not None:
            if self._mock_existing_tunnel_id is None:
                return None
            return CloudflareTunnel(tunnel_id=self._mock_existing_tunnel_id, name=tunnel_name)

        payload = self._request_json(
            method="GET",
            path=f"/accounts/{account_id}/cfd_tunnel",
            params={"is_deleted": "false", "per_page": "100"},
        )
        tunnels = payload.get("result")
        if not isinstance(tunnels, list):
            raise CloudflareError("Cloudflare returned an invalid tunnel list response.")
        for item in tunnels:
            if isinstance(item, dict) and item.get("name") == tunnel_name:
                return _parse_tunnel(item)
        return None

    def create_tunnel(self, account_id: str, tunnel_name: str) -> CloudflareTunnel:
        if self._mock_account_ok is not None:
            tunnel_id = self._mock_existing_tunnel_id or _mock_tunnel_id(tunnel_name)
            return CloudflareTunnel(tunnel_id=tunnel_id, name=tunnel_name)

        payload = self._request_json(
            method="POST",
            path=f"/accounts/{account_id}/cfd_tunnel",
            body={
                "config_src": "cloudflare",
                "name": tunnel_name,
                "tunnel_secret": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
            },
        )
        return _parse_tunnel(payload)

    def list_dns_records(
        self,
        zone_id: str,
        *,
        hostname: str,
        record_type: str,
        content: str | None,
    ) -> tuple[CloudflareDnsRecord, ...]:
        if self._mock_zone_ok is not None:
            if hostname.lower() not in self._mock_existing_hostnames and content is None:
                return ()
            target = content or (
                f"{self._mock_existing_tunnel_id or _mock_tunnel_id(self._mock_tunnel_name)}"
                ".cfargotunnel.com"
            )
            return (
                CloudflareDnsRecord(
                    record_id=_mock_dns_record_id(hostname),
                    name=hostname.lower(),
                    record_type=record_type,
                    content=target,
                    proxied=True,
                ),
            )

        params = {"name.exact": hostname, "type": record_type, "per_page": "100"}
        if content is not None:
            params["content.exact"] = content
        payload = self._request_json(
            method="GET",
            path=f"/zones/{zone_id}/dns_records",
            params=params,
        )
        records_payload = payload.get("result")
        if not isinstance(records_payload, list):
            raise CloudflareError("Cloudflare returned an invalid DNS list response.")
        records: list[CloudflareDnsRecord] = []
        for item in records_payload:
            if not isinstance(item, dict):
                raise CloudflareError("Cloudflare returned a malformed DNS record entry.")
            records.append(_parse_dns_record(item))
        return tuple(records)

    def create_dns_record(
        self,
        zone_id: str,
        *,
        hostname: str,
        content: str,
        proxied: bool,
    ) -> CloudflareDnsRecord:
        if self._mock_zone_ok is not None:
            return CloudflareDnsRecord(
                record_id=_mock_dns_record_id(hostname),
                name=hostname.lower(),
                record_type="CNAME",
                content=content,
                proxied=proxied,
            )

        payload = self._request_json(
            method="POST",
            path=f"/zones/{zone_id}/dns_records",
            body={
                "content": content,
                "name": hostname,
                "proxied": proxied,
                "type": "CNAME",
            },
        )
        return _parse_dns_record(payload)

    def get_access_identity_provider(
        self, account_id: str, provider_id: str
    ) -> CloudflareAccessIdentityProvider | None:
        if self._mock_account_ok is not None:
            expected_id = _mock_access_provider_id()
            if provider_id != expected_id:
                return None
            return CloudflareAccessIdentityProvider(
                provider_id=provider_id,
                name="One-time PIN login",
                provider_type="onetimepin",
            )
        try:
            payload = self._request_json(
                method="GET",
                path=f"/accounts/{account_id}/access/identity_providers/{provider_id}",
            )
        except CloudflareError as error_value:
            if "HTTP 404" in str(error_value):
                return None
            raise
        return _parse_access_identity_provider(payload)

    def find_access_identity_provider_by_name(
        self, account_id: str, name: str
    ) -> CloudflareAccessIdentityProvider | None:
        if self._mock_account_ok is not None:
            return CloudflareAccessIdentityProvider(
                provider_id=_mock_access_provider_id(),
                name=name,
                provider_type="onetimepin",
            )
        payload = self._request_json(
            method="GET",
            path=f"/accounts/{account_id}/access/identity_providers",
        )
        providers = payload.get("result")
        if not isinstance(providers, list):
            raise CloudflareError("Cloudflare returned an invalid Access identity-provider list.")
        for item in providers:
            provider = _parse_access_identity_provider(item)
            if provider.name == name and provider.provider_type == "onetimepin":
                return provider
        return None

    def create_access_identity_provider(
        self, account_id: str, name: str
    ) -> CloudflareAccessIdentityProvider:
        if self._mock_account_ok is not None:
            return CloudflareAccessIdentityProvider(
                provider_id=_mock_access_provider_id(),
                name=name,
                provider_type="onetimepin",
            )
        payload = self._request_json(
            method="POST",
            path=f"/accounts/{account_id}/access/identity_providers",
            body={"name": name, "type": "onetimepin", "config": {}},
        )
        return _parse_access_identity_provider(payload)

    def get_access_application(
        self, account_id: str, app_id: str
    ) -> CloudflareAccessApplication | None:
        if self._mock_account_ok is not None:
            domain = next(
                (host for host, rid in self._mock_access_app_ids.items() if rid == app_id), None
            )
            if domain is None:
                return None
            return CloudflareAccessApplication(
                app_id=app_id,
                name=f"{domain} protected",
                domain=domain,
                app_type="self_hosted",
                allowed_identity_provider_ids=(_mock_access_provider_id(),),
            )
        try:
            payload = self._request_json(
                method="GET",
                path=f"/accounts/{account_id}/access/apps/{app_id}",
            )
        except CloudflareError as error_value:
            if "HTTP 404" in str(error_value):
                return None
            raise
        return _parse_access_application(payload)

    def find_access_application_by_domain(
        self, account_id: str, domain: str
    ) -> CloudflareAccessApplication | None:
        if self._mock_account_ok is not None:
            app_id = self._mock_access_app_ids.get(domain)
            if app_id is None:
                return None
            return CloudflareAccessApplication(
                app_id=app_id,
                name=f"{domain} protected",
                domain=domain,
                app_type="self_hosted",
                allowed_identity_provider_ids=(_mock_access_provider_id(),),
            )
        payload = self._request_json(method="GET", path=f"/accounts/{account_id}/access/apps")
        apps = payload.get("result")
        if not isinstance(apps, list):
            raise CloudflareError("Cloudflare returned an invalid Access application list.")
        for item in apps:
            app = _parse_access_application(item)
            if app.domain == domain and app.app_type == "self_hosted":
                return app
        return None

    def create_access_application(
        self,
        account_id: str,
        *,
        name: str,
        domain: str,
        allowed_identity_provider_ids: tuple[str, ...],
    ) -> CloudflareAccessApplication:
        if self._mock_account_ok is not None:
            app_id = _mock_access_app_id(domain)
            self._mock_access_app_ids[domain] = app_id
            return CloudflareAccessApplication(
                app_id=app_id,
                name=name,
                domain=domain,
                app_type="self_hosted",
                allowed_identity_provider_ids=allowed_identity_provider_ids,
            )
        payload = self._request_json(
            method="POST",
            path=f"/accounts/{account_id}/access/apps",
            body={
                "name": name,
                "domain": domain,
                "type": "self_hosted",
                "allowed_idps": list(allowed_identity_provider_ids),
            },
        )
        return _parse_access_application(payload)

    def get_access_policy(
        self, account_id: str, app_id: str, policy_id: str
    ) -> CloudflareAccessPolicy | None:
        if self._mock_account_ok is not None:
            policy = self._mock_access_policies.get(app_id)
            if policy is None or policy.policy_id != policy_id:
                return None
            return policy
        try:
            payload = self._request_json(
                method="GET",
                path=f"/accounts/{account_id}/access/apps/{app_id}/policies/{policy_id}",
            )
        except CloudflareError as error_value:
            if "HTTP 404" in str(error_value):
                return None
            raise
        return _parse_access_policy(payload)

    def find_access_policy_by_name(
        self, account_id: str, app_id: str, name: str
    ) -> CloudflareAccessPolicy | None:
        if self._mock_account_ok is not None:
            policy = self._mock_access_policies.get(app_id)
            if policy is not None and policy.name == name:
                return policy
            return None
        payload = self._request_json(
            method="GET",
            path=f"/accounts/{account_id}/access/apps/{app_id}/policies",
        )
        policies = payload.get("result")
        if not isinstance(policies, list):
            raise CloudflareError("Cloudflare returned an invalid Access policy list.")
        for item in policies:
            policy = _parse_access_policy(item)
            if policy.name == name and policy.decision == "allow":
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
        if self._mock_account_ok is not None:
            policy = CloudflareAccessPolicy(
                policy_id=_mock_access_policy_id(app_id),
                app_id=app_id,
                name=name,
                decision="allow",
                emails=emails,
            )
            self._mock_access_policies[app_id] = policy
            return policy
        payload = self._request_json(
            method="POST",
            path=f"/accounts/{account_id}/access/apps/{app_id}/policies",
            body={
                "name": name,
                "decision": "allow",
                "include": [{"email": {"email": email}} for email in emails],
            },
        )
        return _parse_access_policy(payload)

    def _request_json(
        self,
        *,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = f"?{parse.urlencode(params)}" if params else ""
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        request_object = request.Request(
            f"{API_BASE_URL}{path}{query}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with request.urlopen(request_object) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace").strip()
            msg = f"Cloudflare API request failed with HTTP {exc.code}."
            if response_body:
                msg = f"{msg} {response_body}"
            raise CloudflareError(msg) from exc
        except error.URLError as exc:
            raise CloudflareError(f"Cloudflare API request failed: {exc.reason}") from exc

        if not isinstance(payload, dict):
            raise CloudflareError("Cloudflare API returned a non-object JSON payload.")
        if payload.get("success") is False:
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                message = "; ".join(
                    item.get("message", "unknown Cloudflare API error")
                    for item in errors
                    if isinstance(item, dict)
                )
                if message:
                    raise CloudflareError(message)
            raise CloudflareError("Cloudflare API reported an unsuccessful response.")
        return payload


def _parse_tunnel(payload: dict[str, Any]) -> CloudflareTunnel:
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        raise CloudflareError("Cloudflare returned an invalid tunnel payload.")
    tunnel_id = result.get("id")
    name = result.get("name")
    if not isinstance(tunnel_id, str) or tunnel_id == "":
        raise CloudflareError("Cloudflare tunnel payload is missing a valid id.")
    if not isinstance(name, str) or name == "":
        raise CloudflareError("Cloudflare tunnel payload is missing a valid name.")
    return CloudflareTunnel(tunnel_id=tunnel_id, name=name)


def _parse_dns_record(payload: dict[str, Any]) -> CloudflareDnsRecord:
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        raise CloudflareError("Cloudflare returned an invalid DNS record payload.")
    record_id = result.get("id")
    name = result.get("name")
    record_type = result.get("type")
    content = result.get("content")
    proxied = result.get("proxied")
    if not isinstance(record_id, str) or record_id == "":
        raise CloudflareError("Cloudflare DNS payload is missing a valid id.")
    if not isinstance(name, str) or name == "":
        raise CloudflareError("Cloudflare DNS payload is missing a valid name.")
    if not isinstance(record_type, str) or record_type == "":
        raise CloudflareError("Cloudflare DNS payload is missing a valid type.")
    if not isinstance(content, str) or content == "":
        raise CloudflareError("Cloudflare DNS payload is missing valid content.")
    if not isinstance(proxied, bool):
        raise CloudflareError("Cloudflare DNS payload is missing a valid proxied flag.")
    return CloudflareDnsRecord(
        record_id=record_id,
        name=name.lower(),
        record_type=record_type,
        content=content,
        proxied=proxied,
    )


def _parse_access_identity_provider(payload: dict[str, Any]) -> CloudflareAccessIdentityProvider:
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        raise CloudflareError("Cloudflare returned an invalid Access identity-provider payload.")
    provider_id = result.get("id")
    name = result.get("name")
    provider_type = result.get("type")
    if not isinstance(provider_id, str) or provider_id == "":
        raise CloudflareError("Cloudflare Access identity provider payload is missing a valid id.")
    if not isinstance(name, str) or name == "":
        raise CloudflareError(
            "Cloudflare Access identity provider payload is missing a valid name."
        )
    if not isinstance(provider_type, str) or provider_type == "":
        raise CloudflareError(
            "Cloudflare Access identity provider payload is missing a valid type."
        )
    return CloudflareAccessIdentityProvider(
        provider_id=provider_id,
        name=name,
        provider_type=provider_type,
    )


def _parse_access_application(payload: dict[str, Any]) -> CloudflareAccessApplication:
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        raise CloudflareError("Cloudflare returned an invalid Access application payload.")
    app_id = result.get("id")
    name = result.get("name")
    domain = result.get("domain")
    app_type = result.get("type")
    allowed_idps = result.get("allowed_idps", ())
    if not isinstance(app_id, str) or app_id == "":
        raise CloudflareError("Cloudflare Access application payload is missing a valid id.")
    if not isinstance(name, str) or name == "":
        raise CloudflareError("Cloudflare Access application payload is missing a valid name.")
    if not isinstance(domain, str) or domain == "":
        raise CloudflareError("Cloudflare Access application payload is missing a valid domain.")
    if not isinstance(app_type, str) or app_type == "":
        raise CloudflareError("Cloudflare Access application payload is missing a valid type.")
    if not isinstance(allowed_idps, list):
        allowed_idps = []
    return CloudflareAccessApplication(
        app_id=app_id,
        name=name,
        domain=domain,
        app_type=app_type,
        allowed_identity_provider_ids=tuple(
            item for item in allowed_idps if isinstance(item, str) and item != ""
        ),
    )


def _parse_access_policy(payload: dict[str, Any]) -> CloudflareAccessPolicy:
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        raise CloudflareError("Cloudflare returned an invalid Access policy payload.")
    policy_id = result.get("id")
    app_id = result.get("app_id") or result.get("appId")
    name = result.get("name")
    decision = result.get("decision")
    include = result.get("include", ())
    if not isinstance(policy_id, str) or policy_id == "":
        raise CloudflareError("Cloudflare Access policy payload is missing a valid id.")
    if not isinstance(app_id, str) or app_id == "":
        raise CloudflareError("Cloudflare Access policy payload is missing a valid app id.")
    if not isinstance(name, str) or name == "":
        raise CloudflareError("Cloudflare Access policy payload is missing a valid name.")
    if not isinstance(decision, str) or decision == "":
        raise CloudflareError("Cloudflare Access policy payload is missing a valid decision.")
    emails: list[str] = []
    if isinstance(include, list):
        for item in include:
            if not isinstance(item, dict):
                continue
            email_rule = item.get("email")
            if isinstance(email_rule, dict):
                email = email_rule.get("email")
                if isinstance(email, str) and email != "":
                    emails.append(email.lower())
    return CloudflareAccessPolicy(
        policy_id=policy_id,
        app_id=app_id,
        name=name,
        decision=decision,
        emails=tuple(sorted(set(emails))),
    )


def _optional_bool(values: dict[str, str], key: str) -> bool | None:
    raw_value = values.get(key)
    if raw_value is None:
        return None
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise StateValidationError(f"Invalid boolean value for '{key}': {raw_value!r}.")


def _require_env_value(values: dict[str, str], key: str) -> str:
    value = values.get(key)
    if value is None or value == "":
        raise StateValidationError(f"Missing required env key '{key}'.")
    return value


def _mock_dns_record_id(hostname: str) -> str:
    return hostname.lower().replace(".", "-")


def _mock_tunnel_id(tunnel_name: str) -> str:
    return tunnel_name


def _mock_access_provider_id() -> str:
    return "access-otp-provider"


def _mock_access_app_id(hostname: str) -> str:
    return f"access-app-{hostname.lower().replace('.', '-')}"


def _mock_access_policy_id(app_id: str) -> str:
    return f"access-policy-{app_id}"


def _mock_zone_id(zone_name: str) -> str:
    return f"zone-{zone_name.lower()}"
