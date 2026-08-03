from __future__ import annotations

from urllib.parse import urlsplit

from dokploy_wizard.dokploy.workspace_catalog_sync_models import WorkspaceCatalogSyncError


def require_sha256(value: str, label: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise WorkspaceCatalogSyncError(f"{label} is invalid")
    return value


def require_internal_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise WorkspaceCatalogSyncError("workspace catalog base URL is invalid") from error
    host = parsed.hostname
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or host is None
        or port != 4000
        or parsed.netloc != f"{host}:4000"
        or parsed.path != "/v1"
        or parsed.query
        or parsed.fragment
        or not host.endswith("-shared-litellm")
    ):
        raise WorkspaceCatalogSyncError("workspace catalog base URL is not internal LiteLLM")
    stack = host.removesuffix("-shared-litellm")
    if (
        not stack
        or stack.startswith("-")
        or stack.endswith("-")
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in stack)
    ):
        raise WorkspaceCatalogSyncError("workspace catalog base URL is not internal LiteLLM")
    return value
