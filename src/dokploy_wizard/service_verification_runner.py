from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from dokploy_wizard import cli
from dokploy_wizard.bootstrap import LOCAL_HEALTH_URL
from dokploy_wizard.dokploy import openclaw as openclaw_module
from dokploy_wizard.state import (
    RawEnvInput,
    load_litellm_generated_keys,
    load_state_dir,
    parse_env_file,
    resolve_desired_state,
)
from dokploy_wizard.verification import ServiceVerificationResult, make_verification_result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dokploy_wizard.service_verification_runner",
        description="Run post-install service verification checks.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        required=True,
        help="path to the reusable env file",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".dokploy-wizard-state"),
        help="directory containing persisted wizard state documents",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_service_verification(env_file=args.env_file, state_dir=args.state_dir)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["passed"] else 1


def run_service_verification(*, env_file: Path, state_dir: Path) -> dict[str, Any]:
    loaded_state = load_state_dir(state_dir)
    raw_env = _merge_persisted_retry_keys(parse_env_file(env_file), loaded_state.raw_input)
    desired_state = resolve_desired_state(raw_env)
    litellm_generated_keys = load_litellm_generated_keys(state_dir)
    dokploy_session_client = cli._build_dokploy_session_client(
        raw_env=raw_env,
        api_url=desired_state.dokploy_api_url or LOCAL_HEALTH_URL,
    )
    shared_core_backend = cli._build_shared_core_backend(
        raw_env=raw_env,
        state_dir=state_dir,
        desired_state=desired_state,
        session_client=dokploy_session_client,
        litellm_generated_keys=litellm_generated_keys,
    )
    nextcloud_backend = cli._build_nextcloud_backend(
        raw_env=raw_env,
        desired_state=desired_state,
        session_client=dokploy_session_client,
    )
    moodle_backend = cli._build_moodle_backend(
        raw_env=raw_env,
        desired_state=desired_state,
        session_client=dokploy_session_client,
    )
    docuseal_backend = cli._build_docuseal_backend(
        raw_env=raw_env,
        desired_state=desired_state,
        session_client=dokploy_session_client,
    )
    seaweedfs_backend = cli._build_seaweedfs_backend(
        raw_env=raw_env,
        state_dir=state_dir,
        desired_state=desired_state,
        session_client=dokploy_session_client,
    )
    coder_backend = cli._build_coder_backend(
        raw_env=raw_env,
        state_dir=state_dir,
        desired_state=desired_state,
        session_client=dokploy_session_client,
    )
    openclaw_backend = cli._build_openclaw_backend(
        raw_env=raw_env,
        state_dir=state_dir,
        desired_state=desired_state,
        session_client=dokploy_session_client,
        litellm_generated_keys=litellm_generated_keys,
    )

    results: list[ServiceVerificationResult] = [
        _verify_shared_core(shared_core_backend=shared_core_backend),
    ]
    if "nextcloud" in desired_state.enabled_packs:
        results.append(
            _verify_backend_method(
                backend=nextcloud_backend,
                method_name="_verify_current_application",
                service_name="nextcloud",
                unavailable_detail="Nextcloud verification backend is unavailable.",
            )
        )
    if "moodle" in desired_state.enabled_packs:
        results.append(
            _verify_backend_method(
                backend=moodle_backend,
                method_name="_verify_current_application",
                service_name="moodle",
                unavailable_detail="Moodle verification backend is unavailable.",
            )
        )
    if "docuseal" in desired_state.enabled_packs:
        results.append(
            _verify_backend_method(
                backend=docuseal_backend,
                method_name="_verify_current_application",
                service_name="docuseal",
                unavailable_detail="DocuSeal verification backend is unavailable.",
            )
        )
    if "seaweedfs" in desired_state.enabled_packs:
        results.append(
            _verify_backend_method(
                backend=seaweedfs_backend,
                method_name="_verify_current_service",
                service_name="seaweedfs",
                unavailable_detail="SeaweedFS verification backend is unavailable.",
            )
        )
    if "coder" in desired_state.enabled_packs:
        results.append(
            _verify_backend_method(
                backend=coder_backend,
                method_name="_verify_current_compose_application",
                service_name="coder",
                unavailable_detail="Coder verification backend is unavailable.",
            )
        )
    if "openclaw" in desired_state.enabled_packs:
        results.append(
            _verify_advisor_runtime(
                backend=openclaw_backend,
                desired_state=desired_state,
                variant="openclaw",
            )
        )
    if "my-farm-advisor" in desired_state.enabled_packs:
        results.append(
            _verify_advisor_runtime(
                backend=openclaw_backend,
                desired_state=desired_state,
                variant="my-farm-advisor",
            )
        )

    return {
        "passed": all(result.passed for result in results),
        "results": [result.to_dict() for result in results],
    }


def _merge_persisted_retry_keys(
    raw_env: RawEnvInput, persisted_raw: RawEnvInput | None
) -> RawEnvInput:
    if persisted_raw is None:
        return raw_env
    values = dict(raw_env.values)
    changed = False
    for key in cli._PERSISTED_RETRY_KEYS:
        persisted_value = persisted_raw.values.get(key)
        if persisted_value is None or values.get(key) == persisted_value:
            continue
        values[key] = persisted_value
        changed = True
    if not changed:
        return raw_env
    return RawEnvInput(format_version=raw_env.format_version, values=values)


def _verify_shared_core(*, shared_core_backend: Any) -> ServiceVerificationResult:
    verifier = getattr(shared_core_backend, "_shared_core_runtime_ready_for_noop", None)
    if not callable(verifier):
        return make_verification_result(
            service_name="shared-core",
            tier="app",
            passed=False,
            detail="Shared core verification backend is unavailable.",
        )
    passed = bool(verifier())
    return make_verification_result(
        service_name="shared-core",
        tier="app",
        passed=passed,
        detail=(
            "Shared core Postgres, Redis, and LiteLLM readiness checks passed."
            if passed
            else "Shared core Postgres, Redis, or LiteLLM readiness checks failed."
        ),
    )


def _verify_backend_method(
    *, backend: Any, method_name: str, service_name: str, unavailable_detail: str
) -> ServiceVerificationResult:
    verifier = getattr(backend, method_name, None)
    if not callable(verifier):
        return make_verification_result(
            service_name=service_name,
            tier="app",
            passed=False,
            detail=unavailable_detail,
        )
    result = verifier()
    if not isinstance(result, ServiceVerificationResult):
        raise TypeError(f"Expected ServiceVerificationResult from {service_name} verifier.")
    return result


def _verify_advisor_runtime(
    *, backend: Any, desired_state: Any, variant: str
) -> ServiceVerificationResult:
    verifier = getattr(backend, "_verify_service_runtime", None)
    if not callable(verifier):
        return make_verification_result(
            service_name=variant,
            tier="app",
            passed=False,
            detail=f"{variant} verification backend is unavailable.",
        )
    hostname_key = "openclaw" if variant == "openclaw" else "my-farm-advisor"
    hostname = desired_state.hostnames.get(hostname_key)
    if not hostname:
        return make_verification_result(
            service_name=variant,
            tier="app",
            passed=False,
            detail=f"{variant} hostname is unavailable for verification.",
        )
    result = verifier(
        service_name=f"{desired_state.stack_name}-{variant}",
        variant=variant,
        url=openclaw_module._external_health_url(hostname=hostname, variant=variant),
    )
    if not isinstance(result, ServiceVerificationResult):
        raise TypeError(f"Expected ServiceVerificationResult from {variant} verifier.")
    return result


@dataclass(frozen=True)
class ServiceVerificationCheck:
    """A single service verification check definition."""

    service_id: str
    label: str
    verify: Callable[[], ServiceVerificationResult]


@dataclass(frozen=True)
class ServiceVerificationReport:
    """Aggregated report from multiple service verification checks."""

    entries: tuple[dict[str, Any], ...]
    summary: dict[str, int]
    status: Literal["pass", "fail"]

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": list(self.entries),
            "summary": self.summary,
            "status": self.status,
        }


def run_service_verification_checks(
    checks: Sequence[ServiceVerificationCheck],
) -> ServiceVerificationReport:
    """Run a sequence of service verification checks and aggregate results."""

    entries: list[dict[str, Any]] = []
    pass_count = 0
    fail_count = 0

    for check in checks:
        result = check.verify()
        entry: dict[str, Any] = {
            "service_id": check.service_id,
            "label": check.label,
            "status": "pass" if result.passed else "fail",
            "detail": result.detail,
        }
        if result.evidence_command is not None:
            entry["evidence_command"] = result.evidence_command
        entries.append(entry)
        if result.passed:
            pass_count += 1
        else:
            fail_count += 1

    status: Literal["pass", "fail"] = "fail" if fail_count > 0 else "pass"
    return ServiceVerificationReport(
        entries=tuple(entries),
        summary={"pass": pass_count, "fail": fail_count},
        status=status,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
