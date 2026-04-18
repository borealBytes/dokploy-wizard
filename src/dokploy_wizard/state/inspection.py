"""Read-only host inspection helpers for inspect-state."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from dokploy_wizard.state.models import DesiredState, OwnershipLedger

_LIVE_DRIFT_CLASSIFICATIONS = (
    "wizard_managed",
    "manual_collision",
    "host_local_route",
    "unknown_unmanaged",
)
_ROUTE_SEARCH_DIRS = (
    Path("/etc/traefik"),
    Path("/etc/traefik/dynamic"),
    Path("/opt/dokploy/traefik/dynamic"),
)
_ROUTE_FILE_PATTERNS = ("*.yaml", "*.yml", "*.toml")


def build_live_drift_report(
    *, desired_state: DesiredState, ownership_ledger: OwnershipLedger | None
) -> dict[str, Any]:
    docker_inspection = _inspect_live_docker(
        desired_state=desired_state, ownership_ledger=ownership_ledger
    )
    route_inspection = _inspect_host_route_files(desired_state)
    entries = sorted(
        [*docker_inspection["entries"], *route_inspection["entries"]],
        key=lambda item: (
            item["classification"],
            item.get("pack", ""),
            item.get("live_name", item.get("path", "")),
        ),
    )
    summary = {classification: 0 for classification in _LIVE_DRIFT_CLASSIFICATIONS}
    for entry in entries:
        summary[entry["classification"]] += 1

    detected = any(_entry_indicates_drift(entry) for entry in entries)
    status = _combine_status(
        detected=detected,
        inspection_available=(docker_inspection["available"], route_inspection["available"]),
    )
    return {
        "detected": detected,
        "entries": entries,
        "inspection": {
            "docker": {
                "available": docker_inspection["available"],
                "detail": docker_inspection["detail"],
            },
            "host_routes": {
                "available": route_inspection["available"],
                "detail": route_inspection["detail"],
            },
        },
        "status": status,
        "summary": summary,
    }


def _combine_status(*, detected: bool, inspection_available: tuple[bool, ...]) -> str:
    if detected:
        return "drift_detected"
    if not all(inspection_available):
        return "unavailable"
    return "clean"


def _entry_indicates_drift(entry: dict[str, Any]) -> bool:
    if entry["classification"] != "wizard_managed":
        return True
    return entry.get("health") != "healthy"


def _inspect_live_docker(
    *, desired_state: DesiredState, ownership_ledger: OwnershipLedger | None
) -> dict[str, Any]:
    if not _docker_cli_available():
        return {
            "available": False,
            "detail": "Docker CLI is not available; skipped live Docker inspection.",
            "entries": [],
        }

    services = _list_docker_services()
    containers = _list_docker_containers()
    if services is None or containers is None:
        return {
            "available": False,
            "detail": "Docker CLI could not inspect live services or containers.",
            "entries": [],
        }

    managed_scopes = (
        {resource.scope for resource in ownership_ledger.resources}
        if ownership_ledger is not None
        else set()
    )
    candidates = _service_candidates(desired_state)
    entries: list[dict[str, Any]] = []
    consumed_live_items: set[tuple[str, str]] = set()
    managed_container_candidates = _managed_container_candidates(
        containers=containers,
        candidates=candidates,
    )

    for candidate in candidates:
        scope_is_managed = candidate["scope"] in managed_scopes
        if scope_is_managed:
            health, detail, live_kind, live_name = _managed_service_health(
                candidate=candidate,
                expected_service_name=candidate["expected_service_name"],
                services=services,
                containers=containers,
                managed_container_candidates=managed_container_candidates,
            )
            entries.append(
                {
                    "classification": "wizard_managed",
                    "detail": detail,
                    "expected_service_name": candidate["expected_service_name"],
                    "health": health,
                    "live_kind": live_kind,
                    "live_name": live_name,
                    "managed": True,
                    "pack": candidate["pack"],
                    "scope": candidate["scope"],
                }
            )
            consumed_live_items.add((live_kind, live_name))

        for service_name in services:
            if not _matches_candidate(service_name, candidate):
                continue
            if service_name == candidate["expected_service_name"] and scope_is_managed:
                continue
            consumed_live_items.add(("service", service_name))
            entries.append(
                _build_unmanaged_entry(
                    classification="manual_collision",
                    candidate=candidate,
                    detail=(
                        f"Live Docker service '{service_name}' matches "
                        f"the requested {candidate['pack']} "
                        "runtime but is not tracked by the current wizard ownership ledger."
                    ),
                    live_kind="service",
                    live_name=service_name,
                )
            )

        for container in containers:
            container_name = container["name"]
            managed_candidate_pack = managed_container_candidates.get(container_name)
            if managed_candidate_pack is not None:
                if managed_candidate_pack == candidate["pack"] and scope_is_managed:
                    consumed_live_items.add(("container", container_name))
                continue
            if _looks_like_managed_task(
                container_name=container_name,
                expected_service_name=candidate["expected_service_name"],
            ):
                continue
            if not _matches_candidate(container_name, candidate):
                continue
            consumed_live_items.add(("container", container_name))
            entries.append(
                _build_unmanaged_entry(
                    classification="manual_collision",
                    candidate=candidate,
                    detail=(
                        f"Live Docker container '{container_name}' matches "
                        f"the requested {candidate['pack']} "
                        "runtime but is not tracked by the current wizard ownership ledger."
                    ),
                    live_kind="container",
                    live_name=container_name,
                    status=container["status"],
                )
            )

    for live_kind, live_name, status in _unknown_unmanaged_live_items(
        desired_state=desired_state,
        services=services,
        containers=containers,
        consumed_live_items=consumed_live_items,
        expected_service_names={candidate["expected_service_name"] for candidate in candidates},
    ):
        entries.append(
            {
                "classification": "unknown_unmanaged",
                "detail": (
                    f"Live Docker {live_kind} '{live_name}' references "
                    f"stack '{desired_state.stack_name}' "
                    "but does not match a known wizard-managed runtime name."
                ),
                "live_kind": live_kind,
                "live_name": live_name,
                "managed": False,
                "pack": None,
                "scope": None,
                **({"status": status} if status is not None else {}),
            }
        )

    return {
        "available": True,
        "detail": "Inspected live Docker services and containers for managed runtime drift.",
        "entries": entries,
    }


def _managed_service_health(
    *,
    candidate: dict[str, Any],
    expected_service_name: str,
    services: tuple[str, ...],
    containers: tuple[dict[str, Any], ...],
    managed_container_candidates: dict[str, str],
) -> tuple[str, str, str, str]:
    if expected_service_name not in services:
        container_matches = [
            container
            for container in containers
            if managed_container_candidates.get(container["name"]) == candidate["pack"]
        ]
        if len(container_matches) == 1:
            container = container_matches[0]
            status = container["status"]
            return (
                "unhealthy" if _status_is_unhealthy(status) else "healthy",
                (
                    f"Wizard-managed Docker container '{container['name']}' is present "
                    f"with label-backed ownership evidence for {candidate['pack']}."
                ),
                "container",
                container["name"],
            )
        return (
            "missing",
            f"Expected wizard-managed Docker service '{expected_service_name}' is missing.",
            "service",
            expected_service_name,
        )
    task_statuses = _list_service_task_statuses(expected_service_name)
    if task_statuses is None:
        return (
            "unknown",
            f"Wizard-managed Docker service '{expected_service_name}' exists, "
            "but task health could not be inspected.",
            "service",
            expected_service_name,
        )
    if not task_statuses:
        return (
            "unhealthy",
            f"Wizard-managed Docker service '{expected_service_name}' has "
            "no running task containers.",
            "service",
            expected_service_name,
        )
    if any(_status_is_unhealthy(status) for status in task_statuses):
        return (
            "unhealthy",
            f"Wizard-managed Docker service '{expected_service_name}' has "
            f"unhealthy task containers: {list(task_statuses)}.",
            "service",
            expected_service_name,
        )
    return (
        "healthy",
        f"Wizard-managed Docker service '{expected_service_name}' is present "
        "with healthy task containers.",
        "service",
        expected_service_name,
    )


def _build_unmanaged_entry(
    *,
    classification: str,
    candidate: dict[str, Any],
    detail: str,
    live_kind: str,
    live_name: str,
    status: str | None = None,
) -> dict[str, Any]:
    entry = {
        "classification": classification,
        "detail": detail,
        "expected_service_name": candidate["expected_service_name"],
        "live_kind": live_kind,
        "live_name": live_name,
        "managed": False,
        "pack": candidate["pack"],
        "scope": candidate["scope"],
    }
    if status is not None:
        entry["status"] = status
    return entry


def _service_candidates(desired_state: DesiredState) -> tuple[dict[str, Any], ...]:
    candidates: list[dict[str, Any]] = []
    if "openclaw" in desired_state.enabled_packs:
        candidates.append(
            {
                "aliases": ("openclaw", "advisor"),
                "hostname": desired_state.hostnames.get("openclaw"),
                "managed_container_labels": {
                    "dokploy-wizard.slot": "openclaw_suite",
                    "dokploy-wizard.variant": "openclaw",
                },
                "port": "18789",
                "expected_service_name": f"{desired_state.stack_name}-openclaw",
                "pack": "openclaw",
                "scope": f"stack:{desired_state.stack_name}:openclaw",
            }
        )
    if "my-farm-advisor" in desired_state.enabled_packs:
        candidates.append(
            {
                "aliases": ("my-farm-advisor", "my-farm", "farm-advisor"),
                "hostname": desired_state.hostnames.get("my-farm-advisor"),
                "managed_container_labels": {
                    "dokploy-wizard.slot": "my-farm-advisor_suite",
                    "dokploy-wizard.variant": "my-farm-advisor",
                },
                "port": "18789",
                "expected_service_name": f"{desired_state.stack_name}-my-farm-advisor",
                "pack": "my-farm-advisor",
                "scope": f"stack:{desired_state.stack_name}:my-farm-advisor",
            }
        )
    if "nextcloud" in desired_state.enabled_packs:
        candidates.append(
            {
                "aliases": ("onlyoffice",),
                "hostname": desired_state.hostnames.get("onlyoffice"),
                "managed_container_labels": {
                    "com.docker.compose.service": f"{desired_state.stack_name}-onlyoffice"
                },
                "port": "80",
                "expected_service_name": f"{desired_state.stack_name}-onlyoffice",
                "pack": "onlyoffice",
                "scope": f"stack:{desired_state.stack_name}:onlyoffice-service",
            }
        )
    return tuple(candidates)


def _matches_candidate(live_name: str, candidate: dict[str, Any]) -> bool:
    normalized_name = live_name.lower()
    if normalized_name == candidate["expected_service_name"].lower():
        return True
    return any(alias in normalized_name for alias in candidate["aliases"])


def _unknown_unmanaged_live_items(
    *,
    desired_state: DesiredState,
    services: tuple[str, ...],
    containers: tuple[dict[str, str], ...],
    consumed_live_items: set[tuple[str, str]],
    expected_service_names: set[str],
) -> tuple[tuple[str, str, str | None], ...]:
    prefix = desired_state.stack_name.lower()
    unknown: list[tuple[str, str, str | None]] = []
    for service_name in services:
        if ("service", service_name) in consumed_live_items:
            continue
        if service_name in expected_service_names:
            continue
        if prefix not in service_name.lower():
            continue
        unknown.append(("service", service_name, None))
    for container in containers:
        container_name = container["name"]
        if ("container", container_name) in consumed_live_items:
            continue
        if any(
            _looks_like_managed_task(container_name=container_name, expected_service_name=name)
            for name in expected_service_names
        ):
            continue
        if prefix not in container_name.lower():
            continue
        unknown.append(("container", container_name, container["status"]))
    return tuple(sorted(unknown, key=lambda item: (item[0], item[1])))


def _looks_like_managed_task(*, container_name: str, expected_service_name: str) -> bool:
    normalized_expected = expected_service_name.lower()
    normalized_name = container_name.lower()
    return normalized_name.startswith(normalized_expected + ".")


def _managed_container_candidates(
    *, containers: tuple[dict[str, Any], ...], candidates: tuple[dict[str, Any], ...]
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for container in containers:
        matches = [
            candidate["pack"]
            for candidate in candidates
            if _container_proves_managed_candidate(container=container, candidate=candidate)
        ]
        if len(matches) == 1:
            resolved[container["name"]] = matches[0]
    return resolved


def _container_proves_managed_candidate(
    *, container: dict[str, Any], candidate: dict[str, Any]
) -> bool:
    labels = container.get("labels")
    if not isinstance(labels, dict) or not labels:
        return False
    hostname = candidate.get("hostname")
    port = candidate.get("port")
    managed_labels = candidate.get("managed_container_labels")
    if hostname is None or port is None or not isinstance(managed_labels, dict):
        return False
    if not all(labels.get(key) == value for key, value in managed_labels.items()):
        return False
    return _labels_reference_hostname(labels, hostname) and _labels_expose_port(labels, port)


def _labels_reference_hostname(labels: dict[str, str], hostname: str) -> bool:
    return any(hostname in value for value in labels.values())


def _labels_expose_port(labels: dict[str, str], port: str) -> bool:
    return any(value == port for value in labels.values())


def _list_docker_services() -> tuple[str, ...] | None:
    result = _run_docker_command(["docker", "service", "ls", "--format", "{{.Name}}"])
    if result is None or result.returncode != 0:
        return None
    return tuple(sorted(line.strip() for line in result.stdout.splitlines() if line.strip()))


def _list_docker_containers() -> tuple[dict[str, Any], ...] | None:
    result = _run_docker_command(
        ["docker", "ps", "-a", "--format", "{{.Names}}|{{.Status}}|{{.ID}}"]
    )
    if result is None or result.returncode != 0:
        return None
    containers: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        name, status, container_id = parts
        if not name.strip():
            continue
        containers.append(
            {
                "name": name.strip(),
                "status": status.strip(),
                "labels": _inspect_container_labels(container_id.strip()),
            }
        )
    return tuple(sorted(containers, key=lambda item: item["name"]))


def _inspect_container_labels(container_id: str) -> dict[str, str]:
    if not container_id:
        return {}
    result = _run_docker_command(
        ["docker", "inspect", "--format", "{{json .Config.Labels}}", container_id]
    )
    if result is None or result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _list_service_task_statuses(service_name: str) -> tuple[str, ...] | None:
    result = _run_docker_command(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.swarm.service.name={service_name}",
            "--format",
            "{{.Status}}",
        ]
    )
    if result is None or result.returncode != 0:
        return None
    return tuple(status.strip() for status in result.stdout.splitlines() if status.strip())


def _run_docker_command(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError:
        return None


def _status_is_unhealthy(status: str) -> bool:
    normalized = status.lower()
    return any(token in normalized for token in ("exited", "dead", "restart", "unhealthy"))


def _docker_cli_available() -> bool:
    return shutil.which("docker") is not None


def _inspect_host_route_files(desired_state: DesiredState) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for search_dir in _ROUTE_SEARCH_DIRS:
        if not search_dir.exists():
            continue
        try:
            for path in _iter_route_files(search_dir):
                matched = _match_route_file(path, desired_state)
                if matched is None:
                    continue
                normalized_path = str(path)
                if normalized_path in seen_paths:
                    continue
                seen_paths.add(normalized_path)
                entries.append(
                    {
                        "classification": "host_local_route",
                        "detail": (
                            f"Host-local route file '{normalized_path}' references the requested "
                            f"{matched['pack']} hostname and may shadow Dokploy-managed ingress."
                        ),
                        "hostname": matched["hostname"],
                        "pack": matched["pack"],
                        "path": normalized_path,
                    }
                )
        except OSError as error:
            return {
                "available": False,
                "detail": f"Route-file inspection could not read '{search_dir}': {error}.",
                "entries": [],
            }

    return {
        "available": True,
        "detail": "Inspected host-local route files for ingress shadowing.",
        "entries": entries,
    }


def _iter_route_files(search_dir: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for pattern in _ROUTE_FILE_PATTERNS:
        paths.extend(path for path in search_dir.rglob(pattern) if path.is_file())
    return tuple(sorted(set(paths)))


def _match_route_file(path: Path, desired_state: DesiredState) -> dict[str, str] | None:
    contents = path.read_text(encoding="utf-8", errors="ignore").lower()
    for candidate in _route_file_candidates(desired_state):
        if candidate["hostname"].lower() in contents:
            return candidate
        if candidate["token"] in path.name.lower():
            return candidate
    return None


def _route_file_candidates(desired_state: DesiredState) -> tuple[dict[str, str], ...]:
    candidates: list[dict[str, str]] = []
    onlyoffice_hostname = desired_state.hostnames.get("onlyoffice")
    if onlyoffice_hostname is not None:
        candidates.append(
            {"hostname": onlyoffice_hostname, "pack": "onlyoffice", "token": "onlyoffice"}
        )
    my_farm_hostname = desired_state.hostnames.get("my-farm-advisor")
    if my_farm_hostname is not None:
        candidates.append(
            {
                "hostname": my_farm_hostname,
                "pack": "my-farm-advisor",
                "token": "my-farm-advisor",
            }
        )
    return tuple(candidates)
