"""Receipt-bound authority and deletion checkpoints for uninstall mutations."""

from __future__ import annotations

from pathlib import Path

from dokploy_wizard.state.models import OwnedResource
from dokploy_wizard.state.uninstall_authority_schema import (
    AuthorityDocument,
    UninstallAuthority,
    UninstallAuthorityError,
    UninstallDeletionReceipt,
    empty_document,
    find_authority,
    find_deletion,
    parse_document,
    resource_key,
    sorted_authorities,
    sorted_deletions,
)
from dokploy_wizard.state.upgrade_io import atomic_json, read_json

_AUTHORITY_FILE = "uninstall-authority.json"


class UninstallAuthorityStore:
    """Persists exact targets that may be safely removed during uninstall."""

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / _AUTHORITY_FILE

    def record_created(
        self,
        *,
        resource: OwnedResource,
        owner_id: str,
        provider: str,
        physical_target_id: str,
        parent_target_id: str,
        expected_fingerprint: str,
    ) -> UninstallAuthority:
        authority = UninstallAuthority(
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            scope=resource.scope,
            owner_id=owner_id,
            provider=provider,
            physical_target_id=physical_target_id,
            parent_target_id=parent_target_id,
            expected_fingerprint=expected_fingerprint,
        )
        document = self._load()
        existing = find_authority(document.authorities, resource)
        if existing is not None:
            if existing != authority:
                raise UninstallAuthorityError("Uninstall authority conflicts with a prior receipt.")
            return existing
        self._write(
            AuthorityDocument(
                authorities=sorted_authorities((*document.authorities, authority)),
                deletions=document.deletions,
            )
        )
        return authority

    def record_created_batch(
        self, authorities: tuple[UninstallAuthority, ...]
    ) -> tuple[UninstallAuthority, ...]:
        """Prevalidate a complete receipt set and publish it with one atomic write."""

        document = self._load()
        desired = {resource_key(authority): authority for authority in authorities}
        if len(desired) != len(authorities):
            raise UninstallAuthorityError(
                "Uninstall authority batch contains a duplicate resource."
            )
        existing = {resource_key(authority): authority for authority in document.authorities}
        for key, authority in desired.items():
            prior = existing.get(key)
            if prior is not None and prior != authority:
                raise UninstallAuthorityError(
                    "Uninstall authority conflicts with a prior receipt."
                )
        missing = tuple(authority for key, authority in desired.items() if key not in existing)
        if missing:
            self._write(
                AuthorityDocument(
                    authorities=sorted_authorities((*document.authorities, *missing)),
                    deletions=document.deletions,
                )
            )
        return authorities

    def load_created(self, resource: OwnedResource) -> UninstallAuthority | None:
        return find_authority(self._load().authorities, resource)

    def record_deletion(self, resource: OwnedResource) -> UninstallDeletionReceipt:
        authority = self.load_created(resource)
        if authority is None:
            raise UninstallAuthorityError("Deletion receipt requires recorded uninstall authority.")
        receipt = UninstallDeletionReceipt(
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            scope=resource.scope,
            expected_fingerprint=authority.expected_fingerprint,
        )
        document = self._load()
        existing = find_deletion(document.deletions, resource)
        if existing is not None:
            if existing != receipt:
                raise UninstallAuthorityError("Deletion receipt conflicts with a prior authority.")
            return existing
        self._write(
            AuthorityDocument(
                authorities=document.authorities,
                deletions=sorted_deletions((*document.deletions, receipt)),
            )
        )
        return receipt

    def load_deletion(self, resource: OwnedResource) -> UninstallDeletionReceipt | None:
        return find_deletion(self._load().deletions, resource)

    def _load(self) -> AuthorityDocument:
        if not self._path.exists():
            return empty_document()
        return parse_document(read_json(self._path))

    def _write(self, document: AuthorityDocument) -> None:
        atomic_json(self._path, document.to_dict())
