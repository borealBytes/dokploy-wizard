from __future__ import annotations

from collections.abc import Iterable


def resolve_compose_container_name(
    service_name: str, container_names: Iterable[str]
) -> str | None:
    candidates = tuple(name.strip() for name in container_names if name.strip())
    if not candidates:
        return None
    if service_name in candidates:
        return service_name
    if len(candidates) == 1:
        return candidates[0]
    preferred = sorted(
        name
        for name in candidates
        if name.startswith(f"{service_name}.")
        or name.endswith(f"-{service_name}-1")
    )
    if preferred:
        return preferred[0]
    return sorted(candidates)[0]
