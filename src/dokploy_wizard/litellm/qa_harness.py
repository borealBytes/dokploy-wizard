# ruff: noqa: E501
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request

from dokploy_wizard.networking import resolve_litellm_admin_hostname
from dokploy_wizard.state import DesiredState, RawEnvInput, parse_env_file, resolve_desired_state

_ACCESS_ALLOWED_STATUSES = (302, 401, 403)
_CURL_IMAGE = "curlimages/curl:8.7.1"
_READINESS_PATH = "/health/readiness"


class LiteLLMAdminAccessCheckError(RuntimeError):
    """Raised when the public LiteLLM admin route is not Access-protected."""


class LiteLLMAdminQaHarnessError(RuntimeError):
    """Raised when a LiteLLM admin QA harness command fails."""


@dataclass(frozen=True)
class LiteLLMAdminQaCheck:
    name: str
    shell_command: str
    command: tuple[str, ...] | None
    success_criteria: str
    failure_criteria: str


@dataclass(frozen=True)
class LiteLLMAdminQaHarness:
    admin_url: str
    internal_url: str
    checks: tuple[LiteLLMAdminQaCheck, ...]


def build_litellm_admin_qa_harness(
    *, raw_env: RawEnvInput, desired_state: DesiredState
) -> LiteLLMAdminQaHarness:
    admin_hostname = resolve_litellm_admin_hostname(raw_env=raw_env, desired_state=desired_state)
    if admin_hostname is None or desired_state.shared_core.litellm is None:
        raise LiteLLMAdminQaHarnessError(
            "LiteLLM shared-core admin QA requires a planned LiteLLM service."
        )

    internal_url = f"http://{desired_state.shared_core.litellm.service_name}:4000{_READINESS_PATH}"
    admin_url = f"https://{admin_hostname}"
    shared_network_command = _shared_network_readiness_command(
        stack_name=desired_state.stack_name,
        internal_url=internal_url,
    )
    checks = [
        LiteLLMAdminQaCheck(
            name="public-admin-access",
            shell_command=_public_access_shell_command(admin_url),
            command=None,
            success_criteria="Anonymous request returns a Cloudflare Access challenge/deny status (302/401/403).",
            failure_criteria="Public admin must never return unauthenticated 200.",
        ),
        LiteLLMAdminQaCheck(
            name="shared-network-readiness",
            shell_command=shlex.join(shared_network_command),
            command=shared_network_command,
            success_criteria="Shared Docker network can reach LiteLLM readiness over service DNS.",
            failure_criteria="Container-network probe cannot reach the internal LiteLLM readiness endpoint.",
        ),
    ]

    if desired_state.enable_tailscale and desired_state.tailscale_enable_ssh:
        assert desired_state.tailscale_hostname is not None
        tailnet_command = _tailnet_readiness_command(
            tailscale_hostname=desired_state.tailscale_hostname,
            shared_network_command=shared_network_command,
        )
        checks.append(
            LiteLLMAdminQaCheck(
                name="tailnet-host-readiness",
                shell_command=shlex.join(tailnet_command),
                command=tailnet_command,
                success_criteria="Tailnet host reachability can run the same shared-network readiness probe without OTP.",
                failure_criteria="Tailnet host path cannot reach or execute the internal LiteLLM readiness probe.",
            )
        )

    return LiteLLMAdminQaHarness(
        admin_url=admin_url,
        internal_url=internal_url,
        checks=tuple(checks),
    )


def verify_public_litellm_admin_access(
    url: str,
    *,
    request_fn: Callable[[request.Request], object] | None = None,
) -> None:
    req = request.Request(url=url, method="GET", headers={"Accept": "text/html"})
    sender = request_fn or _default_public_request
    try:
        response = sender(req)
    except error.HTTPError as exc:
        if exc.code in _ACCESS_ALLOWED_STATUSES:
            return
        raise LiteLLMAdminAccessCheckError(
            f"LiteLLM public admin access returned unexpected status {exc.code}; expected one of {_ACCESS_ALLOWED_STATUSES} and never 200."
        ) from exc
    except error.URLError as exc:
        raise LiteLLMAdminAccessCheckError(
            f"LiteLLM public admin access check failed: {exc.reason}."
        ) from exc

    status = getattr(response, "status", None)
    close = getattr(response, "close", None)
    if callable(close):
        close()
    if status in _ACCESS_ALLOWED_STATUSES:
        return
    if status == 200:
        raise LiteLLMAdminAccessCheckError(
            "LiteLLM public admin access returned 200 without Cloudflare Access protection."
        )
    raise LiteLLMAdminAccessCheckError(
        f"LiteLLM public admin access returned unexpected status {status}; expected one of {_ACCESS_ALLOWED_STATUSES}."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.dokploy_wizard.litellm.qa_harness",
        description="Run the LiteLLM admin ingress and internal readiness QA harness.",
    )
    parser.add_argument("--env-file", required=True, help="path to the install env file")
    parser.add_argument(
        "--print-commands",
        action="store_true",
        help="print the derived QA commands without executing them",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    raw_env = parse_env_file(Path(args.env_file))
    desired_state = resolve_desired_state(raw_env)
    harness = build_litellm_admin_qa_harness(raw_env=raw_env, desired_state=desired_state)

    if args.print_commands:
        for check in harness.checks:
            print(f"[{check.name}] {check.shell_command}")
        return 0

    print(f"[public-admin-access] {harness.admin_url}")
    verify_public_litellm_admin_access(harness.admin_url)
    print("[pass] public-admin-access")

    for check in harness.checks:
        if check.command is None:
            continue
        _run_command(check)
        print(f"[pass] {check.name}")
    return 0


def _run_command(check: LiteLLMAdminQaCheck) -> None:
    assert check.command is not None
    completed = subprocess.run(check.command, capture_output=True, text=True, check=False)  # noqa: S603
    if completed.returncode == 0:
        return
    detail = completed.stderr.strip() or completed.stdout.strip() or "command failed"
    raise LiteLLMAdminQaHarnessError(f"{check.name} failed: {detail}")


def _shared_network_readiness_command(*, stack_name: str, internal_url: str) -> tuple[str, ...]:
    return (
        "docker",
        "run",
        "--rm",
        "--network",
        f"{stack_name}-shared",
        _CURL_IMAGE,
        "-fsS",
        internal_url,
    )


def _tailnet_readiness_command(
    *, tailscale_hostname: str, shared_network_command: tuple[str, ...]
) -> tuple[str, ...]:
    return (
        "tailscale",
        "ssh",
        tailscale_hostname,
        shlex.join(shared_network_command),
    )


def _public_access_shell_command(url: str) -> str:
    return (
        f"status=$(curl -ksS -o /tmp/litellm-access-body.$$ -w '%{{http_code}}' {shlex.quote(url)}) && "
        'case "$status" in 302|401|403) exit 0 ;; 200) '
        "echo 'unauthenticated 200 from LiteLLM admin is forbidden' >&2; exit 1 ;; *) "
        'echo "unexpected LiteLLM admin status $status" >&2; exit 1 ;; esac'
    )


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> request.Request | None:
        del req, fp, code, msg, headers, newurl
        return None


def _default_public_request(req: request.Request) -> object:
    opener = request.build_opener(_NoRedirectHandler())
    return opener.open(req, timeout=30)


if __name__ == "__main__":
    sys.exit(main())
