from __future__ import annotations

import re
import shlex
from importlib.util import find_spec
from pathlib import Path
from typing import Final

from dokploy_wizard.litellm.model_admin import PINNED_LITELLM_IMAGE

POSTGRES_TEST_IMAGE: Final = (
    "cimg/postgres:16.0@sha256:b125148bc76e8e8eee5eb3ad6020a3a14110a14e8192f1c645128afebe2e2f84"
)
PYTEST_RUNNER_PACKAGES: Final = (
    "pytest",
    "_pytest",
    "pluggy",
    "iniconfig",
    "packaging",
    "py",
    "pygments",
    "typing_extensions",
)
_SUCCESS_MARKER: Final = re.compile(
    r"^TASK3_STATUS=0 TASK3_SUMMARY=([0-9]+-passed(?:-[0-9]+-deselected)?)$"
)
_PYTEST_FAILURE_CATEGORIES: Final = (
    ("LiteLLM model routing parameters must contain exactly three keys", "routing-contract"),
    ("No module named 'pluggy'", "pytest-dependency"),
    ("No module named 'iniconfig'", "pytest-dependency"),
    ("No module named 'packaging'", "pytest-dependency"),
    ("No module named 'pygments'", "pytest-dependency"),
    ("No module named 'dokploy_wizard'", "source-import"),
    ("No module named 'tests'", "test-import"),
    ("ModuleNotFoundError", "module-import"),
    ("pinned LiteLLM service did not become ready", "service-readiness"),
    ("FAILED", "assertion"),
    ("ERROR", "collection"),
)


class PytestRunnerDependencyError(RuntimeError):
    def __init__(self, module: str) -> None:
        self.module = module
        super().__init__(f"missing pytest runner dependency: {module}")


def pytest_runner_dependency_paths() -> tuple[Path, ...]:
    paths: list[Path] = []
    for module in PYTEST_RUNNER_PACKAGES:
        spec = find_spec(module)
        if spec is None or spec.origin is None:
            raise PytestRunnerDependencyError(module)
        origin = Path(spec.origin)
        paths.append(origin.parent if spec.submodule_search_locations is not None else origin)
    return tuple(paths)


def remote_runner_command(*, remote_root: str, selector: str, run_id: str) -> tuple[str, ...]:
    pytest_command = shlex.join(("python3", "-m", "pytest", "-q", *shlex.split(selector)))
    command = (
        "set +e; "
        "PYTHONPATH=/work/pytest_site:/work/src:/work "
        "PYTHONDONTWRITEBYTECODE=1 "
        f"{pytest_command} >/work/task3-pytest.log 2>&1; "
        "status=$?; "
        "summary=$(grep -Eo '^[0-9]+ passed(, [0-9]+ deselected)?' "
        "/work/task3-pytest.log | tail -n 1 | tr -s ' ,' '-'); "
        "if [ \"$status\" -eq 0 ] && "
        "printf '%s' \"$summary\" | grep -Eq '^[0-9]+-passed(-[0-9]+-deselected)?$'; then "
        "printf 'TASK3_STATUS=0 TASK3_SUMMARY=%s\\n' \"$summary\"; "
        "else printf 'TASK3_STATUS=1 TASK3_SUMMARY=no-summary\\n'; fi"
    )
    return (
        "docker",
        "run",
        "--detach",
        "--name",
        f"dw-task3-runner-{run_id}",
        "--network",
        "host",
        "--volume",
        f"{remote_root}:/work:rw",
        "--volume",
        "/usr/bin/docker:/usr/bin/docker:ro",
        "--volume",
        "/var/run/docker.sock:/var/run/docker.sock",
        "--workdir",
        "/work",
        "--env",
        "DOKPLOY_WIZARD_REMOTE_LITELLM_ADMIN_TEST=1",
        "--env",
        f"LITELLM_TEST_IMAGE={PINNED_LITELLM_IMAGE}",
        "--env",
        f"POSTGRES_TEST_IMAGE={POSTGRES_TEST_IMAGE}",
        "--env",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "--env",
        f"DOKPLOY_WIZARD_TASK3_RUN_ID={run_id}",
        "--entrypoint",
        "sh",
        PINNED_LITELLM_IMAGE,
        "-ec",
        command,
    )


def classify_runner_marker(marker: str) -> str | None:
    match = _SUCCESS_MARKER.fullmatch(marker)
    return match.group(1) if match is not None else None


def classify_pytest_log(log: str) -> str:
    for marker, category in _PYTEST_FAILURE_CATEGORIES:
        if marker in log:
            return category
    return "other"
