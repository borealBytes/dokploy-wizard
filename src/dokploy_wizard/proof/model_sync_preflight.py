# ruff: noqa: E501
"""Standalone Cloudflare fingerprint overlay for the remote preflight collector."""

from __future__ import annotations


def with_cloudflare_fingerprints(script: str) -> str:
    """Insert strict value-free Cloudflare collection before the script entry point."""
    marker = "def _dokploy(transport, services):"
    if script.count(marker) != 1:
        raise RuntimeError("preflight Cloudflare insertion point is invalid")
    return script.replace(marker, _CLOUDFLARE_OVERLAY + "\n" + marker)


_CLOUDFLARE_OVERLAY = r"""
import hashlib
def _cf_projection(value, keys, label):
    if not isinstance(value, dict) or any(key not in value for key in keys):
        raise RuntimeError("incomplete Cloudflare " + label)
    projection = {key: value[key] for key in keys}
    try:
        json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        raise RuntimeError("invalid Cloudflare " + label)
    return projection
def _cf_text(value, label):
    if not isinstance(value, str) or not value:
        raise RuntimeError("invalid Cloudflare " + label)
    return value
def _cf_hash(projection):
    return hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()
def _cf_resource(resource_id, name, kind, projection):
    return {"fingerprint_sha256": _cf_hash(projection), "id": _cf_text(resource_id, "resource id"), "kind": kind, "name": _cf_text(name, "resource name")}
def _cloudflare(transport):
    account, token = transport["cloudflare_account_id"], transport["cloudflare_token"]
    zone, zone_name = transport["cloudflare_zone_id"], transport["cloudflare_zone_name"]
    if not account or not token or (not zone and not zone_name):
        raise RuntimeError("missing Cloudflare credentials")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    base = "https://api.cloudflare.com/client/v4"
    if not zone:
        query = parse.urlencode({"account.id": account, "name": zone_name})
        zones = _cloudflare_pages(f"{base}/zones?{query}", headers)
        exact = [item for item in zones if isinstance(item, dict) and item.get("name") == zone_name]
        if len(exact) != 1 or not isinstance(exact[0].get("id"), str):
            raise RuntimeError("Cloudflare zone is ambiguous")
        zone = exact[0]["id"]
    resources = []
    tunnels = _cloudflare_pages(f"{base}/accounts/{account}/cfd_tunnel?is_deleted=false", headers)
    for tunnel in tunnels:
        tunnel_fields = _cf_projection(tunnel, ("id", "name", "config_src", "created_at", "deleted_at"), "tunnel")
        tunnel_id = _cf_text(tunnel_fields["id"], "tunnel id")
        config = _request_json(f"{base}/accounts/{account}/cfd_tunnel/{tunnel_id}/configurations", headers)
        result = config.get("result") if isinstance(config, dict) and config.get("success") is True else None
        config_fields = _cf_projection(result, ("config",), "tunnel configuration")
        full_config = config_fields["config"]
        if not isinstance(full_config, dict) or not isinstance(full_config.get("ingress"), list):
            raise RuntimeError("invalid Cloudflare tunnel configuration")
        resources.append(_cf_resource(tunnel_id, tunnel_fields["name"], "tunnel", {"config_src": tunnel_fields["config_src"], "created_at": tunnel_fields["created_at"], "deleted_at": tunnel_fields["deleted_at"], "ingress": full_config["ingress"], "name": tunnel_fields["name"]}))
        for route in full_config["ingress"]:
            if not isinstance(route, dict) or route.get("hostname") is None:
                continue
            route_fields = _cf_projection(route, ("hostname", "service", "originRequest"), "hostname route")
            hostname = _cf_text(route_fields["hostname"], "hostname")
            resources.append(_cf_resource(tunnel_id + ":" + hostname, hostname, "hostname_route", {"bound_tunnel_id": tunnel_id, "hostname": hostname, "originRequest": route_fields["originRequest"], "service": route_fields["service"]}))
    for record in _cloudflare_pages(f"{base}/zones/{zone}/dns_records", headers):
        fields = _cf_projection(record, ("id", "type", "name", "content", "proxied", "ttl", "comment", "tags"), "DNS record")
        resources.append(_cf_resource(fields["id"], fields["name"], "dns_record", {key: fields[key] for key in ("type", "name", "content", "proxied", "ttl", "comment", "tags")}))
    apps = _cloudflare_pages(f"{base}/accounts/{account}/access/apps", headers)
    for app in apps:
        fields = _cf_projection(app, ("id", "name", "domain", "type", "session_duration", "allowed_identity_providers", "auto_redirect_to_identity", "app_launcher_visible"), "Access application")
        app_id = _cf_text(fields["id"], "Access application id")
        resources.append(_cf_resource(app_id, fields["domain"], "access_application", {key: fields[key] for key in fields if key != "id"}))
        for policy in _cloudflare_pages(f"{base}/accounts/{account}/access/apps/{app_id}/policies", headers):
            policy_fields = _cf_projection(policy, ("id", "name", "decision", "precedence", "include", "exclude", "require"), "Access policy")
            resources.append(_cf_resource(policy_fields["id"], policy_fields["name"], "access_policy", {key: policy_fields[key] for key in policy_fields if key != "id"}))
    return resources
"""
