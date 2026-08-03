from __future__ import annotations

import os
import shlex
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from dokploy_wizard.remote_transport import ParamikoRemoteTransport, RemoteCommandCaptureLimits
from dokploy_wizard.state.env import parse_env_file
from tests.integration.coder_migration_remote_diagnostic_result import parse_diagnostic_result
from tests.integration.coder_migration_remote_main import _TASK1_CODER_IMAGE

_ROOT = Path(__file__).resolve().parents[2]
_LIMITS = RemoteCommandCaptureLimits(900, 2 * 1024 * 1024, 256 * 1024)
_REMOTE_FILES = (
    Path("tests/__init__.py"),
    Path("tests/integration/__init__.py"),
    Path("tests/integration/_coder_migration_remote_runtime.py"),
    Path("tests/integration/coder_migration_remote_build_detail.py"),
    Path("tests/integration/coder_migration_remote_causes.py"),
    Path("tests/integration/coder_migration_remote_docker.py"),
    Path("tests/integration/coder_migration_remote_docker_metadata.py"),
    Path("tests/integration/coder_migration_remote_http.py"),
    Path("tests/integration/coder_migration_remote_main.py"),
    Path("tests/integration/coder_migration_remote_protocol.py"),
    Path("tests/integration/coder_migration_remote_result.py"),
    Path("tests/integration/coder_migration_remote_runtime_probe.py"),
    Path("tests/integration/coder_migration_remote_diagnostic.py"),
    Path("tests/integration/coder_migration_remote_diagnostic_result.py"),
    Path("tests/integration/coder_migration_remote_seed.py"),
    Path("tests/integration/coder_migration_remote_template.py"),
    Path("tests/integration/coder_migration_remote_workspace.py"),
)


def test_runs_pinned_coder_contract_on_the_env_selected_vps() -> None:
    # Given
    image = os.environ.get("CODER_TEST_IMAGE")
    if image is None:
        pytest.skip("set CODER_TEST_IMAGE to run the authorized remote Coder contract")
    if image != _TASK1_CODER_IMAGE:
        pytest.fail("CODER_TEST_IMAGE must equal the Task 1 full Coder image reference")
    values = parse_env_file(_ROOT / ".install-min.env").values
    remote_root = f"/tmp/dokploy-wizard-task4-{uuid4().hex}"
    transport: ParamikoRemoteTransport | None = None

    # When
    try:
        transport = ParamikoRemoteTransport.connect(
            hostname=values["VPS_HOST"],
            username="root",
            password=values["VPS_ROOT_PASSWORD"],
            remote_root="/tmp",
        )
        with TemporaryDirectory(prefix="dokploy-wizard-task4-source-") as directory:
            bundle = _bundle(Path(directory))
            transport.ensure_dir(remote_root)
            transport.upload(bundle, f"{remote_root}/source.tar.gz")
            transport.capture(
                "task4-extract-source",
                (
                    f"tar -xzf {shlex.quote(f'{remote_root}/source.tar.gz')} "
                    f"-C {shlex.quote(remote_root)}"
                ),
                _LIMITS,
            )
            captured = transport.capture(
                "task4-pinned-coder-diagnostic",
                (
                    f"PYTHONPATH={shlex.quote(f'{remote_root}:{remote_root}/src')} "
                    f"CODER_TEST_IMAGE={shlex.quote(image)} "
                    "python3 -m tests.integration.coder_migration_remote_diagnostic"
                ),
                _LIMITS,
            )
    except RuntimeError:
        pytest.fail("remote pinned-Coder contract transport failed")
    finally:
        if transport is not None:
            try:
                transport.capture(
                    "task4-cleanup",
                    f"rm -rf -- {shlex.quote(remote_root)}",
                    _LIMITS,
                )
            finally:
                transport.close()

    # Then
    result = parse_diagnostic_result(captured.stdout)
    assert result.stage == "entrypoint", (
        result.stage,
        result.error_category,
        result.error_type,
        result.error_origin,
    )
    assert result.completed == (
        "package-import",
        "module-import",
        "images",
        "stack-construction",
        "run",
        "serialization",
        "entrypoint",
    )
    assert result.error_category == "none", result
    assert result.runtime_status == "passed", result
    assert result.runtime_phase == "assertions", result
    assert result.runtime_cause == "none", result


def _bundle(directory: Path) -> Path:
    bundle = directory / "source.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(_ROOT / "src", arcname="src")
        for relative in _REMOTE_FILES:
            archive.add(_ROOT / relative, arcname=relative.as_posix())
    return bundle
