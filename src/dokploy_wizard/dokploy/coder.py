# mypy: ignore-errors
# ruff: noqa: E501
"""Dokploy-backed Coder runtime backend."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Protocol, assert_never
from urllib import error as urlerror
from urllib import parse
from urllib import request as urlrequest

from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.dokploy.client import (
    DokployApiClient,
    DokployApiError,
    DokployComposeRecord,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.dokploy.coder_runtime_asset_images import (
    DockerWorkspaceRuntimeImageAdapter,
    WorkspaceRuntimeImageAdapter,
    build_workspace_runtime_images,
)
from dokploy_wizard.dokploy.coder_runtime_assets import (
    load_runtime_manifest,
    runtime_manifest_path,
)
from dokploy_wizard.dokploy.coder_secret_client import DockerExecCoderSecretClient
from dokploy_wizard.dokploy.coder_secret_reconciliation import (
    CoderSecretClient,
    CoderSecretError,
    CoderSecretReconciler,
)
from dokploy_wizard.dokploy.coder_secret_specs import build_coder_secret_specs
from dokploy_wizard.dokploy.coder_template_migration_runtime import (
    ProductionMigrationInputs,
    TemplateMigrationExecutionError,
    TemplateSource,
    execute_template_migration,
    execute_template_migration_preflight,
)
from dokploy_wizard.dokploy.compose_noop import (
    apply_compose_noop_guard,
    apply_rendered_compose_to_existing,
    persist_compose_artifact_hash,
)
from dokploy_wizard.dokploy.container_resolution import resolve_compose_container_name
from dokploy_wizard.dokploy.env_spec import DokployEnvSpec, DokployEnvVar, RenderedCompose
from dokploy_wizard.litellm.config_renderer import verified_opencode_go_chat_model_ids
from dokploy_wizard.litellm.model_catalog import DEFAULT_LOCAL_CANONICAL_ALIAS
from dokploy_wizard.packs.coder import CoderError, CoderResourceRecord
from dokploy_wizard.state import load_state_dir
from dokploy_wizard.state.runtime_images import RuntimeImages, resolve_runtime_images
from dokploy_wizard.state.upgrade import (
    StateUpgradeError,
    planned_sync_owner_id,
    state_upgrade_paths,
    upgrade_state_contract,
)
from dokploy_wizard.verification import make_verification_result, redact_text


class DokployCoderApi(Protocol):
    def list_projects(self) -> tuple[DokployProjectSummary, ...]: ...
    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject: ...
    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord: ...
    def update_compose(
        self, *, compose_id: str, compose_file: str | None = None, env: str | None = None
    ) -> DokployComposeRecord: ...
    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult: ...


class CoderSecretClientFactory(Protocol):
    def __call__(
        self, *, container_name: str, session_token: str, state_dir: Path
    ) -> CoderSecretClient: ...


@dataclass(frozen=True)
class _ComposeLocator:
    project_id: str
    environment_id: str
    compose_id: str


@dataclass(frozen=True, slots=True)
class _CoderSecretInputs:
    coder_hermes_key: str
    coder_kdense_key: str
    visible_litellm_aliases: tuple[str, ...]


_DEFAULT_AI_DEFAULT_PROVIDER = "opencode-go"
_DEFAULT_AI_DEFAULT_MODEL = "deepseek-v4-flash"
_DEFAULT_HERMES_INFERENCE_PROVIDER = "dokploy-litellm"
_DEFAULT_HERMES_MODEL = "opencode-go/deepseek-v4-flash"
_DEFAULT_AI_DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
_DEFAULT_LITELLM_INTERNAL_PORT = 4000
_DEFAULT_CODER_OPENROUTER_ALIAS_SAMPLE = "openrouter/minimax/minimax-m2.5:free"
_COPILOT_BYOK_SETTING_KEY = "github.copilot.chat.customOAIModels"
_COPILOT_BYOK_PROVIDER_LABEL = "Dokploy LiteLLM"
_COPILOT_BYOK_CHAT_ONLY_LIMITATION = (
    "Official Copilot BYOK models apply to VS Code chat and agents only; "
    "inline completions still use Copilot-managed models."
)
_CODER_STATE_REQUIRED_FOR_MODIFY = frozenset({"applied-state.json", "ownership-ledger.json"})


class DokployCoderBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        stack_name: str,
        hostname: str,
        wildcard_hostname: str | None,
        admin_email: str,
        admin_password: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        ai_default_provider: str = _DEFAULT_AI_DEFAULT_PROVIDER,
        ai_default_model: str = _DEFAULT_AI_DEFAULT_MODEL,
        hermes_inference_provider: str = _DEFAULT_HERMES_INFERENCE_PROVIDER,
        hermes_model: str = _DEFAULT_HERMES_MODEL,
        ai_default_base_url: str = _DEFAULT_AI_DEFAULT_BASE_URL,
        ai_default_api_key: str | None = None,
        state_dir: Path = Path(".dokploy-wizard-state"),
        client: DokployCoderApi | None = None,
        image_digest: str | None = None,
        runtime_images: RuntimeImages | None = None,
        workspace_image_adapter: WorkspaceRuntimeImageAdapter | None = None,
        coder_hermes_key: str | None = None,
        coder_kdense_key: str | None = None,
        visible_litellm_aliases: tuple[str, ...] = (),
        coder_secret_client_factory: CoderSecretClientFactory | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._compose_name = _service_name(stack_name)
        self._hostname = hostname
        self._wildcard_hostname = wildcard_hostname
        self._admin_email = admin_email
        self._admin_password = admin_password
        self._postgres_service_name = postgres_service_name
        self._postgres = postgres
        self._ai_default_provider = ai_default_provider.strip() or _DEFAULT_AI_DEFAULT_PROVIDER
        self._ai_default_model = _normalize_ai_default_model(ai_default_model)
        self._hermes_inference_provider = (
            hermes_inference_provider.strip() or _DEFAULT_HERMES_INFERENCE_PROVIDER
        )
        self._hermes_model = _normalize_hermes_model_ref(hermes_model)
        self._ai_default_base_url = ai_default_base_url
        self._ai_default_api_key = ai_default_api_key
        self._state_dir = state_dir
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._applied_locator: _ComposeLocator | None = None
        self._created_in_process = False
        accepted_runtime_images = resolve_runtime_images({})
        self._runtime_images = runtime_images or accepted_runtime_images
        self._image_digest = image_digest or self._runtime_images.coder
        if (
            self._runtime_images.coder != accepted_runtime_images.coder
            or self._image_digest != accepted_runtime_images.coder
        ):
            raise CoderError("Coder service image does not match the accepted migration image.")
        self._workspace_image_adapter = (
            workspace_image_adapter
            or DockerWorkspaceRuntimeImageAdapter(
                repository_root=Path(__file__).resolve().parents[3]
            )
        )
        secret_values_supplied = (
            coder_hermes_key is not None
            or coder_kdense_key is not None
            or bool(visible_litellm_aliases)
        )
        if secret_values_supplied and (
            not coder_hermes_key or not coder_kdense_key or not visible_litellm_aliases
        ):
            raise CoderError("Coder LiteLLM secret inputs are incomplete.")
        self._coder_secret_inputs = (
            None
            if not secret_values_supplied
            else _CoderSecretInputs(
                coder_hermes_key=coder_hermes_key,
                coder_kdense_key=coder_kdense_key,
                visible_litellm_aliases=visible_litellm_aliases,
            )
        )
        self._coder_secret_client_factory = (
            coder_secret_client_factory or DockerExecCoderSecretClient
        )

    def get_service(self, resource_id: str) -> CoderResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "service")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return CoderResourceRecord(
            resource_id=resource_id, resource_name=_service_name(self._stack_name)
        )

    def find_service_by_name(self, resource_name: str) -> CoderResourceRecord | None:
        if resource_name != _service_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return CoderResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"), resource_name=resource_name
        )

    def create_service(self, **kwargs: object) -> CoderResourceRecord:
        resource_name = str(kwargs["resource_name"])
        hostname = str(kwargs["hostname"])
        wildcard_hostname = kwargs["wildcard_hostname"]
        postgres_service_name = str(kwargs["postgres_service_name"])
        postgres = kwargs["postgres"]
        data_resource_name = str(kwargs["data_resource_name"])
        if resource_name != _service_name(self._stack_name):
            raise CoderError("Coder service name does not match the active Dokploy plan.")
        if hostname != self._hostname or wildcard_hostname != self._wildcard_hostname:
            raise CoderError("Coder hostnames no longer match the active Dokploy plan.")
        if postgres_service_name != self._postgres_service_name or postgres != self._postgres:
            raise CoderError("Coder postgres inputs no longer match the active Dokploy plan.")
        if data_resource_name != _data_name(self._stack_name):
            raise CoderError("Coder data resource name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return CoderResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"), resource_name=resource_name
        )

    def update_service(self, **kwargs: object) -> CoderResourceRecord:
        kwargs.pop("resource_id", None)
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> CoderResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "data")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return CoderResourceRecord(
            resource_id=resource_id, resource_name=_data_name(self._stack_name)
        )

    def find_persistent_data_by_name(self, resource_name: str) -> CoderResourceRecord | None:
        if resource_name != _data_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return CoderResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"), resource_name=resource_name
        )

    def create_persistent_data(self, resource_name: str) -> CoderResourceRecord:
        if resource_name != _data_name(self._stack_name):
            raise CoderError("Coder data name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return CoderResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"), resource_name=resource_name
        )

    def check_health(self, *, service: CoderResourceRecord, url: str) -> bool:
        del service
        if _local_https_health_check(url):
            return True
        if _public_https_health_check(url):
            return True
        if self._created_in_process:
            return _wait_for_public_https_health(url)
        return False

    def ensure_application_ready(self) -> tuple[str, ...]:
        notes: list[str] = []
        if self._created_in_process:
            _wait_for_coder_bootstrap_api_ready(self._hostname)
        bootstrap_ready = False
        if not self._created_in_process:
            bootstrap_ready = self._verify_current_compose_application().passed
        first_user_provisioned = False
        if not bootstrap_ready and not _coder_first_user_exists(self._hostname):
            _create_coder_first_user(
                hostname=self._hostname,
                email=self._admin_email,
                password=self._admin_password,
            )
            first_user_provisioned = True
            notes.append(f"Provisioned initial Coder admin for '{self._admin_email}'.")
        session_token = _coder_login(
            hostname=self._hostname,
            email=self._admin_email,
            password=self._admin_password,
        )
        container_name = _coder_container_name(_service_name(self._stack_name))
        if container_name is None:
            raise CoderError("Coder container is not running; cannot finish application bootstrap.")
        hermes_litellm_base_url = _litellm_internal_base_url(self._stack_name)
        litellm_fallback_models_json = _shell_double_quote_escape(
            _litellm_workspace_fallback_models_json(
                default_alias=f"{self._ai_default_provider}/{self._ai_default_model}"
            )
        )
        self._reconcile_coder_workspace_secrets(
            container_name=container_name,
            session_token=session_token,
        )
        shared_network_name = _shared_network_name(self._stack_name)
        runtime_image_replacements = self._workspace_runtime_image_replacements()
        shared_defaults = {
            "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": shared_network_name,
            "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": _shell_double_quote_escape(
                self._ai_default_provider
            ),
            "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": _shell_double_quote_escape(
                self._ai_default_model
            ),
            "__DOKPLOY_WIZARD_AI_DEFAULT_BASE_URL__": _shell_double_quote_escape(
                hermes_litellm_base_url
            ),
            "__DOKPLOY_WIZARD_AI_DEFAULT_API_KEY__": _shell_double_quote_escape(
                self._ai_default_api_key or ""
            ),
            "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": litellm_fallback_models_json,
        }
        sources = (
            TemplateSource(
                _default_template_name(),
                _default_template_dir(),
                {**shared_defaults, **runtime_image_replacements},
            ),
            TemplateSource(
                _default_opencode_web_template_name(),
                _default_opencode_web_template_dir(),
                {**shared_defaults, **runtime_image_replacements},
            ),
            TemplateSource(
                _default_hermes_template_name(),
                _default_hermes_template_dir(),
                {
                    "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": shared_network_name,
                    "__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__": _shell_double_quote_escape(
                        self._hermes_inference_provider
                    ),
                    "__DOKPLOY_WIZARD_HERMES_MODEL__": _shell_double_quote_escape(
                        self._hermes_model
                    ),
                    "__DOKPLOY_WIZARD_HERMES_BASE_URL__": _shell_double_quote_escape(
                        hermes_litellm_base_url
                    ),
                    "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": litellm_fallback_models_json,
                    **runtime_image_replacements,
                },
            ),
            TemplateSource(
                _default_kdense_byok_template_name(),
                _default_kdense_byok_template_dir(),
                {
                    "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": shared_network_name,
                    "__DOKPLOY_WIZARD_AI_DEFAULT_PROVIDER__": _shell_double_quote_escape(
                        self._ai_default_provider
                    ),
                    "__DOKPLOY_WIZARD_AI_DEFAULT_MODEL__": _shell_double_quote_escape(
                        self._ai_default_model
                    ),
                    "__DOKPLOY_WIZARD_KDENSE_LITELLM_BASE_URL__": _shell_double_quote_escape(
                        hermes_litellm_base_url
                    ),
                    "__DOKPLOY_WIZARD_KDENSE_LITELLM_API_KEY__": _litellm_virtual_key_ref(
                        "coder-kdense"
                    ),
                    "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": litellm_fallback_models_json,
                    **runtime_image_replacements,
                },
            ),
        )
        try:
            execute_template_migration(
                ProductionMigrationInputs(
                    hostname=self._hostname,
                    session_token=session_token,
                    container_name=container_name,
                    state_dir=self._state_dir,
                    runtime_lock_sha256=load_runtime_manifest(runtime_manifest_path()).lock_sha256,
                    sources=sources,
                    render_template=_rendered_template_dir,
                    copy_template=_docker_copy_template_dir,
                    push_template=_push_default_template,
                    version_name=_template_version_name,
                    active_version_name=_active_template_version_name,
                    version_names_reader=_template_version_names,
                )
            )
        except TemplateMigrationExecutionError as error:
            detail = "" if error.code is None else f" {error.code}"
            raise CoderError(f"Coder template migration failed closed.{detail}") from None
        if bootstrap_ready:
            return tuple(notes)
        workspace_name = _default_workspace_name(self._hostname)
        try:
            if _ensure_default_workspace(
                container_name=container_name,
                hostname=self._hostname,
                session_token=session_token,
                workspace_name=workspace_name,
                template_name=_default_template_name(),
            ):
                if first_user_provisioned:
                    notes.append(
                        f"Created default Coder workspace '{workspace_name}' for '{self._admin_email}'."
                    )
                else:
                    notes.append(f"Created missing default Coder workspace '{workspace_name}'.")
        except CoderError as e:
            notes.append(f"Skipped default workspace creation: {e}")
        return tuple(notes)

    def _reconcile_coder_workspace_secrets(
        self, *, container_name: str, session_token: str
    ) -> None:
        inputs = self._coder_secret_inputs
        if inputs is None:
            return
        try:
            specs = build_coder_secret_specs(
                stack_name=self._stack_name,
                default_alias=f"{self._ai_default_provider}/{self._ai_default_model}",
                visible_aliases=inputs.visible_litellm_aliases,
                coder_hermes_key=inputs.coder_hermes_key,
                coder_kdense_key=inputs.coder_kdense_key,
            )
        except ValueError:
            raise CoderError("Coder workspace secret specification is invalid.") from None
        owner_id = hashlib.sha256(
            f"coder-secret:{self._stack_name}:{self._hostname}".encode()
        ).hexdigest()
        reconciler = CoderSecretReconciler(
            state_dir=self._state_dir,
            client=self._coder_secret_client_factory(
                container_name=container_name,
                session_token=session_token,
                state_dir=self._state_dir,
            ),
            owner_id=owner_id,
        )
        try:
            reconciler.reconcile(specs)
        except CoderSecretError as error:
            match error.kind:
                case "receipt":
                    failure_kind = "receipt"
                case "unknown":
                    failure_kind = "unknown"
                case unreachable:
                    assert_never(unreachable)
            raise CoderError(
                f"Coder workspace secret reconciliation failed. {failure_kind}"
            ) from None

    def _workspace_runtime_image_replacements(self) -> dict[str, str]:
        loaded = load_state_dir(self._state_dir)
        persisted = None if loaded.desired_state is None else loaded.desired_state.runtime_images
        if (
            persisted is not None
            and persisted.workspace_amd64 is not None
            and persisted.workspace_arm64 is not None
        ):
            self._runtime_images = persisted
        if (
            self._runtime_images.workspace_amd64 is None
            or self._runtime_images.workspace_arm64 is None
        ):
            manifest = load_runtime_manifest(runtime_manifest_path())
            derived = build_workspace_runtime_images(manifest, self._workspace_image_adapter)
            self._runtime_images = replace(
                self._runtime_images,
                workspace_amd64=derived.amd64,
                workspace_arm64=derived.arm64,
            )
        if (
            loaded.desired_state is None
            or loaded.applied_state is None
            or loaded.ownership_ledger is None
        ):
            raise CoderError("Coder runtime image state is unavailable.")
        try:
            upgrade_state_contract(
                state_dir=self._state_dir,
                owner_id=planned_sync_owner_id(self._state_dir),
                paths=state_upgrade_paths(self._state_dir),
                runtime_images=self._runtime_images,
                ownership_ledger=loaded.ownership_ledger,
            )
        except StateUpgradeError as error:
            raise CoderError("Unable to persist Coder runtime image IDs.") from error
        amd64 = self._runtime_images.workspace_amd64
        arm64 = self._runtime_images.workspace_arm64
        if amd64 is None or arm64 is None:
            raise CoderError("Coder runtime image IDs are incomplete.")
        return _workspace_runtime_image_replacements(amd64, arm64)

    def _find_compose_locator(self) -> _ComposeLocator | None:
        if self._applied_locator is not None:
            return self._applied_locator
        try:
            projects = self._client.list_projects()
        except DokployApiError as error:
            raise CoderError(str(error)) from error
        for project in projects:
            if project.name != self._stack_name:
                continue
            environment = _pick_environment(project)
            if environment is None:
                continue
            for compose in environment.composes:
                if compose.name == self._compose_name:
                    locator = _ComposeLocator(
                        project_id=project.project_id,
                        environment_id=environment.environment_id,
                        compose_id=compose.compose_id,
                    )
                    self._applied_locator = locator
                    return locator
        return None

    def _ensure_compose_applied(self) -> _ComposeLocator:
        if self._applied_locator is not None and self._created_in_process:
            return self._applied_locator
        compose_file = _render_compose_file(
            stack_name=self._stack_name,
            hostname=self._hostname,
            wildcard_hostname=self._wildcard_hostname,
            postgres_service_name=self._postgres_service_name,
            postgres=self._postgres,
            image_digest=self._image_digest,
        )
        try:
            if self._applied_locator is not None:
                self._applied_locator = self._apply_existing_compose(
                    locator=self._applied_locator,
                    compose_file=compose_file,
                )
                return self._applied_locator
            projects = self._client.list_projects()
            for project in projects:
                if project.name != self._stack_name:
                    continue
                environment = _pick_environment(project)
                if environment is None:
                    break
                for compose in environment.composes:
                    if compose.name == self._compose_name:
                        locator = _ComposeLocator(
                            project_id=project.project_id,
                            environment_id=environment.environment_id,
                            compose_id=compose.compose_id,
                        )
                        self._applied_locator = self._apply_existing_compose(
                            locator=locator,
                            compose_file=compose_file,
                        )
                        return self._applied_locator
                created = self._client.create_compose(
                    name=self._compose_name,
                    environment_id=environment.environment_id,
                    compose_file="services: {}\n",
                    app_name=self._compose_name,
                )
                apply_rendered_compose_to_existing(
                    client=self._client,
                    compose_id=created.compose_id,
                    rendered_compose=compose_file,
                )
                deployment = self._client.deploy_compose(
                    compose_id=created.compose_id,
                    title="dokploy-wizard coder reconcile",
                    description="Create Coder compose app",
                )
                if not deployment.success:
                    msg = "Dokploy deploy for compose service 'coder' did not report success."
                    raise RuntimeError(msg)
                locator = _ComposeLocator(
                    project_id=project.project_id,
                    environment_id=environment.environment_id,
                    compose_id=created.compose_id,
                )
                self._applied_locator = locator
                self._created_in_process = True
                self._persist_compose_hash_if_checkpoint_present(compose_file)
                return locator
            created_project = self._client.create_project(
                name=self._stack_name, description="Managed by dokploy-wizard", env=None
            )
            created_compose = self._client.create_compose(
                name=self._compose_name,
                environment_id=created_project.environment_id,
                compose_file="services: {}\n",
                app_name=self._compose_name,
            )
            apply_rendered_compose_to_existing(
                client=self._client,
                compose_id=created_compose.compose_id,
                rendered_compose=compose_file,
            )
            deployment = self._client.deploy_compose(
                compose_id=created_compose.compose_id,
                title="dokploy-wizard coder reconcile",
                description="Create Coder compose app",
            )
            if not deployment.success:
                msg = "Dokploy deploy for compose service 'coder' did not report success."
                raise RuntimeError(msg)
        except (DokployApiError, RuntimeError) as error:
            raise CoderError(str(error)) from error
        locator = _ComposeLocator(
            project_id=created_project.project_id,
            environment_id=created_project.environment_id,
            compose_id=created_compose.compose_id,
        )
        self._applied_locator = locator
        self._created_in_process = True
        self._persist_compose_hash_if_checkpoint_present(compose_file)
        return locator

    def _apply_existing_compose(
        self, *, locator: _ComposeLocator, compose_file: RenderedCompose
    ) -> _ComposeLocator:
        if not self._has_applied_state_checkpoint():
            updated = apply_rendered_compose_to_existing(
                client=self._client,
                compose_id=locator.compose_id,
                rendered_compose=compose_file,
            )
            deployment = self._client.deploy_compose(
                compose_id=updated.compose_id,
                title="dokploy-wizard coder reconcile",
                description="Update Coder compose app",
            )
            if not deployment.success:
                msg = "Dokploy deploy for compose service 'coder' did not report success."
                raise RuntimeError(msg)
            self._created_in_process = True
            return _ComposeLocator(
                project_id=locator.project_id,
                environment_id=locator.environment_id,
                compose_id=updated.compose_id,
            )
        apply_result = apply_compose_noop_guard(
            rendered_compose=compose_file,
            service_key=self._compose_name,
            state_dir=self._state_dir,
            client=self._client,
            locator=locator,
            compose_id=locator.compose_id,
            title="dokploy-wizard coder reconcile",
            description="Update Coder compose app",
            verify_current=self._verify_current_compose_application,
            locator_factory=lambda compose_id: _ComposeLocator(
                project_id=locator.project_id,
                environment_id=locator.environment_id,
                compose_id=compose_id,
            ),
        )
        self._created_in_process = apply_result.status == "applied"
        return apply_result.locator

    def _has_applied_state_checkpoint(self) -> bool:
        return load_state_dir(self._state_dir).applied_state is not None

    def _persist_compose_hash_if_checkpoint_present(self, compose_file: RenderedCompose) -> None:
        if not self._has_applied_state_checkpoint():
            return
        persist_compose_artifact_hash(
            state_dir=self._state_dir,
            service_key=self._compose_name,
            rendered_compose=compose_file,
        )

    def _verify_current_compose_application(self):
        service_name = _service_name(self._stack_name)
        health_url = f"https://{self._hostname}/healthz"
        if not self.check_health(
            service=CoderResourceRecord(
                resource_id=_resource_id(self._compose_name, "service"),
                resource_name=service_name,
            ),
            url=health_url,
        ):
            return make_verification_result(
                service_name=self._compose_name,
                tier="app",
                passed=False,
                detail=f"Coder health checks failed for '{health_url}'.",
            )
        container_name = _coder_container_name(service_name)
        if container_name is None:
            return make_verification_result(
                service_name=self._compose_name,
                tier="bootstrap",
                passed=False,
                detail="Coder container is not running.",
            )
        try:
            _wait_for_coder_bootstrap_api_ready(self._hostname)
            if not _coder_first_user_exists(self._hostname):
                return make_verification_result(
                    service_name=self._compose_name,
                    tier="bootstrap",
                    passed=False,
                    detail=f"Coder first user '{self._admin_email}' is not provisioned.",
                )
            session_token = _coder_login(
                hostname=self._hostname,
                email=self._admin_email,
                password=self._admin_password,
            )
            workspace_name = _default_workspace_name(self._hostname)
            missing_templates: list[str] = []
            workspace_missing = False
            for attempt in range(3):
                template_names = {
                    item.get("name")
                    for item in _list_templates(
                        container_name=container_name,
                        hostname=self._hostname,
                        session_token=session_token,
                    )
                    if isinstance(item.get("name"), str)
                }
                missing_templates = [
                    template_name
                    for template_name in _required_template_names()
                    if template_name not in template_names
                ]
                workspace_missing = workspace_name not in _list_workspaces(
                    container_name=container_name,
                    hostname=self._hostname,
                    session_token=session_token,
                )
                if not missing_templates and not workspace_missing:
                    break
                if attempt < 2:
                    time.sleep(2.0)

            if missing_templates:
                return make_verification_result(
                    service_name=self._compose_name,
                    tier="bootstrap",
                    passed=False,
                    detail=(
                        "Coder templates are missing or not visible: "
                        + ", ".join(missing_templates)
                        + "."
                    ),
                )
            if workspace_missing:
                return make_verification_result(
                    service_name=self._compose_name,
                    tier="bootstrap",
                    passed=False,
                    detail=f"Coder default workspace '{workspace_name}' is missing.",
                )
        except CoderError as error:
            return make_verification_result(
                service_name=self._compose_name,
                tier="bootstrap",
                passed=False,
                detail=str(error),
            )
        return make_verification_result(
            service_name=self._compose_name,
            tier="bootstrap",
            passed=True,
            detail=(
                "Coder container/API, first user bootstrap, seeded templates, "
                "and default workspace are ready."
            ),
        )


def _resource_id(compose_id: str, kind: str) -> str:
    return f"dokploy-compose:{compose_id}:{kind}"


def _parse_resource_id(resource_id: str, kind: str) -> str | None:
    prefix = "dokploy-compose:"
    suffix = f":{kind}"
    if not resource_id.startswith(prefix) or not resource_id.endswith(suffix):
        return None
    compose_id = resource_id.removeprefix(prefix).removesuffix(suffix)
    return compose_id or None


def _pick_environment(project: DokployProjectSummary) -> DokployEnvironmentSummary | None:
    if not project.environments:
        return None
    for environment in project.environments:
        if environment.is_default:
            return environment
    return project.environments[0]


def _service_name(stack_name: str) -> str:
    return f"{stack_name}-coder"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-coder-data"


def _shared_network_name(stack_name: str) -> str:
    return f"{stack_name}-shared"


def _litellm_internal_base_url(stack_name: str) -> str:
    return f"http://{stack_name}-shared-litellm:{_DEFAULT_LITELLM_INTERNAL_PORT}"


def _litellm_virtual_key_ref(consumer: str) -> str:
    normalized = consumer.strip().replace("-", "_").upper()
    return f"$${{LITELLM_VIRTUAL_KEY_{normalized}}}"


def _workspace_runtime_image_replacements(amd64: str, arm64: str) -> dict[str, str]:
    return {
        "__DOKPLOY_WIZARD_RUNTIME_IMAGE_AMD64__": amd64,
        "__DOKPLOY_WIZARD_RUNTIME_IMAGE_ARM64__": arm64,
    }


def _normalize_hermes_model_ref(model_ref: str) -> str:
    normalized = model_ref.strip()
    if normalized in {"", "unsloth-active", "local/unsloth-active"}:
        return DEFAULT_LOCAL_CANONICAL_ALIAS
    return normalized


def _normalize_ai_default_model(model_ref: str) -> str:
    normalized = model_ref.strip()
    if normalized == "":
        return _DEFAULT_AI_DEFAULT_MODEL
    provider_prefix = f"{_DEFAULT_AI_DEFAULT_PROVIDER}/"
    if normalized.startswith(provider_prefix):
        return normalized.removeprefix(provider_prefix)
    return normalized


def _wildcard_suffix(wildcard_hostname: str) -> str:
    if not wildcard_hostname.startswith("*."):
        raise CoderError("Coder wildcard hostname must start with '*.'")
    return wildcard_hostname.removeprefix("*.")


def _render_compose_file(
    *,
    stack_name: str,
    hostname: str,
    wildcard_hostname: str | None,
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
    image_digest: str | None = None,
) -> RenderedCompose:
    service_name = _service_name(stack_name)
    data_name = _data_name(stack_name)
    shared_network = _shared_network_name(stack_name)
    image_digest = image_digest or resolve_runtime_images({}).coder
    wildcard_env = ""
    wildcard_router = ""
    if wildcard_hostname is not None:
        wildcard_suffix = _wildcard_suffix(wildcard_hostname)
        wildcard_host_pattern = re.escape(wildcard_suffix).replace("\\", "\\\\")
        wildcard_env = f"      CODER_WILDCARD_ACCESS_URL: {_yaml_quote(wildcard_hostname)}\n"
        wildcard_router = (
            f'      traefik.http.routers.{service_name}-wildcard.entrypoints: "websecure"\n'
            f'      traefik.http.routers.{service_name}-wildcard.rule: "HostRegexp(`(?i)^[a-z0-9-]+(?:--[a-z0-9-]+){{2,}}\\\\.{wildcard_host_pattern}$`)"\n'
            f'      traefik.http.routers.{service_name}-wildcard.middlewares: "{service_name}-forwarded-https"\n'
            f'      traefik.http.routers.{service_name}-wildcard.tls: "true"\n'
        )
    pg_url_env = "CODER_PG_CONNECTION_URL"
    pg_url = (
        f"postgres://{postgres.user_name}:change-me@{postgres_service_name}:5432/"
        f"{postgres.database_name}?sslmode=disable"
    )
    compose_file = (
        "services:\n"
        f"  {service_name}:\n"
        f"    image: {image_digest}\n"
        "    restart: unless-stopped\n"
        '    user: "0:0"\n'
        "    environment:\n"
        "      CODER_HTTP_ADDRESS: 0.0.0.0:3000\n"
        f"      CODER_ACCESS_URL: {_yaml_quote(f'https://{hostname}/')}\n"
        f"{wildcard_env}"
        f'      CODER_PG_CONNECTION_URL: "{_required_placeholder(pg_url_env)}"\n'
        '      CODER_DERP_FORCE_WEBSOCKETS: "true"\n'
        f"      CODER_PROXY_TRUSTED_HEADERS: {_yaml_quote('X-Forwarded-For')}\n"
        f"      CODER_PROXY_TRUSTED_ORIGINS: {_yaml_quote('10.0.0.0/8,172.16.0.0/12,192.168.0.0/16')}\n"
        f"      CODER_CACHE_DIRECTORY: {_yaml_quote('/home/coder/.cache')}\n"
        "    labels:\n"
        '      traefik.enable: "true"\n'
        f'      traefik.http.routers.{service_name}.entrypoints: "websecure"\n'
        f'      traefik.http.routers.{service_name}.rule: "Host(`{hostname}`)"\n'
        f'      traefik.http.routers.{service_name}.middlewares: "{service_name}-forwarded-https,{service_name}-forwarded-host"\n'
        f'      traefik.http.routers.{service_name}.tls: "true"\n'
        f"{wildcard_router}"
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Proto: "https"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Port: "443"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-host.headers.customrequestheaders.X-Forwarded-Host: "{hostname}"\n'
        f'      traefik.http.services.{service_name}.loadbalancer.server.port: "3000"\n'
        "    healthcheck:\n"
        "      test: ['CMD-SHELL', 'wget -qO- http://127.0.0.1:3000/healthz >/dev/null']\n"
        "      interval: 30s\n"
        "      timeout: 5s\n"
        "      retries: 10\n"
        f"    volumes:\n      - {data_name}:/home/coder\n"
        "      - /var/run/docker.sock:/var/run/docker.sock\n"
        "    expose:\n"
        "      - '3000'\n"
        "    networks:\n"
        "      - default\n"
        "      - dokploy-network\n"
        f"      - {shared_network}\n"
        "volumes:\n"
        f"  {data_name}:\n"
        "networks:\n"
        "  dokploy-network:\n"
        "    external: true\n"
        f"  {shared_network}:\n"
        f"    name: {shared_network}\n"
        "    external: true\n"
    )
    return RenderedCompose(
        compose_file=compose_file,
        env_specs=(
            _coder_env_spec(
                name=pg_url_env,
                value=pg_url,
                target_services=(service_name,),
                source="coder-postgres-url",
            ),
        ),
    )


def _coder_env_spec(
    *, name: str, value: str, target_services: tuple[str, ...], source: str
) -> DokployEnvSpec:
    return DokployEnvSpec(
        variable=DokployEnvVar(name=name, value=value, sensitive=True, source=source),
        owner="coder",
        target_services=target_services,
        placeholder=_required_placeholder(name),
        required=True,
    )


def _required_placeholder(name: str) -> str:
    return f"${{{name}:?{name} is required}}"


def _local_https_health_check(url: str) -> bool:
    parsed = parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        return False
    connection = http.client.HTTPSConnection(
        "127.0.0.1",
        443,
        timeout=10,
        context=ssl._create_unverified_context(),
    )
    try:
        connection.request(
            "GET",
            parsed.path or "/",
            headers={"Host": parsed.netloc},
        )
        response = connection.getresponse()
        response.read()
        return response.status == 200
    except OSError:
        return False
    finally:
        connection.close()


def _public_https_health_check(url: str) -> bool:
    try:
        context = ssl._create_unverified_context()
        with urlrequest.urlopen(url, timeout=10, context=context) as response:  # noqa: S310
            response.read()
            return response.status == 200
    except (urlerror.HTTPError, urlerror.URLError, OSError, TimeoutError):
        return False


def _wait_for_public_https_health(
    url: str, *, attempts: int = 19, delay_seconds: float = 5.0
) -> bool:
    for attempt in range(attempts):
        if _public_https_health_check(url):
            return True
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return False


def _wait_for_coder_bootstrap_api_ready(
    hostname: str, *, attempts: int = 12, delay_seconds: float = 5.0
) -> None:
    for attempt in range(attempts):
        try:
            _coder_request(hostname=hostname, method="GET", path="/api/v2/users/first")
            return
        except _CoderHTTPError as exc:
            if exc.status == 404:
                return
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
                continue
            raise CoderError(
                f"Coder bootstrap API did not become ready before first-user setup (HTTP {exc.status})."
            ) from exc
    raise CoderError("Coder bootstrap API did not become ready before first-user setup.")


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _coder_first_user_exists(hostname: str) -> bool:
    try:
        _coder_request(hostname=hostname, method="GET", path="/api/v2/users/first")
    except _CoderHTTPError as exc:
        if exc.status == 404:
            return False
        raise CoderError(f"Unable to determine Coder bootstrap state: HTTP {exc.status}") from exc
    return True


def _create_coder_first_user(
    *, hostname: str, email: str, password: str, attempts: int = 12, delay_seconds: float = 5.0
) -> None:
    path = "/api/v2/users/first"
    payload: dict[str, object] = {
        "email": email,
        "username": _username_from_email(email),
        "name": _display_name_from_email(email),
        "password": password,
    }
    for attempt in range(attempts):
        try:
            _coder_request(
                hostname=hostname,
                method="POST",
                path=path,
                payload=payload,
                expected_statuses={201},
            )
            return
        except _CoderHTTPError as exc:
            if exc.status == 404 and attempt < attempts - 1:
                time.sleep(delay_seconds)
                continue
            raise CoderError(
                f"Coder bootstrap first-user creation failed at POST {path}: HTTP {exc.status}."
            ) from exc
    raise CoderError(f"Coder bootstrap first-user creation failed at POST {path}.")


def _coder_login(*, hostname: str, email: str, password: str) -> str:
    response = _coder_request(
        hostname=hostname,
        method="POST",
        path="/api/v2/users/login",
        payload={"email": email, "password": password},
        expected_statuses={200, 201},
    )
    token = response.get("session_token")
    if not isinstance(token, str) or token == "":
        raise CoderError("Coder login response did not include a session token.")
    return token


def preflight_coder_template_migration(hostname: str, email: str, password: str) -> None:
    """Run the production Coder migration's read-only global preflight."""

    session_token = _coder_login(hostname=hostname, email=email, password=password)
    try:
        execute_template_migration_preflight(hostname, session_token)
    except TemplateMigrationExecutionError as error:
        if error.code is not None:
            raise
        raise CoderError("Coder template migration preflight failed closed.") from None


def _coder_request(
    *,
    hostname: str,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    expected_statuses: set[int] | None = None,
) -> dict[str, object]:
    if expected_statuses is None:
        expected_statuses = {200}
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        f"https://127.0.0.1{path}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Host": hostname,
        },
        method=method,
    )
    try:
        context = ssl._create_unverified_context()
        opener = urlrequest.build_opener(urlrequest.HTTPSHandler(context=context))
        with opener.open(req, timeout=20) as response:
            raw = response.read().decode("utf-8", "ignore")
            if response.status not in expected_statuses:
                raise CoderError(f"Coder request {method} {path} returned HTTP {response.status}.")
            return {} if raw == "" else json.loads(raw)
    except urlerror.HTTPError as exc:
        raise _CoderHTTPError(status=exc.code) from exc


def _coder_container_name(service_name: str) -> str | None:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.service={service_name}",
            "--format",
            "{{.Names}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to locate Coder container: {(result.stderr or result.stdout).strip()}"
        )
    return resolve_compose_container_name(service_name, result.stdout.splitlines())


def _default_template_dir() -> Path:
    return (
        Path(__file__).resolve().parents[3] / "templates" / "coder" / "default-ubuntu-code-server"
    )


def _default_template_name() -> str:
    return "ubuntu-vscode-opencode-pi"


def _default_opencode_web_template_dir() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "templates"
        / "coder"
        / "default-ubuntu-code-server-opencode-web"
    )


def _default_opencode_web_template_name() -> str:
    return "ubuntu-vscode-opencode-web"


def _default_openwork_template_dir() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "templates"
        / "coder"
        / "default-ubuntu-code-server-openwork"
    )


def _default_openwork_template_name() -> str:
    return "ubuntu-vscode-openwork"


def _default_kdense_byok_template_dir() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "templates"
        / "coder"
        / "default-ubuntu-code-server-kdense-byok"
    )


def _default_kdense_byok_template_name() -> str:
    return "ubuntu-vscode-kdense-byok"


def _default_hermes_template_dir() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "templates"
        / "coder"
        / "default-ubuntu-code-server-hermes"
    )


def _default_hermes_template_name() -> str:
    return "ubuntu-vscode-hermes"


def _default_pi_web_template_dir() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "templates"
        / "coder"
        / "default-ubuntu-code-server-pi-web"
    )


def _default_pi_web_template_name() -> str:
    return "ubuntu-vscode-pi-web"


def _default_workspace_name(hostname: str, *, today: date | None = None) -> str:
    root_domain = (
        hostname.split(".", 1)[1] if hostname.startswith("coder.") and "." in hostname else hostname
    )
    root_token = re.sub(r"[^a-z0-9]+", "", root_domain.lower())
    effective_date = (today or date.today()).isoformat()
    suffix = f"-workspace-{effective_date}"
    max_root_length = max(1, 32 - len(suffix))
    normalized_root = (root_token or "workspace")[:max_root_length]
    return f"{normalized_root}{suffix}"


def _required_template_names() -> tuple[str, ...]:
    return (
        _default_template_name(),
        _default_opencode_web_template_name(),
        _default_hermes_template_name(),
        _default_kdense_byok_template_name(),
    )


def _seed_template(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    template_name: str,
    template_dir: Path,
    replacements: dict[str, str] | None,
) -> bool:
    _emit_coder_progress(f"Checking Coder template '{template_name}'.")
    desired_version_name = _template_version_name(
        template_dir=template_dir,
        replacements=replacements,
    )
    if (
        _active_template_version_name(
            container_name=container_name,
            hostname=hostname,
            session_token=session_token,
            template_name=template_name,
        )
        == desired_version_name
    ):
        _emit_coder_progress(
            f"Coder template '{template_name}' already has the desired active version."
        )
        return False
    if desired_version_name in _template_version_names(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
        template_name=template_name,
    ):
        _emit_coder_progress(
            f"Coder template '{template_name}' already has desired version '{desired_version_name}'."
        )
        return False
    _copy_template_into_container(
        container_name=container_name,
        template_dir=template_dir,
        template_name=template_name,
        replacements=replacements,
    )
    _emit_coder_progress(
        f"Pushing Coder template '{template_name}' as version '{desired_version_name}'."
    )
    try:
        _push_default_template(
            container_name=container_name,
            hostname=hostname,
            session_token=session_token,
            template_name=template_name,
            template_version_name=desired_version_name,
        )
    except CoderError as e:
        _emit_coder_progress(
            f"Coder template '{template_name}' push failed: {_safe_progress_reason(str(e))}"
        )
        raise
    _emit_coder_progress(f"Finished Coder template '{template_name}' push.")
    return True


def _emit_coder_progress(message: str) -> None:
    print(f"[dokploy-wizard] {message}", file=sys.stderr)


def _safe_progress_reason(reason: str, *, limit: int = 360) -> str:
    safe = " ".join(redact_text(reason).split())
    if len(safe) <= limit:
        return safe
    return f"{safe[: limit - 1]}…"


def _template_version_name(*, template_dir: Path, replacements: dict[str, str] | None) -> str:
    digest = hashlib.sha256()
    digest.update(template_dir.name.encode("utf-8"))
    digest.update(b"\0")
    with _rendered_template_dir(
        template_dir=template_dir, replacements=replacements
    ) as rendered_dir:
        for path in sorted(path for path in rendered_dir.rglob("*") if path.is_file()):
            digest.update(path.relative_to(rendered_dir).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    digest.update(b"runtime-manifest\0")
    digest.update(load_runtime_manifest(runtime_manifest_path()).lock_sha256.encode("utf-8"))
    digest.update(b"\0")
    return f"dokploy-wizard-{digest.hexdigest()[:16]}"


def _copy_template_into_container(
    *,
    container_name: str,
    template_dir: Path,
    template_name: str,
    replacements: dict[str, str] | None,
) -> None:
    subprocess.run(
        ["docker", "exec", container_name, "rm", "-rf", f"/tmp/{template_name}"],
        check=False,
        capture_output=True,
        text=True,
    )
    with _rendered_template_dir(
        template_dir=template_dir, replacements=replacements
    ) as copy_source:
        _docker_copy_template_dir(
            container_name=container_name,
            template_name=template_name,
            template_dir=copy_source,
        )


@contextmanager
def _rendered_template_dir(
    *, template_dir: Path, replacements: dict[str, str] | None
) -> Iterator[Path]:
    if not template_dir.exists():
        raise CoderError(f"Default Coder template directory is missing: {template_dir}")
    with tempfile.TemporaryDirectory(prefix="dokploy-wizard-coder-template-") as tmp_dir:
        rendered_dir = Path(tmp_dir) / template_dir.name
        shutil.copytree(template_dir, rendered_dir)
        if replacements:
            rendered_main_tf = rendered_dir / "main.tf"
            contents = rendered_main_tf.read_text(encoding="utf-8")
            for placeholder, value in replacements.items():
                contents = contents.replace(placeholder, value)
            rendered_main_tf.write_text(contents, encoding="utf-8")
        _write_workspace_model_sync_utility(rendered_dir)
        yield rendered_dir


def _workspace_model_sync_utility_sources() -> tuple[tuple[Path, str], ...]:
    source_root = Path(__file__).resolve().parents[2]
    dokploy_root = source_root / "dokploy_wizard" / "dokploy"
    utility_sources = tuple(sorted(dokploy_root.glob("workspace_catalog_sync*.py")))
    return tuple((source, f"dokploy_wizard/dokploy/{source.name}") for source in utility_sources)


def _write_workspace_model_sync_utility(template_dir: Path) -> None:
    utility_path = template_dir / ".dokploy-wizard/model-sync/workspace-catalog-sync.pyz"
    utility_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(
        utility_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        _write_zipapp_member(archive, "dokploy_wizard/__init__.py", b"")
        _write_zipapp_member(archive, "dokploy_wizard/dokploy/__init__.py", b"")
        for source, archive_path in _workspace_model_sync_utility_sources():
            _write_zipapp_member(archive, archive_path, source.read_bytes())
        _write_zipapp_member(
            archive,
            "__main__.py",
            b"from dokploy_wizard.dokploy.workspace_catalog_sync_runtime import main\n"
            b"raise SystemExit(main())\n",
        )


def _write_zipapp_member(archive: zipfile.ZipFile, path: str, content: bytes) -> None:
    metadata = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    metadata.create_system = 3
    metadata.external_attr = 0o100644 << 16
    archive.writestr(
        metadata,
        content,
        compress_type=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    )


def _docker_copy_template_dir(
    *, container_name: str, template_name: str, template_dir: Path
) -> None:
    result = subprocess.run(
        ["docker", "cp", str(template_dir), f"{container_name}:/tmp/{template_name}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to copy default Coder template into container: {(result.stderr or result.stdout).strip()}"
        )


def _coder_cli_url() -> str:
    return "http://127.0.0.1:3000"


def _push_default_template(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    template_name: str,
    template_version_name: str | None = None,
) -> None:
    command = [
        "docker",
        "exec",
        "-e",
        f"CODER_URL={_coder_cli_url()}",
        "-e",
        f"CODER_SESSION_TOKEN={session_token}",
        container_name,
        "/opt/coder",
        "templates",
        "push",
        template_name,
    ]
    if template_version_name:
        command.extend(["--name", template_version_name])
    command.extend(
        [
            "--directory",
            f"/tmp/{template_name}",
            "--yes",
        ]
    )
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output = "\n".join(part for part in (result.stderr, result.stdout) if part).strip()
        if template_version_name and _is_duplicate_template_version_error(
            output=output,
            template_version_name=template_version_name,
        ):
            return
        raise CoderError(f"Unable to push default Coder template '{template_name}': {output}")


def _is_duplicate_template_version_error(*, output: str, template_version_name: str) -> bool:
    if not output:
        return False
    normalized_output = output.lower()
    if template_version_name.lower() not in normalized_output:
        return False
    if "template version" not in normalized_output:
        return False
    return "already exists" in normalized_output or "already in use" in normalized_output


def _sync_hermes_workspace_secrets(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    hermes_inference_provider: str,
    hermes_model: str,
    ai_default_base_url: str,
    ai_default_api_key: str | None,
) -> None:
    del (
        container_name,
        hostname,
        session_token,
        hermes_inference_provider,
        hermes_model,
        ai_default_base_url,
        ai_default_api_key,
    )


def _shell_double_quote_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


def _litellm_workspace_fallback_models_json(*, default_alias: str) -> str:
    model_ids = [default_alias]
    model_ids.extend(
        f"opencode-go/{model_id}" for model_id in verified_opencode_go_chat_model_ids()
    )
    model_ids.append(_DEFAULT_CODER_OPENROUTER_ALIAS_SAMPLE)
    return json.dumps(list(dict.fromkeys(model_ids)))


def _copilot_byok_contract() -> dict[str, str]:
    return {
        "setting": _COPILOT_BYOK_SETTING_KEY,
        "provider_label": _COPILOT_BYOK_PROVIDER_LABEL,
        "scope": "chat-and-agents-only",
        "limitation": _COPILOT_BYOK_CHAT_ONLY_LIMITATION,
        "reference": "https://code.visualstudio.com/docs/copilot/customization/language-models",
    }


def _copilot_byok_openai_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/v1") or normalized.endswith("/v1/chat/completions"):
        return normalized
    return f"{normalized}/v1"


def _copilot_byok_model_ids(*, default_alias: str, fallback_models_json: str) -> tuple[str, ...]:
    model_ids: list[str] = []
    try:
        parsed = json.loads(fallback_models_json)
    except json.JSONDecodeError:
        parsed = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str) and item.strip():
                model_ids.append(item.strip())
    if default_alias and default_alias not in model_ids:
        model_ids.insert(0, default_alias)
    return tuple(dict.fromkeys(model_ids))


def _copilot_byok_settings_json(
    *,
    base_url: str,
    api_key: str,
    default_alias: str,
    fallback_models_json: str,
) -> str:
    openai_base_url = _copilot_byok_openai_base_url(base_url)
    model_ids = _copilot_byok_model_ids(
        default_alias=default_alias, fallback_models_json=fallback_models_json
    )
    custom_models = {
        model_id: {
            "name": f"{_COPILOT_BYOK_PROVIDER_LABEL}: {model_id}",
            "model": model_id,
            "url": openai_base_url,
            "apiKey": api_key,
            "keyStorage": "dokploy-litellm",
            "requiresAPIKey": bool(api_key),
            "toolCalling": True,
            "vision": False,
            "thinking": False,
            "maxInputTokens": 131072,
            "maxOutputTokens": 8192,
        }
        for model_id in model_ids
    }
    settings = {
        _COPILOT_BYOK_SETTING_KEY: custom_models,
    }
    return json.dumps(settings, indent=2, sort_keys=True)


def _copilot_byok_compatibility_probe() -> str:
    return (
        f"official-byok-supported: target-setting={_COPILOT_BYOK_SETTING_KEY}; "
        "scope=chat-and-agents-only; "
        "requires VS Code/code-server build with Copilot Chat BYOK/OpenAI-compatible model support; "
        "no secrets required for this dry-run probe"
    )


def _coder_state_gap_diagnosis(state_files: tuple[str, ...]) -> str:
    present = set(state_files)
    missing = sorted(_CODER_STATE_REQUIRED_FOR_MODIFY - present)
    if not missing:
        return "state-complete: modify can perform normal non-destructive Coder template reseed"
    return (
        "state-incomplete: focused-coder-template-reseed-required; "
        f"missing={','.join(missing)}; do-not-reinstall"
    )


def _coder_live_template_update_command_bundle() -> tuple[str, ...]:
    return (
        "./bin/dokploy-wizard inspect-state --env-file ./.install.env --state-dir .dokploy-wizard-state",
        (
            "python3 - <<'PY'\n"
            "# Focused Coder template reseed only. Do not print DOKPLOY_API_KEY, Coder session tokens, or LiteLLM keys.\n"
            "# 1. Load .install.env and .dokploy-wizard-state.\n"
            "# 2. If applied-state.json and ownership-ledger.json exist, run non-interactive modify to invoke DokployCoderBackend.ensure_application_ready().\n"
            "# 3. If state is incomplete, authenticate to live Coder with redacted operator credentials, copy templates/coder/* into the running Coder container, and run /opt/coder templates push for the six wizard-managed templates only.\n"
            "# 4. Query template versions and assert github.copilot.chat.customOAIModels exists in all six payloads.\n"
            "print('focused-coder-template-reseed <redacted credentials>')\n"
            "PY"
        ),
    )


def _list_workspaces(*, container_name: str, hostname: str, session_token: str) -> tuple[str, ...]:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL={_coder_cli_url()}",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "list",
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to list Coder workspaces: {(result.stderr or result.stdout).strip()}"
        )
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise CoderError("Coder workspace list returned invalid JSON.") from exc
    if not isinstance(payload, list):
        raise CoderError("Coder workspace list returned an unexpected payload shape.")
    names: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def _list_templates(
    *, container_name: str, hostname: str, session_token: str
) -> tuple[dict[str, object], ...]:
    del hostname
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL={_coder_cli_url()}",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "templates",
            "list",
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to list Coder templates: {(result.stderr or result.stdout).strip()}"
        )
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise CoderError("Coder template list returned invalid JSON.") from exc
    if not isinstance(payload, list):
        raise CoderError("Coder template list returned an unexpected payload shape.")
    templates: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        template = item.get("Template")
        templates.append(template if isinstance(template, dict) else item)
    return tuple(templates)


def _active_template_version_name(
    *, container_name: str, hostname: str, session_token: str, template_name: str
) -> str | None:
    for item in _list_template_versions(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
        template_name=template_name,
    ):
        if item.get("active") is True:
            name = item.get("name")
            if isinstance(name, str) and name:
                return name
            return None
    return None


def _template_version_names(
    *, container_name: str, hostname: str, session_token: str, template_name: str
) -> tuple[str, ...]:
    names: list[str] = []
    for item in _list_template_versions(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
        template_name=template_name,
    ):
        name = item.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def _list_template_versions(
    *, container_name: str, hostname: str, session_token: str, template_name: str
) -> tuple[dict[str, object], ...]:
    template_exists = False
    for item in _list_templates(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
    ):
        name = item.get("name")
        if isinstance(name, str) and name == template_name:
            template_exists = True
            break
    if not template_exists:
        return ()
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL={_coder_cli_url()}",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "templates",
            "versions",
            "list",
            template_name,
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to list Coder template versions for '{template_name}': {(result.stderr or result.stdout).strip()}"
        )
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise CoderError(
            f"Coder template version list for '{template_name}' returned invalid JSON."
        ) from exc
    versions: list[dict[str, object]]
    if isinstance(payload, list):
        versions = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        raw_versions = payload.get("versions")
        if not isinstance(raw_versions, list):
            raise CoderError(
                f"Coder template version list for '{template_name}' returned an unexpected payload shape."
            )
        versions = [item for item in raw_versions if isinstance(item, dict)]
    else:
        raise CoderError(
            f"Coder template version list for '{template_name}' returned an unexpected payload shape."
        )
    return tuple(versions)


def _create_default_workspace(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    workspace_name: str,
    template_name: str,
) -> None:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL={_coder_cli_url()}",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "create",
            workspace_name,
            "--template",
            template_name,
            "--use-parameter-defaults",
            "--yes",
            "--no-wait",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to create default Coder workspace '{workspace_name}': {(result.stderr or result.stdout).strip()}"
        )


def _ensure_default_workspace(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    workspace_name: str,
    template_name: str,
) -> bool:
    if workspace_name in _list_workspaces(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
    ):
        return False
    _create_default_workspace(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
        workspace_name=workspace_name,
        template_name=template_name,
    )
    return True


def _username_from_email(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", local).strip("-")
    return normalized or "admin"


def _display_name_from_email(email: str) -> str:
    local = email.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ").strip()
    return local.title() or "Dokploy Admin"


@dataclass(frozen=True)
class _CoderHTTPError(RuntimeError):
    status: int
