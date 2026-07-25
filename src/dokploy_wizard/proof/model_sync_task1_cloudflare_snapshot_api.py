"""Cloudflare complete-list API adapter used only by Task 1 snapshot capture."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypeVar
from urllib import error, parse, request

from dokploy_wizard.networking.cloudflare import (
    API_BASE_URL,
    CloudflareAccessApplication,
    CloudflareAccessIdentityProvider,
    CloudflareAccessPolicy,
    CloudflareCertificatePack,
    CloudflareDnsRecord,
    CloudflareError,
    CloudflareTunnel,
    _parse_access_application,
    _parse_access_identity_provider,
    _parse_access_policy,
    _parse_certificate_pack,
    _parse_dns_record,
    _parse_tunnel,
)
from dokploy_wizard.proof.model_sync_task1_cloudflare_snapshot_backend import (
    CloudflareSnapshotPage,
)
from dokploy_wizard.state import RawEnvInput

T = TypeVar("T")
_REQUEST_TIMEOUT_SECONDS = 15
_MAX_RESPONSE_BYTES = 1024 * 1024


class CloudflareSnapshotApiBackend:
    """Account/zone inventory adapter that rejects incomplete result_info envelopes."""

    def __init__(self, raw_env: RawEnvInput) -> None:
        token = raw_env.values.get("CLOUDFLARE_API_TOKEN", "")
        if token == "":
            raise CloudflareError("Cloudflare snapshot requires an API token")
        self._token = token

    def list_tunnels_page(
        self, account_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareTunnel]:
        return self._list(
            f"/accounts/{account_id}/cfd_tunnel",
            {"is_deleted": "false"},
            _parse_tunnel,
            page,
            per_page,
        )

    def get_tunnel_configuration(
        self, account_id: str, tunnel_id: str
    ) -> tuple[dict[str, Any], ...]:
        payload = self._request(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations", {})
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("config"), dict):
            raise CloudflareError("Cloudflare snapshot tunnel configuration is invalid")
        ingress = result["config"].get("ingress")
        if not isinstance(ingress, list) or not all(isinstance(item, dict) for item in ingress):
            raise CloudflareError("Cloudflare snapshot tunnel configuration is invalid")
        return tuple(ingress)

    def list_dns_records_page(
        self, zone_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareDnsRecord]:
        return self._list(f"/zones/{zone_id}/dns_records", {}, _parse_dns_record, page, per_page)

    def list_identity_providers_page(
        self, account_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareAccessIdentityProvider]:
        return self._list(
            f"/accounts/{account_id}/access/identity_providers",
            {},
            _parse_access_identity_provider,
            page,
            per_page,
        )

    def list_access_applications_page(
        self, account_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareAccessApplication]:
        return self._list(
            f"/accounts/{account_id}/access/apps", {}, _parse_access_application, page, per_page
        )

    def list_access_policies_page(
        self, account_id: str, app_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareAccessPolicy]:
        return self._list(
            f"/accounts/{account_id}/access/apps/{app_id}/policies",
            {},
            lambda value: _parse_access_policy(value, app_id=app_id),
            page,
            per_page,
        )

    def list_certificate_packs_page(
        self, zone_id: str, page: int, per_page: int
    ) -> CloudflareSnapshotPage[CloudflareCertificatePack]:
        return self._list(
            f"/zones/{zone_id}/ssl/certificate_packs",
            {"status": "all"},
            _parse_certificate_pack,
            page,
            per_page,
        )

    def _list(
        self,
        path: str,
        filters: dict[str, str],
        parser: Callable[[dict[str, Any]], T],
        page: int,
        per_page: int,
    ) -> CloudflareSnapshotPage[T]:
        payload = self._request(path, {**filters, "page": str(page), "per_page": str(per_page)})
        result = payload.get("result")
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise CloudflareError("Cloudflare snapshot list result is invalid")
        page_info = payload.get("result_info")
        if not isinstance(page_info, dict):
            raise CloudflareError("Cloudflare snapshot result_info is missing")
        page_value = _page_number(page_info.get("page"))
        per_page_value = _page_number(page_info.get("per_page"))
        total_count = _page_number(page_info.get("total_count"))
        if "total_pages" in page_info:
            total_pages = _page_number(page_info["total_pages"])
        else:
            if per_page_value < 1:
                raise CloudflareError("Cloudflare snapshot result_info is invalid")
            total_pages = max(1, (total_count + per_page_value - 1) // per_page_value)
        return CloudflareSnapshotPage(
            page_value,
            per_page_value,
            total_count,
            total_pages,
            tuple(parser(item) for item in result),
        )

    def _request(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        query = parse.urlencode(params)
        request_object = request.Request(
            f"{API_BASE_URL}{path}?{query}",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            method="GET",
        )
        try:
            with request.urlopen(request_object, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                content = response.read(_MAX_RESPONSE_BYTES + 1)
                declared_length = response.headers.get("Content-Length")
                if len(content) > _MAX_RESPONSE_BYTES or _content_length_is_invalid(
                    declared_length, len(content)
                ):
                    raise CloudflareError("Cloudflare snapshot response is incomplete or oversized")
                payload = json.loads(content.decode("utf-8"))
        except error.HTTPError as exc:
            raise CloudflareError(
                f"Cloudflare snapshot request failed with HTTP {exc.code}"
            ) from None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise CloudflareError("Cloudflare snapshot request failed") from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise CloudflareError("Cloudflare snapshot request was unsuccessful")
        return payload


def _page_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CloudflareError("Cloudflare snapshot result_info is invalid")
    return int(value)


def _content_length_is_invalid(declared_length: str | None, actual_length: int) -> bool:
    if declared_length is None:
        return False
    return (
        not declared_length.isascii()
        or not declared_length.isdecimal()
        or int(declared_length) != actual_length
    )
