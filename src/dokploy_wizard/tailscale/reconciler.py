"""Host-level Tailscale reconciliation."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol

from dokploy_wizard.state.models import DesiredState, OwnedResource, OwnershipLedger, RawEnvInput
from dokploy_wizard.tailscale.models import (
    TailscaleManagedResource,
    TailscaleNodeStatus,
    TailscalePhase,
    TailscaleResult,
)

TAILSCALE_NODE_RESOURCE_TYPE = "tailscale_node"
TAILSCALE_INSTALL_COMMAND = (
    "tmp=\"$(mktemp)\" && "
    "trap 'rm -f \"$tmp\"' EXIT && "
    'curl -fsSL https://tailscale.com/install.sh -o "$tmp" && '
    'sh "$tmp"'
)


class TailscaleError(RuntimeError):
    """Raised when Tailscale reconciliation fails."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class TailscaleBackend(Protocol):
    def get_node(self, resource_id: str) -> TailscaleManagedResource | None: ...

    def find_node_by_name(self, resource_name: str) -> TailscaleManagedResource | None: ...

    def apply(
        self,
        *,
        resource_name: str,
        auth_key: str,
        enable_ssh: bool,
        tags: tuple[str, ...],
        subnet_routes: tuple[str, ...],
    ) -> TailscaleManagedResource: ...

    def get_status(self, resource_name: str) -> TailscaleNodeStatus | None: ...

    def disconnect(self) -> None: ...


class CommandRunner(Protocol):
    def __call__(self, command: list[str]) -> CommandResult: ...


class ShellTailscaleBackend:
    """Subprocess-backed host Tailscale backend with test seams."""

    def __init__(self, raw_env: RawEnvInput, runner: CommandRunner | None = None) -> None:
        self._values = raw_env.values
        self._runner = runner or _run_command

    def get_node(self, resource_id: str) -> TailscaleManagedResource | None:
        status = self.get_status(resource_id)
        if status is None:
            return None
        return TailscaleManagedResource(
            action="reuse_owned",
            resource_id=resource_id,
            resource_name=resource_id,
        )

    def find_node_by_name(self, resource_name: str) -> TailscaleManagedResource | None:
        status = self.get_status(resource_name)
        if status is None:
            return None
        return TailscaleManagedResource(
            action="reuse_existing",
            resource_id=resource_name,
            resource_name=resource_name,
        )

    def apply(
        self,
        *,
        resource_name: str,
        auth_key: str,
        enable_ssh: bool,
        tags: tuple[str, ...],
        subnet_routes: tuple[str, ...],
    ) -> TailscaleManagedResource:
        if not self._is_installed():
            self._install()
        command = [
            "tailscale",
            "up",
            f"--auth-key={auth_key}",
            f"--hostname={resource_name}",
        ]
        if enable_ssh:
            command.append("--ssh")
        if tags:
            command.append(f"--advertise-tags={','.join(tags)}")
        if subnet_routes:
            command.append(f"--advertise-routes={','.join(subnet_routes)}")
        result = self._run_tailscale_command(command)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            msg = "tailscale up failed"
            if stderr:
                msg = f"{msg}: {stderr}"
            raise TailscaleError(msg)
        status = self.get_status(resource_name)
        if status is None or not status.online:
            raise TailscaleError(
                "tailscale up completed but Tailscale did not report an online node."
            )
        return TailscaleManagedResource(
            action="create",
            resource_id=resource_name,
            resource_name=resource_name,
        )

    def get_status(self, resource_name: str) -> TailscaleNodeStatus | None:
        status_result = self._run_tailscale_command(["tailscale", "status", "--json"])
        if status_result.returncode != 0:
            return None
        try:
            payload = json.loads(status_result.stdout)
        except json.JSONDecodeError as error:
            raise TailscaleError("tailscale status --json returned invalid JSON.") from error
        self_payload = payload.get("Self")
        if not isinstance(self_payload, dict):
            raise TailscaleError("tailscale status --json did not contain a Self object.")
        hostname = self_payload.get("HostName")
        if not isinstance(hostname, str) or hostname == "":
            raise TailscaleError("tailscale status --json did not contain a valid hostname.")
        if hostname != resource_name:
            return None
        online = bool(self_payload.get("Online", payload.get("BackendState") == "Running"))
        login_name = self_payload.get("LoginName")
        if login_name is not None and not isinstance(login_name, str):
            login_name = None
        ipv4 = _command_stdout_or_none(self._run_tailscale_command(["tailscale", "ip", "-4"]))
        ipv6 = _command_stdout_or_none(self._run_tailscale_command(["tailscale", "ip", "-6"]))
        return TailscaleNodeStatus(
            hostname=hostname,
            online=online,
            login_name=login_name,
            ipv4=ipv4,
            ipv6=ipv6,
        )

    def disconnect(self) -> None:
        result = self._run_tailscale_command(["tailscale", "down"])
        if result.returncode != 0:
            stderr = result.stderr.strip()
            msg = "tailscale down failed"
            if stderr:
                msg = f"{msg}: {stderr}"
            raise TailscaleError(msg)

    def _is_installed(self) -> bool:
        forced = self._values.get("TAILSCALE_MOCK_INSTALLED")
        if forced is not None:
            return forced.lower() in {"1", "true", "yes", "on"}
        return shutil.which("tailscale") is not None

    def _install(self) -> None:
        forced = self._values.get("TAILSCALE_MOCK_INSTALL_OK")
        if forced is not None:
            if forced.lower() in {"1", "true", "yes", "on"}:
                return
            raise TailscaleError("tailscale install command failed.")
        result = self._run_install_command(["sh", "-c", TAILSCALE_INSTALL_COMMAND])
        if result.returncode != 0:
            stderr = result.stderr.strip()
            msg = "tailscale install command failed"
            if stderr:
                msg = f"{msg}: {stderr}"
            raise TailscaleError(msg)
        try:
            verification = self._runner(["tailscale", "version"])
        except FileNotFoundError as error:
            raise TailscaleError(
                "tailscale install command reported success but the tailscale binary "
                "was not found on PATH afterward."
            ) from error
        if verification.returncode != 0:
            stderr = verification.stderr.strip()
            msg = "tailscale install verification failed"
            if stderr:
                msg = f"{msg}: {stderr}"
            raise TailscaleError(msg)

    def _run_tailscale_command(self, command: list[str]) -> CommandResult:
        try:
            return self._runner(command)
        except FileNotFoundError as error:
            raise TailscaleError(
                "tailscale command could not be executed because the tailscale "
                "binary was not found on PATH."
            ) from error

    def _run_install_command(self, command: list[str]) -> CommandResult:
        try:
            return self._runner(command)
        except FileNotFoundError as error:
            raise TailscaleError(
                "tailscale install command could not be executed on this host."
            ) from error


def reconcile_tailscale(
    *,
    dry_run: bool,
    raw_env: RawEnvInput,
    desired_state: DesiredState,
    ownership_ledger: OwnershipLedger,
    backend: TailscaleBackend,
) -> TailscalePhase:
    if not desired_state.enable_tailscale:
        return TailscalePhase(
            result=TailscaleResult(
                outcome="skipped",
                enabled=False,
                hostname=None,
                node=None,
                ssh_enabled=False,
                tags=(),
                subnet_routes=(),
                status=None,
                notes=("Tailscale private/admin access is disabled for this install.",),
            ),
            node_resource_id=None,
        )
    hostname = desired_state.tailscale_hostname
    if hostname is None:
        raise TailscaleError(
            "Desired state is missing tailscale_hostname while Tailscale is enabled."
        )
    auth_key = raw_env.values.get("TAILSCALE_AUTH_KEY")
    if auth_key is None:
        raise TailscaleError("TAILSCALE_AUTH_KEY is required when Tailscale is enabled.")
    owned_resource = _find_owned_resource(ownership_ledger, desired_state.stack_name)
    if owned_resource is not None:
        existing = backend.get_node(owned_resource.resource_id)
        if existing is None:
            raise TailscaleError(
                "Ownership ledger says the Tailscale node exists, "
                "but the backend could not find it."
            )
        if existing.resource_name != hostname:
            raise TailscaleError(
                "Ownership ledger Tailscale node no longer matches the desired hostname."
            )
        if dry_run:
            status = backend.get_status(hostname)
            if status is None:
                raise TailscaleError("Tailscale node exists but status verification failed.")
            return TailscalePhase(
                result=TailscaleResult(
                    outcome="plan_only",
                    enabled=True,
                    hostname=hostname,
                    node=existing,
                    ssh_enabled=desired_state.tailscale_enable_ssh,
                    tags=desired_state.tailscale_tags,
                    subnet_routes=desired_state.tailscale_subnet_routes,
                    status=status,
                    notes=("Tailscale configuration is already present and would be preserved.",),
                ),
                node_resource_id=None,
            )
        applied = backend.apply(
            resource_name=hostname,
            auth_key=auth_key,
            enable_ssh=desired_state.tailscale_enable_ssh,
            tags=desired_state.tailscale_tags,
            subnet_routes=desired_state.tailscale_subnet_routes,
        )
        status = backend.get_status(hostname)
        if status is None:
            raise TailscaleError("Tailscale apply completed but status verification failed.")
        return TailscalePhase(
            result=TailscaleResult(
                outcome="applied",
                enabled=True,
                hostname=hostname,
                node=applied,
                ssh_enabled=desired_state.tailscale_enable_ssh,
                tags=desired_state.tailscale_tags,
                subnet_routes=desired_state.tailscale_subnet_routes,
                status=status,
                notes=("Tailscale host access is configured and verified.",),
            ),
            node_resource_id=applied.resource_id,
        )
    collision = backend.find_node_by_name(hostname)
    if collision is not None:
        raise TailscaleError(f"Refusing to adopt existing unowned Tailscale node '{hostname}'.")
    if dry_run:
        planned_id = f"planned:{hostname}"
        return TailscalePhase(
            result=TailscaleResult(
                outcome="plan_only",
                enabled=True,
                hostname=hostname,
                node=TailscaleManagedResource(
                    action="create",
                    resource_id=planned_id,
                    resource_name=hostname,
                ),
                ssh_enabled=desired_state.tailscale_enable_ssh,
                tags=desired_state.tailscale_tags,
                subnet_routes=desired_state.tailscale_subnet_routes,
                status=None,
                notes=("Tailscale would be installed if needed and enrolled with tailscale up.",),
            ),
            node_resource_id=None,
        )
    applied = backend.apply(
        resource_name=hostname,
        auth_key=auth_key,
        enable_ssh=desired_state.tailscale_enable_ssh,
        tags=desired_state.tailscale_tags,
        subnet_routes=desired_state.tailscale_subnet_routes,
    )
    status = backend.get_status(hostname)
    if status is None:
        raise TailscaleError("Tailscale apply completed but status verification failed.")
    return TailscalePhase(
        result=TailscaleResult(
            outcome="applied",
            enabled=True,
            hostname=hostname,
            node=applied,
            ssh_enabled=desired_state.tailscale_enable_ssh,
            tags=desired_state.tailscale_tags,
            subnet_routes=desired_state.tailscale_subnet_routes,
            status=status,
            notes=("Tailscale host access is configured and verified.",),
        ),
        node_resource_id=applied.resource_id,
    )


def build_tailscale_ledger(
    *, existing_ledger: OwnershipLedger, stack_name: str, node_resource_id: str | None
) -> OwnershipLedger:
    resources = [
        resource
        for resource in existing_ledger.resources
        if not (
            resource.resource_type == TAILSCALE_NODE_RESOURCE_TYPE
            and resource.scope == _node_scope(stack_name)
        )
    ]
    if node_resource_id is not None:
        resources.append(
            OwnedResource(
                resource_type=TAILSCALE_NODE_RESOURCE_TYPE,
                resource_id=node_resource_id,
                scope=_node_scope(stack_name),
            )
        )
    return OwnershipLedger(
        format_version=existing_ledger.format_version, resources=tuple(resources)
    )


def _find_owned_resource(
    ownership_ledger: OwnershipLedger, stack_name: str
) -> OwnedResource | None:
    matches = [
        resource
        for resource in ownership_ledger.resources
        if resource.resource_type == TAILSCALE_NODE_RESOURCE_TYPE
        and resource.scope == _node_scope(stack_name)
    ]
    if len(matches) > 1:
        raise TailscaleError(
            "Ownership ledger contains multiple Tailscale nodes for scope "
            f"'{_node_scope(stack_name)}'."
        )
    return matches[0] if matches else None


def _node_scope(stack_name: str) -> str:
    return f"stack:{stack_name}:tailscale"


def _command_stdout_or_none(result: CommandResult) -> str | None:
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _run_command(command: list[str]) -> CommandResult:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    return CommandResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
