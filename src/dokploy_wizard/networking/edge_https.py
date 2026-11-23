"""Traefik HTTPS-only edge for wizard-owned hostnames.

The Cloudflare Tunnel ingress the wizard publishes terminates at this box on
``https://localhost:443`` with ``noTLSVerify`` — visitor traffic arrives as TLS
regardless of whether the visitor asked for ``http://`` or ``https://``. The
only signal of the visitor's scheme is the ``X-Forwarded-Proto`` header that
cloudflared writes. The Cloudflare zone's ``Always Use HTTPS`` setting would
also work, but it is zone-wide state the wizard should not own: it would also
silence plain HTTP for Coder workspace wildcard routes, where serving HTTP is
a legitimate choice.

So the redirect is enforced locally, in two halves:

1. Traefik must be told to *trust* forwarded headers, otherwise it overwrites
   ``X-Forwarded-Proto`` with the real TLS scheme before any rule can see the
   visitor's scheme. Dokploy's static config is patched idempotently with
   ``entryPoints.websecure.forwardedHeaders.trustedIPs``. Because swarm
   publishes :443 through the userland proxy, Traefik sees connections from
   Docker's internal gateway addresses, so the trusted set is loopback plus the
   RFC1918 ranges; clients that bypass the tunnel and hit :443 directly are
   public and stay untrusted (their only lever would be skipping the redirect).
2. A file-provider drop-in (``dokploy-wizard-edge-https.yml``) rendered into
   the directory Dokploy already watches adds one high-priority router per
   wizard hostname matching ``Header(``X-Forwarded-Proto``, ``http``)`` — the
   Traefik v3 rule name; v2's ``HeadersRegexp`` was removed and v3 fails such
   rules open — attached to a permanent ``redirectScheme`` middleware and a
   discard service (the middleware short-circuits before the service is
   dialed). Trust makes the request's scheme ``http``, so ``redirectScheme``
   fires and Traefik builds the Location itself from the real host and path.

Coder workspace wildcard hostnames are deliberately not listed, so a workspace
may still choose to serve plain HTTP.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

EDGE_HTTPS_FILENAME = "dokploy-wizard-edge-https.yml"
TRAEFIK_STATIC_FILENAME = "traefik.yml"
TRAEFIK_DYNAMIC_SUBDIR = "dynamic"
TRAEFIK_DIR = Path("/etc/dokploy/traefik")
TRAEFIK_DYNAMIC_DIR = TRAEFIK_DIR / TRAEFIK_DYNAMIC_SUBDIR
TRAEFIK_STATIC_FILE = TRAEFIK_DIR / TRAEFIK_STATIC_FILENAME
TRAEFIK_SERVICE_NAME = "dokploy-traefik"

# cloudflared (host network) reaches :443 through swarm's userland proxy, so
# the peer Traefik sees is a Docker gateway address, not the visitor. Trust
# loopback + the RFC1918 ranges; public clients that bypass the tunnel stay
# untrusted and can only ever skip the redirect, never fake an https origin.
TRUSTED_FORWARDED_IPS: tuple[str, ...] = (
    "127.0.0.1/32",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)

_EDGE_ROUTER_PREFIX = "wizard-edge-https-"
_EDGE_REDIRECT_MIDDLEWARE = "wizard-redirect-https"
_EDGE_DISCARD_SERVICE = "wizard-edge-discard"
_EDGE_ROUTER_PRIORITY = 1000
_DISCARD_SERVICE_URL = "http://127.0.0.1:1"

_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME_PATTERN = re.compile(rf"^{_DNS_LABEL}(?:\.{_DNS_LABEL})*$")


def _router_name(hostname: str) -> str:
    return _EDGE_ROUTER_PREFIX + hostname.replace(".", "-")


def build_edge_https_config(hostnames: Iterable[str]) -> str:
    """Render the Traefik file-provider drop-in for ``hostnames``.

    Hostnames are sorted and deduplicated so the rendering is byte-stable
    across passes; an unchanged desired state must not churn the file.
    """
    cleaned: list[str] = []
    for raw in hostnames:
        hostname = raw.strip().lower()
        if hostname == "":
            continue
        if hostname.startswith("*."):
            msg = (
                "Wildcard hostnames must not appear in the HTTPS-edge drop-in; "
                f"plain HTTP stays available for '{hostname}' by design. Got: {raw!r}"
            )
            raise ValueError(msg)
        if not _HOSTNAME_PATTERN.match(hostname):
            msg = f"Invalid hostname for the HTTPS-edge drop-in: {raw!r}"
            raise ValueError(msg)
        if hostname not in cleaned:
            cleaned.append(hostname)
    cleaned.sort()

    lines = [
        "# Managed by dokploy-wizard — do not edit by hand.",
        "# Plain HTTP must never serve a wizard login surface: every request",
        "# cloudflared tagged X-Forwarded-Proto: http is 301-redirected to the",
        "# HTTPS URL at the Traefik edge (v3 rule name: Header; the v2",
        "# HeadersRegexp was removed and fails open). Coder workspace wildcard",
        "# hosts are intentionally absent so a workspace can still serve plain",
        "# HTTP. Requires forwardedHeaders.trustedIPs in the static config,",
        "# which the wizard patches (see ensure_trusted_forwarded_headers).",
        "http:",
        "  routers:",
    ]
    for hostname in cleaned:
        lines.extend(
            [
                f"    {_router_name(hostname)}:",
                f"      rule: \"Host(`{hostname}`) && "
                "Header(`X-Forwarded-Proto`, `http`)\"",
                "      entryPoints:",
                "        - web",
                "        - websecure",
                f"      priority: {_EDGE_ROUTER_PRIORITY}",
                f"      service: {_EDGE_DISCARD_SERVICE}",
                "      middlewares:",
                f"        - {_EDGE_REDIRECT_MIDDLEWARE}",
            ]
        )
    lines.extend(
        [
            "  middlewares:",
            f"    {_EDGE_REDIRECT_MIDDLEWARE}:",
            # redirectScheme works because the static trustedIPs patch makes",
            # the request's scheme follow the trusted X-Forwarded-Proto header.",
            "      redirectScheme:",
            "        scheme: https",
            "        permanent: true",
            "  services:",
            f"    {_EDGE_DISCARD_SERVICE}:",
            "      loadBalancer:",
            "        servers:",
            f'          - url: "{_DISCARD_SERVICE_URL}"',
        ]
    )
    return "\n".join(lines) + "\n"


def _trusted_block_lines(indent: str) -> list[str]:
    lines = [f"{indent}forwardedHeaders:", f"{indent}  trustedIPs:"]
    lines.extend(f"{indent}    - {cidr}" for cidr in TRUSTED_FORWARDED_IPS)
    return lines


def ensure_trusted_forwarded_headers(text: str) -> tuple[str, bool]:
    """Return (patched_text, changed) for Dokploy's static Traefik config.

    Inserts (or replaces) a ``forwardedHeaders.trustedIPs`` block inside the
    ``websecure`` entrypoint so Traefik trusts forwarded headers coming from
    the tunnel's proxy path. Idempotent: the exact desired block is detected
    and left alone, so repeated passes report ``changed=False``.
    """
    lines = text.splitlines()
    websecure_idx: int | None = None
    for i, line in enumerate(lines):
        if line == "  websecure:":
            websecure_idx = i
            break
    if websecure_idx is None:
        msg = "static Traefik config has no '  websecure:' entrypoint block"
        raise ValueError(msg)

    # Scope = everything after websecure until the next entry at indent <= 2.
    end = len(lines)
    for i in range(websecure_idx + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            continue
        if not line.startswith("    "):
            end = i
            break

    existing_start: int | None = None
    existing_end = end
    for i in range(websecure_idx + 1, end):
        if lines[i] == "    forwardedHeaders:":
            existing_start = i
            existing_end = end
            break
    if existing_start is not None:
        for i in range(existing_start + 1, end):
            line = lines[i]
            if line.strip() != "" and not line.startswith("      "):
                existing_end = i
                break

    desired = _trusted_block_lines("    ")
    if existing_start is not None:
        current = lines[existing_start:existing_end]
        if current == desired:
            return text, False
        replaced = lines[:existing_start] + desired + lines[existing_end:]
        return "\n".join(replaced) + "\n", True

    # Insert directly under the entrypoint's address line when present,
    # otherwise straight below 'websecure:'.
    insert_at = websecure_idx + 1
    for i in range(websecure_idx + 1, end):
        if lines[i].strip().startswith("address:"):
            insert_at = i + 1
            break
    patched = lines[:insert_at] + desired + lines[insert_at:]
    return "\n".join(patched) + "\n", True


def _default_docker_restart(service: str) -> bool:
    """Force-restart the Traefik service (static config needs a restart)."""
    service_update = subprocess.run(
        ["docker", "service", "update", "--force", service],
        check=False,
        capture_output=True,
        text=True,
    )
    if service_update.returncode == 0:
        return True
    container_restart = subprocess.run(
        ["docker", "restart", service],
        check=False,
        capture_output=True,
        text=True,
    )
    return container_restart.returncode == 0


def apply_static_trust(
    *,
    static_file: Path,
    dry_run: bool,
    restart: Callable[[str], bool] | None = None,
    service: str = TRAEFIK_SERVICE_NAME,
) -> tuple[str, ...]:
    """Patch (or plan) forwardedHeaders trust into the static Traefik config."""
    if restart is None:
        restart = _default_docker_restart
    if not static_file.is_file():
        return (
            f"WARNING: Traefik static config {static_file} is missing; forwarded-header "
            "trust and the HTTPS-edge redirect were not applied. Wizard logins stay "
            "reachable over plain HTTP until the next run once Dokploy's Traefik exists.",
        )
    current = static_file.read_text(encoding="utf-8")
    try:
        patched, changed = ensure_trusted_forwarded_headers(current)
    except ValueError as exc:
        return (
            f"WARNING: could not patch forwarded-header trust into {static_file}: {exc}. "
            "Plain HTTP keeps reaching wizard logins until this is fixed.",
        )
    if not changed:
        return (f"Traefik static config {static_file} already trusts forwarded headers.",)
    if dry_run:
        return (
            f"Would add forwardedHeaders.trustedIPs for the websecure entrypoint to "
            f"{static_file} and restart {service}.",
        )
    static_file.write_text(patched, encoding="utf-8")
    if not restart(service):
        return (
            f"WARNING: patched {static_file} but restarting {service} failed; plain HTTP "
            "keeps reaching wizard logins until Traefik reloads. Re-run the wizard once "
            "the Traefik service is healthy.",
        )
    return (
        f"Patched {static_file} to trust tunnel-forwarded headers and restarted {service}; "
        "the HTTPS-edge redirect can now see the visitor scheme.",
    )


def apply_edge_https_config(
    hostnames: Iterable[str],
    *,
    traefik_dir: Path,
    dry_run: bool,
    restart: Callable[[str], bool] | None = None,
) -> tuple[str, ...]:
    """Write (or plan) both halves of the HTTPS edge: trust + drop-in.

    ``traefik_dir`` is the base Traefik directory (static config plus the
    watched dynamic dir). Degrades to warnings when the files are missing:
    the edge is hardening owned by the Traefik install, and a bootstrap that
    has not run yet (or a mock environment) must not fail the networking
    phase.
    """
    planned = build_edge_https_config(hostnames)
    count = planned.count("\n    wizard-edge-https-")
    static_file = traefik_dir / TRAEFIK_STATIC_FILENAME
    directory = traefik_dir / TRAEFIK_DYNAMIC_SUBDIR
    if not static_file.is_file() and not directory.is_dir():
        return (
            f"WARNING: no Traefik install found at {traefik_dir}; the HTTPS-edge redirect "
            "was not applied. Wizard logins stay reachable over plain HTTP until the next "
            "run once Dokploy's Traefik is installed.",
        )

    notes = list(
        apply_static_trust(static_file=static_file, dry_run=dry_run, restart=restart)
    )

    route_file = directory / EDGE_HTTPS_FILENAME
    if dry_run:
        notes.append(
            f"Would write the Traefik HTTPS-edge redirect drop-in for {count} hostname(s) "
            f"to {route_file}; plain HTTP redirects to HTTPS on every wizard hostname.",
        )
        return tuple(notes)
    if not directory.is_dir():
        notes.append(
            f"WARNING: Traefik dynamic directory {directory} is missing; the HTTPS-edge "
            "redirect drop-in was not written. Wizard logins stay reachable over plain "
            "HTTP until the next run once Dokploy's Traefik is installed.",
        )
        return tuple(notes)
    existing: str | None = None
    if route_file.is_file():
        existing = route_file.read_text(encoding="utf-8")
    if existing == planned:
        notes.append(
            f"Traefik HTTPS-edge redirect drop-in at {route_file} is up to date "
            f"({count} hostname(s)).",
        )
        return tuple(notes)
    route_file.write_text(planned, encoding="utf-8")
    action = "Updated" if existing is not None else "Wrote"
    notes.append(
        f"{action} the Traefik HTTPS-edge redirect drop-in at {route_file} "
        f"({count} hostname(s)); plain HTTP now 301-redirects to HTTPS on every "
        "wizard hostname while Coder workspace routes keep plain HTTP.",
    )
    return tuple(notes)
