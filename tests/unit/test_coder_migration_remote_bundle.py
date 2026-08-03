from __future__ import annotations

from tests.integration.test_coder_migration_remote_runtime import _REMOTE_FILES


def test_remote_bundle_manifest_includes_every_runtime_support_module() -> None:
    # Given
    names = tuple(path.name for path in _REMOTE_FILES)

    # When / Then
    assert names == (
        "__init__.py",
        "__init__.py",
        "_coder_migration_remote_runtime.py",
        "coder_migration_remote_build_detail.py",
        "coder_migration_remote_causes.py",
        "coder_migration_remote_docker.py",
        "coder_migration_remote_docker_metadata.py",
        "coder_migration_remote_http.py",
        "coder_migration_remote_main.py",
        "coder_migration_remote_protocol.py",
        "coder_migration_remote_result.py",
        "coder_migration_remote_runtime_probe.py",
        "coder_migration_remote_diagnostic.py",
        "coder_migration_remote_diagnostic_result.py",
        "coder_migration_remote_seed.py",
        "coder_migration_remote_template.py",
        "coder_migration_remote_workspace.py",
    )
