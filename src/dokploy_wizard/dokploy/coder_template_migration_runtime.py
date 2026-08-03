from __future__ import annotations

import hashlib
import ssl
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from dokploy_wizard.dokploy.coder_migration_api import (
    CoderMigrationApi,
    CoderTransportError,
    UrllibCoderTransport,
)
from dokploy_wizard.dokploy.coder_migration_receipt_types import ReceiptSchemaError
from dokploy_wizard.dokploy.coder_migration_receipts import (
    CoderMigrationReceiptStore,
    ReceiptConcurrencyError,
)
from dokploy_wizard.dokploy.coder_migration_types import CoderApiError, CoderProtocolError
from dokploy_wizard.dokploy.coder_migration_workspace_models import CoderMigrationBlockedError
from dokploy_wizard.dokploy.coder_runtime_asset_types import RuntimeAssetError
from dokploy_wizard.dokploy.coder_runtime_assets import validate_template_runtime_inputs
from dokploy_wizard.dokploy.coder_template_migration import CoderTemplateMigration
from dokploy_wizard.dokploy.coder_template_migration_inventory import (
    read_coder_migration_inventory,
)
from dokploy_wizard.dokploy.coder_template_migration_models import (
    TemplateMigrationDependencies,
    TemplateMigrationTarget,
)


class TemplateMigrationExecutionError(RuntimeError):
    def __init__(self, reason: str, *, code: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code

    def __str__(self) -> str:
        return self.reason if self.code is None else f"{self.code}: {self.reason}"


class RenderTemplate(Protocol):
    def __call__(
        self, *, template_dir: Path, replacements: dict[str, str] | None
    ) -> AbstractContextManager[Path]: ...


class CopyTemplate(Protocol):
    def __call__(self, *, container_name: str, template_name: str, template_dir: Path) -> None: ...


class PushTemplate(Protocol):
    def __call__(
        self,
        *,
        container_name: str,
        hostname: str,
        session_token: str,
        template_name: str,
        template_version_name: str | None,
    ) -> None: ...


class TemplateVersionName(Protocol):
    def __call__(self, *, template_dir: Path, replacements: dict[str, str] | None) -> str: ...


class ReadTemplateVersion(Protocol):
    def __call__(
        self, *, container_name: str, hostname: str, session_token: str, template_name: str
    ) -> str | None: ...


class ReadTemplateVersions(Protocol):
    def __call__(
        self, *, container_name: str, hostname: str, session_token: str, template_name: str
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class TemplateSource:
    name: str
    directory: Path
    replacements: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ProductionMigrationInputs:
    hostname: str
    session_token: str
    container_name: str
    state_dir: Path
    runtime_lock_sha256: str
    sources: tuple[TemplateSource, ...]
    render_template: RenderTemplate
    copy_template: CopyTemplate
    push_template: PushTemplate
    version_name: TemplateVersionName
    active_version_name: ReadTemplateVersion
    version_names_reader: ReadTemplateVersions


class _NoopCrashHook:
    def hit(self, point: str) -> None:
        del point


class RuntimeTemplatePusher:
    def __init__(self, inputs: ProductionMigrationInputs) -> None:
        self._inputs = inputs
        self._sources = {source.name: source for source in inputs.sources}

    def targets(self) -> tuple[TemplateMigrationTarget, ...]:
        validate_template_runtime_inputs(
            tuple(source.directory / "main.tf" for source in self._inputs.sources)
        )
        targets: list[TemplateMigrationTarget] = []
        for source in self._inputs.sources:
            _require_terraform_lock(source.directory)
            replacements = dict(source.replacements)
            with self._inputs.render_template(
                template_dir=source.directory, replacements=replacements
            ) as rendered:
                rendered_sha256 = _directory_sha256(rendered)
            targets.append(
                TemplateMigrationTarget(
                    name=source.name,
                    rendered_sha256=rendered_sha256,
                    runtime_lock_sha256=self._inputs.runtime_lock_sha256,
                    version_name=self._inputs.version_name(
                        template_dir=source.directory, replacements=replacements
                    ),
                )
            )
        return tuple(targets)

    def active_version_name(self, template_name: str) -> str | None:
        return self._inputs.active_version_name(
            container_name=self._inputs.container_name,
            hostname=self._inputs.hostname,
            session_token=self._inputs.session_token,
            template_name=template_name,
        )

    def version_names(self, template_name: str) -> tuple[str, ...]:
        return self._inputs.version_names_reader(
            container_name=self._inputs.container_name,
            hostname=self._inputs.hostname,
            session_token=self._inputs.session_token,
            template_name=template_name,
        )

    def push(self, target: TemplateMigrationTarget) -> None:
        source = self._sources.get(target.name)
        if source is None:
            raise TemplateMigrationExecutionError("template migration target is unavailable")
        replacements = dict(source.replacements)
        with self._inputs.render_template(
            template_dir=source.directory, replacements=replacements
        ) as rendered:
            _require_terraform_lock(rendered)
            if _directory_sha256(rendered) != target.rendered_sha256:
                raise TemplateMigrationExecutionError(
                    "template rendered digest changed before push"
                )
            self._inputs.copy_template(
                container_name=self._inputs.container_name,
                template_name=target.name,
                template_dir=rendered,
            )
        self._inputs.push_template(
            container_name=self._inputs.container_name,
            hostname=self._inputs.hostname,
            session_token=self._inputs.session_token,
            template_name=target.name,
            template_version_name=target.version_name,
        )


def execute_template_migration(inputs: ProductionMigrationInputs) -> None:
    pusher = RuntimeTemplatePusher(inputs)
    api = CoderMigrationApi(
        transport=_local_coder_transport(inputs.hostname),
        session_token=inputs.session_token,
    )
    migration = CoderTemplateMigration(
        TemplateMigrationDependencies(
            api=api,
            receipt_store=CoderMigrationReceiptStore(inputs.state_dir / "coder-template-migration"),
            pusher=pusher,
            clock=_utc_now,
            crash_hook=_NoopCrashHook(),
        )
    )
    try:
        migration.run(api.default_organization_id(), pusher.targets())
    except (
        CoderApiError,
        CoderMigrationBlockedError,
        CoderProtocolError,
        CoderTransportError,
        ReceiptConcurrencyError,
        ReceiptSchemaError,
        RuntimeAssetError,
    ) as error:
        code = error.code if isinstance(error, CoderMigrationBlockedError) else None
        raise TemplateMigrationExecutionError(
            "Coder template migration failed closed",
            code=code,
        ) from error


def execute_template_migration_preflight(hostname: str, session_token: str) -> None:
    """Validate retired Coder dependencies without creating a receipt or mutating."""

    api = CoderMigrationApi(
        transport=_local_coder_transport(hostname),
        session_token=session_token,
    )
    try:
        read_coder_migration_inventory(api, api.default_organization_id())
    except (
        CoderApiError,
        CoderMigrationBlockedError,
        CoderProtocolError,
        CoderTransportError,
    ) as error:
        code = error.code if isinstance(error, CoderMigrationBlockedError) else None
        raise TemplateMigrationExecutionError(
            "Coder template migration preflight failed closed",
            code=code,
        ) from error


def _local_coder_transport(hostname: str) -> UrllibCoderTransport:
    return UrllibCoderTransport(
        "https://127.0.0.1",
        host_header=hostname,
        ssl_context=ssl._create_unverified_context(),
    )


def _require_terraform_lock(directory: Path) -> None:
    lock = directory / ".terraform.lock.hcl"
    try:
        payload = lock.read_bytes()
    except OSError as error:
        raise TemplateMigrationExecutionError("template Terraform lock is unreadable") from error
    if not lock.is_file() or lock.is_symlink() or not payload:
        raise TemplateMigrationExecutionError("template Terraform lock is absent")


def _directory_sha256(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(path for path in directory.rglob("*") if path.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
