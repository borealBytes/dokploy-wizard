"""Dokploy-backed OpenClaw and My Farm Advisor runtime backend."""

from __future__ import annotations

import base64
import json
import shlex
import ssl
import subprocess
from dataclasses import dataclass
from typing import Protocol
from urllib import error, parse, request

from dokploy_wizard.dokploy.client import (
    DokployApiClient,
    DokployApiError,
    DokployComposeRecord,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.packs.openclaw.models import (
    OpenClawNexaDeploymentContract,
    OpenClawResourceRecord,
)
from dokploy_wizard.packs.openclaw.reconciler import OpenClawError

_DEFAULT_MODEL_PROVIDER = "openai"
_DEFAULT_MODEL_NAME = "gpt-4o-mini"
_DEFAULT_TRUSTED_PROXIES = "127.0.0.1/32,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
_DEFAULT_NVIDIA_VISIBLE_DEVICES = "all"
_DEFAULT_APP_PORT = 18789
_MY_FARM_ADVISOR_PORT = 18789
_DEFAULT_NEXA_OPENCLAW_WORKSPACE_ROOT = "/home/node/.openclaw/workspace/nexa"
_DEFAULT_NEXA_RUNTIME_CONTRACT_PATH = "/home/node/.openclaw/.nexa/runtime-contract.json"
_DEFAULT_NEXA_WORKSPACE_CONTRACT_PATH = f"{_DEFAULT_NEXA_OPENCLAW_WORKSPACE_ROOT}/contract.json"
_DEFAULT_NEXA_WORKSPACE_README_PATH = f"{_DEFAULT_NEXA_OPENCLAW_WORKSPACE_ROOT}/README.md"
_DEFAULT_NEXA_VISIBLE_WORKSPACE_ROOT = "/mnt/openclaw/workspace/nexa"
_DEFAULT_NEXA_RUNTIME_VOLUME_ROOT = "/mnt/openclaw"
_DEFAULT_NEXA_RUNTIME_STATE_DIR = f"{_DEFAULT_NEXA_RUNTIME_VOLUME_ROOT}/.nexa/state"
_DEFAULT_NEXA_DEPLOYMENT_MODE = "sidecar"
_DEFAULT_NEXA_RUNTIME_SERVICE_NAME = "nexa-runtime"
_DEFAULT_NEXA_MEM0_SERVICE_NAME = "mem0"
_DEFAULT_NEXA_MEM0_PORT = 8000
_DEFAULT_NEXA_QDRANT_SERVICE_NAME = "qdrant"
_DEFAULT_NEXA_QDRANT_PORT = 6333
_DEFAULT_NEXA_RUNTIME_IMAGE = "local/dokploy-wizard-nexa-runtime:latest"
_DEFAULT_NEXA_RUNTIME_BUILD_CONTEXT = "."
_DEFAULT_NEXA_RUNTIME_DOCKERFILE = "docker/nexa-runtime/Dockerfile"


class DokployOpenClawApi(Protocol):
    def list_projects(self) -> tuple[DokployProjectSummary, ...]: ...

    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject: ...

    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord: ...

    def update_compose(self, *, compose_id: str, compose_file: str) -> DokployComposeRecord: ...

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult: ...


@dataclass(frozen=True)
class _ComposeLocator:
    project_id: str
    environment_id: str
    compose_id: str


@dataclass(frozen=True)
class _AdvisorRuntimeConfig:
    gateway_token: str | None
    gateway_password: str | None
    trusted_proxy_emails: tuple[str, ...]
    primary_model: str | None
    fallback_models: tuple[str, ...]
    openrouter_api_key: str | None
    nvidia_api_key: str | None
    telegram_bot_token: str | None
    telegram_owner_user_id: str | None
    model_provider: str
    model_name: str
    trusted_proxies: str
    nvidia_visible_devices: str
    nexa_env: dict[str, str]
    nexa_contract: OpenClawNexaDeploymentContract | None


class DokployOpenClawBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        stack_name: str,
        gateway_token: str | None = None,
        openclaw_gateway_password: str | None = None,
        my_farm_gateway_password: str | None = None,
        trusted_proxy_emails: tuple[str, ...] = (),
        openclaw_primary_model: str | None = None,
        openclaw_fallback_models: tuple[str, ...] = (),
        openclaw_openrouter_api_key: str | None = None,
        openclaw_nvidia_api_key: str | None = None,
        openclaw_telegram_bot_token: str | None = None,
        openclaw_telegram_owner_user_id: str | None = None,
        openclaw_nexa_env: dict[str, str] | None = None,
        my_farm_primary_model: str | None = None,
        my_farm_fallback_models: tuple[str, ...] = (),
        my_farm_openrouter_api_key: str | None = None,
        my_farm_nvidia_api_key: str | None = None,
        my_farm_telegram_bot_token: str | None = None,
        my_farm_telegram_owner_user_id: str | None = None,
        model_provider: str = _DEFAULT_MODEL_PROVIDER,
        model_name: str = _DEFAULT_MODEL_NAME,
        trusted_proxies: str = _DEFAULT_TRUSTED_PROXIES,
        nvidia_visible_devices: str = _DEFAULT_NVIDIA_VISIBLE_DEVICES,
        client: DokployOpenClawApi | None = None,
    ) -> None:
        resolved_openclaw_nexa_env = _resolve_openclaw_nexa_env(openclaw_nexa_env or {})
        self._stack_name = stack_name
        self._runtime_configs = {
            "openclaw": _AdvisorRuntimeConfig(
                gateway_token=gateway_token,
                gateway_password=openclaw_gateway_password,
                trusted_proxy_emails=trusted_proxy_emails,
                primary_model=openclaw_primary_model,
                fallback_models=openclaw_fallback_models,
                openrouter_api_key=openclaw_openrouter_api_key,
                nvidia_api_key=openclaw_nvidia_api_key,
                telegram_bot_token=openclaw_telegram_bot_token,
                telegram_owner_user_id=openclaw_telegram_owner_user_id,
                model_provider=model_provider,
                model_name=model_name,
                trusted_proxies=trusted_proxies,
                nvidia_visible_devices=nvidia_visible_devices,
                nexa_env=resolved_openclaw_nexa_env,
                nexa_contract=_build_nexa_deployment_contract(resolved_openclaw_nexa_env),
            ),
            "my-farm-advisor": _AdvisorRuntimeConfig(
                gateway_token=gateway_token,
                gateway_password=my_farm_gateway_password,
                trusted_proxy_emails=trusted_proxy_emails,
                primary_model=my_farm_primary_model,
                fallback_models=my_farm_fallback_models,
                openrouter_api_key=my_farm_openrouter_api_key,
                nvidia_api_key=my_farm_nvidia_api_key,
                telegram_bot_token=my_farm_telegram_bot_token,
                telegram_owner_user_id=my_farm_telegram_owner_user_id,
                model_provider=model_provider,
                model_name=model_name,
                trusted_proxies=trusted_proxies,
                nvidia_visible_devices=nvidia_visible_devices,
                nexa_env={},
                nexa_contract=None,
            ),
        }
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)

    def get_service(self, resource_id: str) -> OpenClawResourceRecord | None:
        parsed = _parse_resource_id(resource_id)
        if parsed is None:
            return None
        compose_id, variant, replicas = parsed
        locator = self._find_compose_locator(_service_name(self._stack_name, variant))
        if locator is None or locator.compose_id != compose_id:
            return None
        return OpenClawResourceRecord(
            resource_id=resource_id,
            resource_name=_service_name(self._stack_name, variant),
            replicas=replicas,
        )

    def find_service_by_name(self, resource_name: str) -> OpenClawResourceRecord | None:
        variant = _variant_from_service_name(self._stack_name, resource_name)
        if variant is None:
            return None
        locator = self._find_compose_locator(resource_name)
        if locator is None:
            return None
        return OpenClawResourceRecord(
            resource_id=_resource_id(locator.compose_id, variant, 1),
            resource_name=resource_name,
            replicas=1,
        )

    def create_service(
        self,
        *,
        resource_name: str,
        hostname: str,
        template_path: object,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
        secret_refs: tuple[str, ...],
    ) -> OpenClawResourceRecord:
        del template_path
        self._validate_inputs(
            resource_name=resource_name,
            hostname=hostname,
            variant=variant,
            channels=channels,
            replicas=replicas,
            secret_refs=secret_refs,
        )
        locator = self._ensure_compose_applied(
            resource_name=resource_name,
            hostname=hostname,
            variant=variant,
            channels=channels,
            replicas=replicas,
        )
        return OpenClawResourceRecord(
            resource_id=_resource_id(locator.compose_id, variant, replicas),
            resource_name=resource_name,
            replicas=replicas,
        )

    def update_service(
        self,
        *,
        resource_id: str,
        resource_name: str,
        hostname: str,
        template_path: object,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
        secret_refs: tuple[str, ...],
    ) -> OpenClawResourceRecord:
        del resource_id, template_path
        return self.create_service(
            resource_name=resource_name,
            hostname=hostname,
            template_path=None,
            variant=variant,
            channels=channels,
            replicas=replicas,
            secret_refs=secret_refs,
        )

    def check_health(self, *, service: OpenClawResourceRecord, url: str) -> bool:
        if not _docker_container_is_up(service.resource_name):
            return _local_https_health_check(url)
        if not _control_ui_origin_ready(service.resource_name, url):
            return False
        return _local_https_health_check(url)

    def _validate_inputs(
        self,
        *,
        resource_name: str,
        hostname: str,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
        secret_refs: tuple[str, ...],
    ) -> None:
        if variant not in {"openclaw", "my-farm-advisor"}:
            raise OpenClawError(f"Unsupported advisor variant '{variant}'.")
        if resource_name != _service_name(self._stack_name, variant):
            raise OpenClawError("Advisor service name does not match the active Dokploy plan.")
        if not hostname:
            raise OpenClawError("Advisor hostname cannot be empty.")
        if not channels:
            raise OpenClawError("Advisor channels cannot be empty.")
        if replicas < 1:
            raise OpenClawError("Advisor replicas must be a positive integer.")
        if secret_refs:
            raise OpenClawError("Advisor secret refs are not modeled for the Dokploy backend.")

    def _find_compose_locator(self, resource_name: str) -> _ComposeLocator | None:
        try:
            projects = self._client.list_projects()
        except DokployApiError as error:
            raise OpenClawError(str(error)) from error
        for project in projects:
            if project.name != self._stack_name:
                continue
            environment = _pick_environment(project)
            if environment is None:
                continue
            for compose in environment.composes:
                if compose.name == resource_name:
                    return _ComposeLocator(
                        project_id=project.project_id,
                        environment_id=environment.environment_id,
                        compose_id=compose.compose_id,
                    )
        return None

    def _ensure_compose_applied(
        self,
        *,
        resource_name: str,
        hostname: str,
        variant: str,
        channels: tuple[str, ...],
        replicas: int,
    ) -> _ComposeLocator:
        compose_file = _render_compose_file(
            service_name=resource_name,
            hostname=hostname,
            variant=variant,
            channels=channels,
            replicas=replicas,
            runtime_config=self._runtime_configs[variant],
        )
        try:
            projects = self._client.list_projects()
            for project in projects:
                if project.name != self._stack_name:
                    continue
                environment = _pick_environment(project)
                if environment is None:
                    break
                for compose in environment.composes:
                    if compose.name == resource_name:
                        updated = self._client.update_compose(
                            compose_id=compose.compose_id,
                            compose_file=compose_file,
                        )
                        self._client.deploy_compose(
                            compose_id=updated.compose_id,
                            title=f"dokploy-wizard {variant} reconcile",
                            description=f"Update {variant} compose app",
                        )
                        return _ComposeLocator(
                            project_id=project.project_id,
                            environment_id=environment.environment_id,
                            compose_id=updated.compose_id,
                        )
                created = self._client.create_compose(
                    name=resource_name,
                    environment_id=environment.environment_id,
                    compose_file=compose_file,
                    app_name=resource_name,
                )
                self._client.deploy_compose(
                    compose_id=created.compose_id,
                    title=f"dokploy-wizard {variant} reconcile",
                    description=f"Create {variant} compose app",
                )
                return _ComposeLocator(
                    project_id=project.project_id,
                    environment_id=environment.environment_id,
                    compose_id=created.compose_id,
                )

            created_project = self._client.create_project(
                name=self._stack_name,
                description="Managed by dokploy-wizard",
                env=None,
            )
            created_compose = self._client.create_compose(
                name=resource_name,
                environment_id=created_project.environment_id,
                compose_file=compose_file,
                app_name=resource_name,
            )
            self._client.deploy_compose(
                compose_id=created_compose.compose_id,
                title=f"dokploy-wizard {variant} reconcile",
                description=f"Create {variant} compose app",
            )
        except DokployApiError as error:
            raise OpenClawError(str(error)) from error
        return _ComposeLocator(
            project_id=created_project.project_id,
            environment_id=created_project.environment_id,
            compose_id=created_compose.compose_id,
        )


def _service_name(stack_name: str, variant: str) -> str:
    suffix = "openclaw" if variant == "openclaw" else "my-farm-advisor"
    return f"{stack_name}-{suffix}"


def _shared_network_name(stack_name: str) -> str:
    return f"{stack_name}-shared"


def _variant_from_service_name(stack_name: str, resource_name: str) -> str | None:
    if resource_name == f"{stack_name}-openclaw":
        return "openclaw"
    if resource_name == f"{stack_name}-advisor":
        return "openclaw"
    if resource_name == f"{stack_name}-my-farm-advisor":
        return "my-farm-advisor"
    return None


def _resource_id(compose_id: str, variant: str, replicas: int) -> str:
    return f"dokploy-compose:{compose_id}:{variant}:replicas:{replicas}"


def _parse_resource_id(resource_id: str) -> tuple[str, str, int] | None:
    prefix = "dokploy-compose:"
    middle = ":replicas:"
    if not resource_id.startswith(prefix):
        return None
    if middle not in resource_id:
        payload = resource_id.removeprefix(prefix)
        compose_id, _, legacy_kind = payload.partition(":")
        if not compose_id or not legacy_kind:
            return None
        if legacy_kind == "advisor-service":
            return compose_id, "openclaw", 1
        return None
    payload = resource_id.removeprefix(prefix)
    compose_variant, _, raw_replicas = payload.rpartition(middle)
    compose_id, _, variant = compose_variant.partition(":")
    if not compose_id or not variant:
        return None
    try:
        replicas = int(raw_replicas)
    except ValueError:
        return None
    if replicas < 1:
        return None
    return compose_id, variant, replicas


def _pick_environment(project: DokployProjectSummary) -> DokployEnvironmentSummary | None:
    if not project.environments:
        return None
    for environment in project.environments:
        if environment.is_default:
            return environment
    return project.environments[0]


def _docker_container_is_up(service_name: str) -> bool:
    result = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        name, _, status = line.partition("\t")
        if service_name not in name:
            continue
        return status.startswith("Up ")
    return False


def _control_ui_origin_ready(service_name: str, url: str) -> bool:
    parsed = parse.urlsplit(url)
    if not parsed.hostname:
        return False
    container_name = _find_container_name(service_name)
    if container_name is None:
        return False
    result = subprocess.run(
        [
            "docker",
            "exec",
            container_name,
            "sh",
            "-lc",
            "cat /home/node/.openclaw/openclaw.json 2>/dev/null "
            "|| cat /data/.openclaw/openclaw.json 2>/dev/null || true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stdout.strip() == "":
        return False
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    origins = payload.get("gateway", {}).get("controlUi", {}).get("allowedOrigins", [])
    if not isinstance(origins, list):
        return False
    return f"https://{parsed.hostname}" in origins


def _find_container_name(service_name: str) -> str | None:
    result = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if service_name in line:
            return line
    return None


def _local_https_health_check(url: str) -> bool:
    parsed = parse.urlsplit(url)
    if not parsed.hostname:
        return False
    request_path = parsed.path or "/"
    if parsed.query:
        request_path = f"{request_path}?{parsed.query}"
    req = request.Request(
        f"https://127.0.0.1{request_path}",
        headers={"Host": parsed.hostname},
        method="GET",
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with request.urlopen(req, timeout=15, context=context):  # noqa: S310
            return True
    except error.HTTPError as exc:
        return exc.code < 500
    except (error.URLError, TimeoutError):
        return False


def _render_compose_file(
    *,
    service_name: str,
    hostname: str,
    variant: str,
    channels: tuple[str, ...],
    replicas: int,
    runtime_config: _AdvisorRuntimeConfig,
) -> str:
    app_port = _app_port_for_variant(variant)
    stack_name = (
        service_name.removesuffix("-openclaw")
        .removesuffix("-advisor")
        .removesuffix("-my-farm-advisor")
    )
    shared_network = _shared_network_name(stack_name)
    channel_list = ",".join(channels)
    startup_mode = "advisor" if variant == "openclaw" else "my-farm-advisor"
    image = _image_for_variant(variant)
    slot_name = "openclaw_suite" if variant == "openclaw" else "my-farm-advisor_suite"
    nexa_sidecars_enabled = variant == "openclaw" and _openclaw_nexa_sidecars_enabled(runtime_config)
    labels = {
        "dokploy-wizard.slot": slot_name,
        "dokploy-wizard.variant": variant,
        "traefik.enable": "true",
        f"traefik.http.routers.{service_name}.entrypoints": "websecure",
        f"traefik.http.routers.{service_name}.rule": f"Host(`{hostname}`)",
        f"traefik.http.routers.{service_name}.tls": "true",
        f"traefik.http.services.{service_name}.loadbalancer.server.port": str(app_port),
    }
    environment = {
        "ADVISOR_VARIANT": variant,
        "ADVISOR_CHANNELS": channel_list,
        "ADVISOR_CANONICAL_HOSTNAME": hostname,
        "ADVISOR_CANONICAL_URL": f"https://{hostname}",
        "ADVISOR_PUBLIC_URL": f"https://{hostname}",
        "ADVISOR_STARTUP_MODE": startup_mode,
        "CONTROL_UI_ALLOWED_ORIGINS": f"https://{hostname}",
        "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
        "NVIDIA_VISIBLE_DEVICES": runtime_config.nvidia_visible_devices,
        "PORT": str(app_port),
        "TRUSTED_PROXIES": runtime_config.trusted_proxies,
    }
    if runtime_config.primary_model is None and not runtime_config.fallback_models:
        environment["MODEL_PROVIDER"] = runtime_config.model_provider
        environment["MODEL_NAME"] = runtime_config.model_name
    if runtime_config.gateway_token is not None and not runtime_config.trusted_proxy_emails:
        environment["OPENCLAW_GATEWAY_TOKEN"] = runtime_config.gateway_token
    if runtime_config.openrouter_api_key is not None:
        environment["OPENROUTER_API_KEY"] = runtime_config.openrouter_api_key
    if runtime_config.nvidia_api_key is not None:
        environment["NVIDIA_API_KEY"] = runtime_config.nvidia_api_key
    if runtime_config.telegram_bot_token is not None:
        environment["TELEGRAM_BOT_TOKEN"] = runtime_config.telegram_bot_token
    if variant == "openclaw" and runtime_config.nexa_contract is not None:
        environment.update(runtime_config.nexa_env)
        environment.update(
            {
                "DOKPLOY_WIZARD_NEXA_ENABLED": "true",
                "DOKPLOY_WIZARD_NEXA_DEPLOYMENT_MODE": runtime_config.nexa_contract.deployment_mode,
                "DOKPLOY_WIZARD_NEXA_MEM0_MODE": runtime_config.nexa_contract.mem0_mode,
                "DOKPLOY_WIZARD_NEXA_CREDENTIAL_MEDIATION_MODE": (
                    runtime_config.nexa_contract.credential_mediation_mode
                ),
                "DOKPLOY_WIZARD_NEXA_RUNTIME_CONTRACT_PATH": (
                    runtime_config.nexa_contract.runtime_contract_path
                ),
                "DOKPLOY_WIZARD_NEXA_WORKSPACE_ROOT": runtime_config.nexa_contract.workspace_root,
                "DOKPLOY_WIZARD_NEXA_WORKSPACE_CONTRACT_PATH": (
                    runtime_config.nexa_contract.workspace_contract_path
                ),
                "DOKPLOY_WIZARD_NEXA_VISIBLE_WORKSPACE_ROOT": _DEFAULT_NEXA_VISIBLE_WORKSPACE_ROOT,
            }
        )
    if variant == "my-farm-advisor":
        environment.update(
            {
                "HOME": "/data",
                "OPENCLAW_STATE_DIR": "/data",
                "OPENCLAW_WORKSPACE_DIR": "/data/workspace",
            }
        )
    lines = [
        "version: '3.9'",
        "services:",
        f"  {service_name}:",
        f"    image: {image}",
        "    restart: unless-stopped",
        "    command: "
        f"{_command_for_variant(variant=variant, hostname=hostname, app_port=app_port, channels=channels, runtime_config=runtime_config)}",
        "    environment:",
        *[f"      {key}: {_yaml_quote(value)}" for key, value in environment.items()],
        "    labels:",
        *[f"      {key}: {_yaml_quote(value)}" for key, value in labels.items()],
        "    expose:",
        f"      - '{app_port}'",
        "    healthcheck:",
        (
            "      test: ['CMD-SHELL', 'wget -q -O- "
            f"http://127.0.0.1:{app_port}{_health_path_for_variant(variant)} "
            ">/dev/null']"
        ),
        "      interval: 30s",
        "      timeout: 5s",
        "      retries: 5",
    ]
    if nexa_sidecars_enabled:
        lines.extend(
            [
                "    depends_on:",
                f"      - {_DEFAULT_NEXA_MEM0_SERVICE_NAME}",
                f"      - {_DEFAULT_NEXA_QDRANT_SERVICE_NAME}",
            ]
        )
    if variant == "my-farm-advisor":
        volume_name = f"{service_name}-data"
        lines.extend(
            [
                '    user: "0:0"',
                "    volumes:",
                f"      - {volume_name}:/data",
            ]
        )
    elif variant == "openclaw":
        volume_name = _openclaw_data_volume_name(stack_name)
        lines.extend(
            [
                '    user: "0:0"',
                "    volumes:",
                f"      - {volume_name}:/home/node/.openclaw",
            ]
        )
    lines.extend(
        [
            "    networks:",
            "      - default",
            "      - dokploy-network",
            f"      - {shared_network}",
            "    deploy:",
            f"      replicas: {replicas}",
        ]
    )
    if variant == "my-farm-advisor":
        lines.extend(["volumes:", f"  {service_name}-data:"])
    elif variant == "openclaw":
        if nexa_sidecars_enabled:
            lines.extend(
                _render_openclaw_nexa_sidecar_services(
                    stack_name,
                    runtime_config,
                    service_name=service_name,
                )
            )
        lines.extend(["volumes:"])
        for volume in _openclaw_named_volumes(stack_name, include_nexa_sidecars=nexa_sidecars_enabled):
            lines.extend([f"  {volume}:", f"    name: {volume}"])
    lines.extend(
        [
            "networks:",
            "  dokploy-network:",
            "    external: true",
            f"  {shared_network}:",
            "    external: true",
        ]
    )
    return "\n".join(lines) + "\n"


def _app_port_for_variant(variant: str) -> int:
    if variant == "my-farm-advisor":
        return _MY_FARM_ADVISOR_PORT
    return _DEFAULT_APP_PORT


def _image_for_variant(variant: str) -> str:
    if variant == "my-farm-advisor":
        return "ghcr.io/borealbytes/my-farm-advisor:latest"
    return "ghcr.io/openclaw/openclaw:latest"


def _openclaw_data_volume_name(stack_name: str) -> str:
    return f"{stack_name}-openclaw-data"


def _nexa_mem0_history_volume_name(stack_name: str) -> str:
    return f"{stack_name}-openclaw-mem0-history"


def _nexa_qdrant_data_volume_name(stack_name: str) -> str:
    return f"{stack_name}-openclaw-qdrant-data"


def _nexa_mem0_base_url() -> str:
    return f"http://{_DEFAULT_NEXA_MEM0_SERVICE_NAME}:{_DEFAULT_NEXA_MEM0_PORT}"


def _nexa_qdrant_base_url() -> str:
    return f"http://{_DEFAULT_NEXA_QDRANT_SERVICE_NAME}:{_DEFAULT_NEXA_QDRANT_PORT}"


def _nexa_runtime_volume_path(path: str) -> str:
    openclaw_root = "/home/node/.openclaw"
    if path == openclaw_root:
        return _DEFAULT_NEXA_RUNTIME_VOLUME_ROOT
    prefix = f"{openclaw_root}/"
    if path.startswith(prefix):
        return f"{_DEFAULT_NEXA_RUNTIME_VOLUME_ROOT}/{path.removeprefix(prefix)}"
    return path


def _openclaw_named_volumes(stack_name: str, *, include_nexa_sidecars: bool) -> tuple[str, ...]:
    volumes = [_openclaw_data_volume_name(stack_name)]
    if include_nexa_sidecars:
        volumes.extend(
            [
                _nexa_qdrant_data_volume_name(stack_name),
                _nexa_mem0_history_volume_name(stack_name),
            ]
        )
    return tuple(volumes)


def _openclaw_nexa_sidecars_enabled(runtime_config: _AdvisorRuntimeConfig) -> bool:
    return (
        runtime_config.nexa_contract is not None
        and runtime_config.nexa_contract.deployment_mode == _DEFAULT_NEXA_DEPLOYMENT_MODE
    )


def _render_openclaw_nexa_sidecar_services(
    stack_name: str,
    runtime_config: _AdvisorRuntimeConfig,
    *,
    service_name: str,
) -> list[str]:
    mem0_config_json = json.dumps(_build_mem0_sidecar_config(runtime_config.nexa_env), separators=(",", ":"))
    mem0_environment = {
        "HISTORY_DB_PATH": "/app/history/history.db",
        "MEM0_DEFAULT_CONFIG_JSON": mem0_config_json,
        "PYTHONUNBUFFERED": "1",
    }
    mem0_api_key = runtime_config.nexa_env.get("OPENCLAW_NEXA_MEM0_API_KEY")
    if mem0_api_key is not None:
        mem0_environment["ADMIN_API_KEY"] = mem0_api_key
    llm_api_key = runtime_config.nexa_env.get("OPENCLAW_NEXA_MEM0_LLM_API_KEY")
    if llm_api_key is not None:
        mem0_environment["OPENAI_API_KEY"] = llm_api_key
    qdrant_environment = {}
    qdrant_api_key = runtime_config.nexa_env.get("OPENCLAW_NEXA_MEM0_VECTOR_API_KEY")
    if qdrant_api_key is not None:
        qdrant_environment["QDRANT__SERVICE__API_KEY"] = qdrant_api_key
    if runtime_config.nexa_contract is None:
        msg = "Expected a Nexa deployment contract before rendering sidecar services."
        raise OpenClawError(msg)
    runtime_environment = {
        **runtime_config.nexa_env,
        "DOKPLOY_WIZARD_NEXA_RUNTIME_CONTRACT_PATH": _nexa_runtime_volume_path(
            runtime_config.nexa_contract.runtime_contract_path
        ),
        "DOKPLOY_WIZARD_NEXA_STATE_DIR": runtime_config.nexa_contract.runtime_state_dir,
        "DOKPLOY_WIZARD_NEXA_WORKER_MODE": "queue",
        "DOKPLOY_WIZARD_NEXA_WORKSPACE_CONTRACT_PATH": _nexa_runtime_volume_path(
            runtime_config.nexa_contract.workspace_contract_path
        ),
        "DOKPLOY_WIZARD_NEXA_WORKSPACE_ROOT": _nexa_runtime_volume_path(
            runtime_config.nexa_contract.workspace_root
        ),
        "PYTHONUNBUFFERED": "1",
    }
    lines = [
        f"  {_DEFAULT_NEXA_QDRANT_SERVICE_NAME}:",
        "    image: qdrant/qdrant",
        "    restart: unless-stopped",
        "    volumes:",
        f"      - {_nexa_qdrant_data_volume_name(stack_name)}:/qdrant/storage",
        "    networks:",
        "      - default",
    ]
    if qdrant_environment:
        lines.extend(
            [
                "    environment:",
                *[
                    f"      {key}: {_yaml_quote(value)}"
                    for key, value in sorted(qdrant_environment.items())
                ],
            ]
        )
    lines.extend(
        [
            f"  {_DEFAULT_NEXA_MEM0_SERVICE_NAME}:",
            "    image: mem0/mem0-api-server",
            "    restart: unless-stopped",
            "    command: "
            f"{_render_mem0_sidecar_command()}",
            "    environment:",
            *[
                f"      {key}: {_yaml_quote(value)}"
                for key, value in sorted(mem0_environment.items())
            ],
            "    volumes:",
            f"      - {_nexa_mem0_history_volume_name(stack_name)}:/app/history",
            "    networks:",
            "      - default",
            "    depends_on:",
            f"      - {_DEFAULT_NEXA_QDRANT_SERVICE_NAME}",
            "    healthcheck:",
            "      test: ['CMD-SHELL', 'python -c \"import urllib.request; urllib.request.urlopen(\'http://127.0.0.1:8000/openapi.json\')\"']",
            "      interval: 30s",
            "      timeout: 5s",
            "      retries: 5",
            f"  {_DEFAULT_NEXA_RUNTIME_SERVICE_NAME}:",
            f"    image: {_DEFAULT_NEXA_RUNTIME_IMAGE}",
            "    build:",
            f"      context: {_DEFAULT_NEXA_RUNTIME_BUILD_CONTEXT}",
            f"      dockerfile: {_DEFAULT_NEXA_RUNTIME_DOCKERFILE}",
            "    restart: unless-stopped",
            "    environment:",
            *[
                f"      {key}: {_yaml_quote(value)}"
                for key, value in sorted(runtime_environment.items())
            ],
            "    volumes:",
            f"      - {_openclaw_data_volume_name(stack_name)}:{_DEFAULT_NEXA_RUNTIME_VOLUME_ROOT}",
            "    networks:",
            "      - default",
            "    depends_on:",
            f"      - {service_name}",
            f"      - {_DEFAULT_NEXA_MEM0_SERVICE_NAME}",
            f"      - {_DEFAULT_NEXA_QDRANT_SERVICE_NAME}",
            "    healthcheck:",
            "      test: ['CMD-SHELL', 'python -c \"import os; from pathlib import Path; contract=Path(os.environ[\\\"DOKPLOY_WIZARD_NEXA_RUNTIME_CONTRACT_PATH\\\"]); workspace=Path(os.environ[\\\"DOKPLOY_WIZARD_NEXA_WORKSPACE_CONTRACT_PATH\\\"]); state_dir=Path(os.environ[\\\"DOKPLOY_WIZARD_NEXA_STATE_DIR\\\"]); raise SystemExit(0 if contract.exists() and workspace.exists() and state_dir.exists() else 1)\"']",
            "      interval: 30s",
            "      timeout: 5s",
            "      retries: 5",
        ]
    )
    return lines


def _build_mem0_sidecar_config(nexa_env: dict[str, str]) -> dict[str, object]:
    vector_config: dict[str, object] = {
        "collection_name": "mem0",
        "embedding_model_dims": int(
            nexa_env.get("OPENCLAW_NEXA_MEM0_VECTOR_DIMENSIONS", "384")
        ),
        "url": _nexa_qdrant_base_url(),
    }
    qdrant_api_key = nexa_env.get("OPENCLAW_NEXA_MEM0_VECTOR_API_KEY")
    if qdrant_api_key is not None:
        vector_config["api_key"] = qdrant_api_key
    llm_config: dict[str, object] = {}
    if nexa_env.get("OPENCLAW_NEXA_MEM0_LLM_API_KEY") is not None:
        llm_config["api_key"] = nexa_env["OPENCLAW_NEXA_MEM0_LLM_API_KEY"]
    if nexa_env.get("OPENCLAW_NEXA_MEM0_LLM_BASE_URL") is not None:
        llm_config["base_url"] = nexa_env["OPENCLAW_NEXA_MEM0_LLM_BASE_URL"]
    config: dict[str, object] = {
        "version": "v1.1",
        "vector_store": {
            "provider": "qdrant",
            "config": vector_config,
        },
        "llm": {
            "provider": "openai",
            "config": llm_config,
        },
        "embedder": {
            "provider": "huggingface",
            "config": {
                "model": nexa_env.get(
                    "OPENCLAW_NEXA_MEM0_EMBEDDER_MODEL", "BAAI/bge-small-en-v1.5"
                ),
                "embedding_dims": int(
                    nexa_env.get("OPENCLAW_NEXA_MEM0_EMBEDDER_DIMENSIONS", "384")
                ),
            },
        },
        "history_db_path": "/app/history/history.db",
    }
    return config


def _render_mem0_sidecar_command() -> str:
    bootstrap_script = (
        "import pathlib;"
        "source=pathlib.Path('/app/main.py').read_text(encoding='utf-8');"
        "start=source.index('DEFAULT_CONFIG = {');"
        "marker='\\n\\n\\nMEMORY_INSTANCE = Memory.from_config(DEFAULT_CONFIG)';"
        "end=source.index(marker);"
        "replacement='DEFAULT_CONFIG = json.loads(os.environ[\\\"MEM0_DEFAULT_CONFIG_JSON\\\"])';"
        "pathlib.Path('/app/sidecar_main.py').write_text(source[:start]+replacement+source[end:], encoding='utf-8')"
    )
    qdrant_wait_script = (
        "import time, urllib.request;"
        "deadline=time.time()+60;"
        "last_error=None;"
        "url='http://qdrant:6333/collections';"
        "while time.time()<deadline:"
        "\n  try: urllib.request.urlopen(url, timeout=2); raise SystemExit(0)"
        "\n  except Exception as exc: last_error=exc; time.sleep(1)"
        "\nraise SystemExit(f'Qdrant sidecar did not become ready: {last_error}')"
    )
    return json.dumps(
        [
            "sh",
            "-lc",
            (
                f"python -c {shlex.quote(bootstrap_script)} && "
                f"python -c {shlex.quote(qdrant_wait_script)} && "
                "exec uvicorn sidecar_main:app --host 0.0.0.0 --port 8000"
            ),
        ]
    )


def _health_path_for_variant(variant: str) -> str:
    del variant
    return "/healthz"


def _command_for_variant(
    *,
    variant: str,
    hostname: str,
    app_port: int,
    channels: tuple[str, ...],
    runtime_config: _AdvisorRuntimeConfig,
) -> str:
    trusted_proxy_mode = bool(runtime_config.trusted_proxy_emails)
    gateway_payload: dict[str, object] = {
        "bind": "lan",
        "mode": "local",
        "controlUi": {
            "allowedOrigins": [
                f"http://127.0.0.1:{app_port}",
                f"http://localhost:{app_port}",
                f"https://{hostname}",
            ],
            "allowInsecureAuth": not trusted_proxy_mode,
            "dangerouslyAllowHostHeaderOriginFallback": False,
        },
    }
    if trusted_proxy_mode:
        gateway_payload["trustedProxies"] = [
            item.strip() for item in runtime_config.trusted_proxies.split(",") if item.strip()
        ]
        gateway_payload["auth"] = {
            "mode": "trusted-proxy",
            "password": runtime_config.gateway_password,
            "trustedProxy": {
                "userHeader": "cf-access-authenticated-user-email",
                "requiredHeaders": ["cf-access-jwt-assertion"],
                "allowUsers": list(runtime_config.trusted_proxy_emails),
            },
        }
    else:
        gateway_payload["auth"] = {"mode": "token"}
    payload: dict[str, object] = {"gateway": gateway_payload}
    if runtime_config.primary_model is not None or runtime_config.fallback_models:
        allowed_models = _allowed_models(runtime_config)
        model_defaults: dict[str, object] = {}
        if runtime_config.primary_model is not None:
            model_defaults["primary"] = runtime_config.primary_model
        if runtime_config.fallback_models:
            model_defaults["fallbacks"] = list(runtime_config.fallback_models)
        payload["agents"] = {
            "defaults": {
                "model": model_defaults,
                "models": {model_ref: {} for model_ref in allowed_models},
            }
        }
    if runtime_config.nexa_contract is not None:
        payload["wizard"] = {
            "nexa": {
                **runtime_config.nexa_contract.to_dict(),
                "visible_workspace_root": _DEFAULT_NEXA_VISIBLE_WORKSPACE_ROOT,
            }
        }
    if "telegram" in channels:
        agents_payload = payload.setdefault("agents", {})
        if not isinstance(agents_payload, dict):
            agents_payload = {}
            payload["agents"] = agents_payload
        agents_list = agents_payload.setdefault(
            "list",
            [
                {"id": "main", "default": True},
                {"id": "telly", "name": "Telly"},
            ],
        )
        if not isinstance(agents_list, list):
            agents_list = []
            agents_payload["list"] = agents_list
        existing_ids = {
            item.get("id")
            for item in agents_list
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        if "main" not in existing_ids:
            agents_list.insert(0, {"id": "main", "default": True})
        if "telly" not in existing_ids:
            agents_list.append({"id": "telly", "name": "Telly"})
        bindings = payload.setdefault("bindings", [])
        if not isinstance(bindings, list):
            bindings = []
            payload["bindings"] = bindings
        if not any(
            isinstance(item, dict)
            and item.get("agentId") == "telly"
            and isinstance(item.get("match"), dict)
            and item.get("match", {}).get("channel") == "telegram"
            for item in bindings
        ):
            bindings.append({"agentId": "telly", "match": {"channel": "telegram"}})
        if runtime_config.telegram_bot_token is not None:
            telegram_config: dict[str, object] = {"botToken": runtime_config.telegram_bot_token}
            if runtime_config.telegram_owner_user_id is not None:
                telegram_config["dmPolicy"] = "allowlist"
                telegram_config["allowFrom"] = [runtime_config.telegram_owner_user_id]
            if trusted_proxy_mode:
                telegram_config["execApprovals"] = {"enabled": False}
            channels_payload = payload.setdefault("channels", {})
            if not isinstance(channels_payload, dict):
                channels_payload = {}
                payload["channels"] = channels_payload
            channels_payload["telegram"] = telegram_config
    seeded_payload = json.dumps(payload, indent=2) + "\n"
    seeded_payload_b64 = base64.b64encode(seeded_payload.encode("utf-8")).decode("ascii")
    extra_files = (
        _nexa_contract_files(runtime_config.nexa_contract, runtime_config.nexa_env)
        if variant == "openclaw"
        else {}
    )
    extra_files_payload = [
        {
            "path": path,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        for path, content in extra_files.items()
    ]
    extra_files_b64 = base64.b64encode(
        json.dumps(extra_files_payload, indent=2).encode("utf-8")
    ).decode("ascii")
    token_injection = (
        "if (process.env.OPENCLAW_GATEWAY_TOKEN) {"
        "payload.gateway = payload.gateway || {};"
        "payload.gateway.auth = payload.gateway.auth || {};"
        "payload.gateway.auth.token = process.env.OPENCLAW_GATEWAY_TOKEN;"
        "}"
        if runtime_config.gateway_token is not None and not trusted_proxy_mode
        else ""
    )
    node_script = _render_seed_script(
        seeded_payload_b64=seeded_payload_b64,
        token_injection=token_injection,
        extra_files_b64=extra_files_b64,
        config_targets=(
            ("/data/openclaw.json", "/data/.openclaw/openclaw.json")
            if variant == "my-farm-advisor"
            else ("/home/node/.openclaw/openclaw.json",)
        ),
    )
    if variant == "my-farm-advisor":
        seed_command = (
            "mkdir -p /data /data/.openclaw /data/workspace && "
            f"node -e {shlex.quote(node_script)}"
        )
        return json.dumps(
            [
                "sh",
                "-lc",
                (
                    f"{seed_command} && "
                    f"exec node openclaw.mjs gateway --bind lan --port {app_port} "
                    "--allow-unconfigured"
                ),
            ]
        )
    seed_command = (
        "mkdir -p /home/node/.openclaw /home/node/.openclaw/workspace "
        "/home/node/.openclaw/workspace/nexa /home/node/.openclaw/.nexa && "
        f"node -e {shlex.quote(node_script)}"
    )
    return json.dumps(
        [
            "sh",
            "-lc",
            f"umask 0000 && {seed_command} && chown -R node:node /home/node/.openclaw && chmod -R a+rwX /home/node/.openclaw && (while true; do chmod -R a+rwX /home/node/.openclaw 2>/dev/null || true; sleep 5; done) & "
            f"exec su -s /bin/sh node -c {json.dumps(f'umask 0000 && exec node openclaw.mjs gateway --bind lan --port {app_port} --allow-unconfigured')}",
        ]
    )


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _allowed_models(runtime_config: _AdvisorRuntimeConfig) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for model_ref in (
        (runtime_config.primary_model,) if runtime_config.primary_model is not None else ()
    ) + runtime_config.fallback_models:
        if model_ref in seen:
            continue
        seen.add(model_ref)
        ordered.append(model_ref)
    return tuple(ordered)


def _resolve_openclaw_nexa_env(nexa_env: dict[str, str]) -> dict[str, str]:
    if not nexa_env:
        return {}
    resolved = {key: value for key, value in nexa_env.items() if value.strip() != ""}
    deployment_mode = resolved.get("OPENCLAW_NEXA_DEPLOYMENT_MODE", _DEFAULT_NEXA_DEPLOYMENT_MODE)
    resolved["OPENCLAW_NEXA_DEPLOYMENT_MODE"] = deployment_mode
    if deployment_mode == _DEFAULT_NEXA_DEPLOYMENT_MODE:
        resolved["OPENCLAW_NEXA_MEM0_BASE_URL"] = _nexa_mem0_base_url()
        resolved["OPENCLAW_NEXA_MEM0_VECTOR_BACKEND"] = "qdrant"
        resolved["OPENCLAW_NEXA_MEM0_VECTOR_BASE_URL"] = _nexa_qdrant_base_url()
    return dict(sorted(resolved.items()))


def _build_nexa_deployment_contract(
    nexa_env: dict[str, str],
) -> OpenClawNexaDeploymentContract | None:
    if not nexa_env:
        return None
    deployment_mode = nexa_env.get("OPENCLAW_NEXA_DEPLOYMENT_MODE", _DEFAULT_NEXA_DEPLOYMENT_MODE)
    mem0_mode = "rest" if nexa_env.get("OPENCLAW_NEXA_MEM0_BASE_URL") else "library"
    topology_mode = "internal-compose-sidecars" if deployment_mode == "sidecar" else "external"
    notes = [
        "Nexa stays inside the existing OpenClaw service footprint; no separate agent pack is created.",
        "Credential-bearing Nexa settings remain server-owned environment variables and are not copied into the visible workspace surface.",
        "Nextcloud-visible workspace files are operator/user surfaces only; durable state JSON docs and server-owned env stay authoritative.",
    ]
    if deployment_mode == "sidecar":
        notes.append(
            "Mem0 and Qdrant run as internal-only sidecars on the same compose-default network as OpenClaw, with no Traefik labels or public port publishing."
        )
        notes.append(
            "The Mem0 sidecar is bootstrapped with an explicit Qdrant-backed server config at startup so it does not fall back to the image's pgvector default."
        )
    if mem0_mode == "rest":
        notes.append(
            "Mem0 REST mode requires private-network exposure and API-key auth; those assumptions are emitted as explicit deployment markers."
        )
    else:
        notes.append(
            "Mem0 REST wiring is absent, so deployment remains in library-mode assumptions until explicit REST env is provided."
        )
    return OpenClawNexaDeploymentContract(
        enabled=True,
        deployment_mode=deployment_mode,
        topology_mode=topology_mode,
        mem0_mode=mem0_mode,
        credential_mediation_mode="server-owned-env",
        runtime_contract_path=_DEFAULT_NEXA_RUNTIME_CONTRACT_PATH,
        runtime_service_name=(
            _DEFAULT_NEXA_RUNTIME_SERVICE_NAME if deployment_mode == "sidecar" else None
        ),
        runtime_state_dir=_DEFAULT_NEXA_RUNTIME_STATE_DIR,
        workspace_root=_DEFAULT_NEXA_OPENCLAW_WORKSPACE_ROOT,
        workspace_contract_path=_DEFAULT_NEXA_WORKSPACE_CONTRACT_PATH,
        internal_network_only=deployment_mode == "sidecar",
        mem0_service_name=(
            _DEFAULT_NEXA_MEM0_SERVICE_NAME if deployment_mode == "sidecar" else None
        ),
        mem0_base_url=nexa_env.get("OPENCLAW_NEXA_MEM0_BASE_URL"),
        qdrant_service_name=(
            _DEFAULT_NEXA_QDRANT_SERVICE_NAME if deployment_mode == "sidecar" else None
        ),
        qdrant_base_url=nexa_env.get("OPENCLAW_NEXA_MEM0_VECTOR_BASE_URL"),
        secret_env_keys=_nexa_secret_env_keys(nexa_env),
        notes=tuple(notes),
    )


def _nexa_secret_env_keys(nexa_env: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        key
        for key in sorted(nexa_env)
        if key.endswith("_API_KEY")
        or key.endswith("_PASSWORD")
        or key.endswith("_SECRET")
        or "SIGNING_SECRET" in key
    )


def _nexa_contract_files(
    contract: OpenClawNexaDeploymentContract | None,
    nexa_env: dict[str, str],
) -> dict[str, str]:
    if contract is None:
        return {}
    runtime_contract = {
        "nexa": contract.to_dict(),
        "credential_mediation": {
            "mode": contract.credential_mediation_mode,
            "secret_env": {
                key: {"present": key in nexa_env, "source": "server-owned-env"}
                for key in contract.secret_env_keys
            },
            "server_owned_runtime_inputs": {
                "nextcloud_base_url": {
                    "present": "OPENCLAW_NEXA_NEXTCLOUD_BASE_URL" in nexa_env,
                    "source": "server-owned-env",
                },
                "webdav_auth_user": {
                    "present": "OPENCLAW_NEXA_WEBDAV_AUTH_USER" in nexa_env,
                    "source": "server-owned-env",
                },
                "agent_user_id": {
                    "present": "OPENCLAW_NEXA_AGENT_USER_ID" in nexa_env,
                    "source": "server-owned-env",
                },
                "agent_display_name": {
                    "present": "OPENCLAW_NEXA_AGENT_DISPLAY_NAME" in nexa_env,
                    "source": "server-owned-env",
                },
            },
            "workspace_override_blocked_fields": [
                "agent_user_id",
                "agent_display_name",
                "credential_values",
                "secret_env",
                "task_identity",
            ],
        },
        "mem0": {
            "mode": contract.mem0_mode,
            "base_url": contract.mem0_base_url,
            "service_name": contract.mem0_service_name,
            "llm_base_url": nexa_env.get("OPENCLAW_NEXA_MEM0_LLM_BASE_URL"),
            "vector_backend": nexa_env.get("OPENCLAW_NEXA_MEM0_VECTOR_BACKEND"),
            "vector_base_url": contract.qdrant_base_url,
            "vector_service_name": contract.qdrant_service_name,
            "require_private_network": contract.internal_network_only,
            "require_api_key_auth": "OPENCLAW_NEXA_MEM0_API_KEY" in nexa_env,
        },
        "topology": {
            "mode": contract.topology_mode,
            "internal_network_only": contract.internal_network_only,
            "runtime_state_dir": contract.runtime_state_dir,
            "services": {
                "runtime": contract.runtime_service_name,
                "mem0": contract.mem0_service_name,
                "qdrant": contract.qdrant_service_name,
            },
        },
        "presence_policy": nexa_env.get("OPENCLAW_NEXA_PRESENCE_POLICY"),
        "nextcloud": {
            "base_url": nexa_env.get("OPENCLAW_NEXA_NEXTCLOUD_BASE_URL"),
            "talk_bot_auth": {
                "shared_secret_present": "OPENCLAW_NEXA_TALK_SHARED_SECRET" in nexa_env,
                "signing_secret_present": "OPENCLAW_NEXA_TALK_SIGNING_SECRET" in nexa_env,
                "source": "server-owned-env",
            },
            "webdav": {
                "auth_user_present": "OPENCLAW_NEXA_WEBDAV_AUTH_USER" in nexa_env,
                "auth_password_present": "OPENCLAW_NEXA_WEBDAV_AUTH_PASSWORD" in nexa_env,
                "source": "server-owned-env",
            },
        },
        "agent_identity": {
            "user_id_present": "OPENCLAW_NEXA_AGENT_USER_ID" in nexa_env,
            "display_name_present": "OPENCLAW_NEXA_AGENT_DISPLAY_NAME" in nexa_env,
            "source": "server-owned-env",
        },
        "workspace": {
            "visible_root": _DEFAULT_NEXA_VISIBLE_WORKSPACE_ROOT,
            "authoritative_runtime_state": "server-owned env + durable state JSON",
            "operator_surface_only": True,
        },
    }
    workspace_contract = {
        "surface": "operator-user-visible",
        "visible_root": _DEFAULT_NEXA_VISIBLE_WORKSPACE_ROOT,
        "contract_path": contract.workspace_contract_path,
        "authoritative_runtime_state": "server-owned env + durable state JSON",
        "files": {
            "briefing": "briefing.md",
            "memory": "memory.md",
            "status": "status.json",
            "tasks": "tasks.md",
        },
        "notes": list(contract.notes),
    }
    return {
        contract.runtime_contract_path: json.dumps(runtime_contract, indent=2) + "\n",
        contract.workspace_contract_path: json.dumps(workspace_contract, indent=2) + "\n",
        _DEFAULT_NEXA_WORKSPACE_README_PATH: (
            "# Nexa workspace\n\n"
            "This directory is a Nextcloud-visible operator/user surface for Nexa.\n"
            "It is not the sole runtime state source. Hidden server-owned env values and durable state JSON docs remain authoritative.\n\n"
            "User-editable files here must not override credentials, task identity, or agent identity fields.\n"
        ),
        f"{_DEFAULT_NEXA_OPENCLAW_WORKSPACE_ROOT}/briefing.md": (
            "# Briefing\n\nUse this file for operator-visible briefings only.\n"
        ),
        f"{_DEFAULT_NEXA_OPENCLAW_WORKSPACE_ROOT}/memory.md": (
            "# Memory surface\n\nSummaries here are optional user/operator legibility aids, not canonical memory state.\n"
        ),
        f"{_DEFAULT_NEXA_OPENCLAW_WORKSPACE_ROOT}/tasks.md": (
            "# Task surface\n\nTrack visible tasks here without treating this file as the authoritative job queue.\n"
        ),
        f"{_DEFAULT_NEXA_OPENCLAW_WORKSPACE_ROOT}/status.json": (
            json.dumps(
                {
                    "authoritative_runtime_state": "server-owned env + durable state JSON",
                    "operator_surface_only": True,
                    "visible_root": _DEFAULT_NEXA_VISIBLE_WORKSPACE_ROOT,
                },
                indent=2,
            )
            + "\n"
        ),
    }


def _render_seed_script(
    *,
    seeded_payload_b64: str,
    token_injection: str,
    extra_files_b64: str,
    config_targets: tuple[str, ...],
) -> str:
    return "".join(
        [
            'const fs=require("fs");',
            'const path=require("path");',
            (
                f'const payload=JSON.parse(Buffer.from("{seeded_payload_b64}","base64").toString("utf8"));'
            ),
            token_injection,
            'const rendered=JSON.stringify(payload, null, 2)+"\\n";',
            f"for (const target of {json.dumps(list(config_targets))}) {{",
            "fs.mkdirSync(path.dirname(target), {recursive:true});",
            "fs.writeFileSync(target, rendered);",
            "}",
            (
                f'for (const item of JSON.parse(Buffer.from("{extra_files_b64}","base64").toString("utf8"))) {{'
            ),
            "fs.mkdirSync(path.dirname(item.path), {recursive:true});",
            'fs.writeFileSync(item.path, Buffer.from(item.content,"base64").toString("utf8"));',
            "}",
        ]
    )
