"""Canonical names for Dokploy API resources."""

from __future__ import annotations

import hashlib
import re
from typing import Final

DOKPLOY_COMPOSE_APP_NAME_INPUT_LIMIT: Final = 56
_HASH_HEX_LENGTH: Final = 24
_READABLE_PREFIX_LENGTH: Final = DOKPLOY_COMPOSE_APP_NAME_INPUT_LIMIT - _HASH_HEX_LENGTH - 1
_CANONICAL_APP_NAME: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class InvalidDokployAppNameError(ValueError):
    """Raised when a compose app name is outside the wizard's canonical alphabet."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            "Dokploy app names must be non-empty lowercase alphanumeric segments "
            "separated by single hyphens."
        )


def canonical_compose_app_name(name: str) -> str:
    """Return a stable appName that leaves room for Dokploy's generated suffix."""
    if _CANONICAL_APP_NAME.fullmatch(name) is None:
        raise InvalidDokployAppNameError(name)
    if len(name) <= DOKPLOY_COMPOSE_APP_NAME_INPUT_LIMIT:
        return name
    digest = hashlib.sha256(name.encode("ascii")).hexdigest()[:_HASH_HEX_LENGTH]
    return f"{name[:_READABLE_PREFIX_LENGTH]}-{digest}"
