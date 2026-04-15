"""Dokploy-backed OpenClaw and My Farm Advisor runtime backend."""

from __future__ import annotations

import base64
import json
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
from dokploy_wizard.packs.openclaw.models import OpenClawResourceRecord
from dokploy_wizard.packs.openclaw.reconciler import OpenClawError

_DEFAULT_MODEL_PROVIDER = "openai"
_DEFAULT_MODEL_NAME = "gpt-4o-mini"
_DEFAULT_TRUSTED_PROXIES = "127.0.0.1/32,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
_DEFAULT_NVIDIA_VISIBLE_DEVICES = "all"
_DEFAULT_APP_PORT = 18789
_MY_FARM_ADVISOR_PORT = 18789


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


class DokployOpenClawBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        stack_name: str,
        gateway_token: str | None = None,
        trusted_proxy_emails: tuple[str, ...] = (),
        openclaw_primary_model: str | None = None,
        openclaw_fallback_models: tuple[str, ...] = (),
        openclaw_openrouter_api_key: str | None = None,
        openclaw_nvidia_api_key: str | None = None,
        openclaw_telegram_bot_token: str | None = None,
        openclaw_telegram_owner_user_id: str | None = None,
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
        self._stack_name = stack_name
        self._runtime_configs = {
            "openclaw": _AdvisorRuntimeConfig(
                gateway_token=gateway_token,
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
            ),
            "my-farm-advisor": _AdvisorRuntimeConfig(
                gateway_token=gateway_token,
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
    suffix = "advisor" if variant == "openclaw" else "my-farm-advisor"
    return f"{stack_name}-{suffix}"


def _shared_network_name(stack_name: str) -> str:
    return f"{stack_name}-shared"


def _variant_from_service_name(stack_name: str, resource_name: str) -> str | None:
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
    stack_name = service_name.removesuffix("-advisor").removesuffix("-my-farm-advisor")
    shared_network = _shared_network_name(stack_name)
    channel_list = ",".join(channels)
    startup_mode = "advisor" if variant == "openclaw" else "my-farm-advisor"
    image = _image_for_variant(variant)
    labels = {
        "dokploy-wizard.slot": "advisor_suite",
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
        f"{_command_for_variant(variant=variant, hostname=hostname, app_port=app_port, runtime_config=runtime_config)}",
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
    if variant == "my-farm-advisor":
        volume_name = f"{service_name}-data"
        lines.extend(
            [
                '    user: "0:0"',
                "    volumes:",
                f"      - {volume_name}:/data",
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


def _health_path_for_variant(variant: str) -> str:
    del variant
    return "/healthz"


def _command_for_variant(
    *, variant: str, hostname: str, app_port: int, runtime_config: _AdvisorRuntimeConfig
) -> str:
    gateway_payload: dict[str, object] = {
        "bind": "lan",
        "mode": "local",
        "controlUi": {
            "allowedOrigins": [
                f"http://127.0.0.1:{app_port}",
                f"http://localhost:{app_port}",
                f"https://{hostname}",
            ],
            "allowInsecureAuth": True,
            "dangerouslyAllowHostHeaderOriginFallback": False,
        },
    }
    trusted_proxy_mode = bool(runtime_config.trusted_proxy_emails)
    if trusted_proxy_mode:
        gateway_payload["trustedProxies"] = [
            item.strip() for item in runtime_config.trusted_proxies.split(",") if item.strip()
        ]
        gateway_payload["auth"] = {
            "mode": "trusted-proxy",
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
    token_injection = (
        "if (process.env.OPENCLAW_GATEWAY_TOKEN) {"
        "payload.gateway = payload.gateway || {};"
        "payload.gateway.auth = payload.gateway.auth || {};"
        "payload.gateway.auth.token = process.env.OPENCLAW_GATEWAY_TOKEN;"
        "}"
        if runtime_config.gateway_token is not None and not trusted_proxy_mode
        else ""
    )
    if variant == "my-farm-advisor":
        seed_command = (
            "mkdir -p /data /data/.openclaw /data/workspace && "
            "node -e \"const fs=require('fs');"
            "const path=require('path');"
            f"const payload=JSON.parse(Buffer.from('{seeded_payload_b64}','base64').toString('utf8'));"
            f"{token_injection}"
            "const rendered=JSON.stringify(payload, null, 2)+'\\n';"
            "for (const target of ['/data/openclaw.json','/data/.openclaw/openclaw.json']) {"
            "fs.mkdirSync(path.dirname(target), {recursive:true});"
            "fs.writeFileSync(target, rendered);"
            '}"'
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
        "mkdir -p /home/node/.openclaw && "
        "node -e \"const fs=require('fs');"
        f"const payload=JSON.parse(Buffer.from('{seeded_payload_b64}','base64').toString('utf8'));"
        f"{token_injection}"
        "const rendered=JSON.stringify(payload, null, 2)+'\\n';"
        "fs.writeFileSync('/home/node/.openclaw/openclaw.json', rendered);\""
    )
    return json.dumps(
        [
            "sh",
            "-lc",
            f"{seed_command} && exec node openclaw.mjs gateway "
            f"--bind lan --port {app_port} --allow-unconfigured",
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
