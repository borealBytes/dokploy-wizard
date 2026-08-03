"""Dependency construction for production Host A upgrade operations."""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

from dokploy_wizard import proof
from dokploy_wizard.dokploy.coder_migration_api import (
    CoderMigrationApi,
)
from dokploy_wizard.proof.model_sync_artifacts import sha256_bytes
from dokploy_wizard.proof.model_sync_env import (
    resolve_proof_transport,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_coder import CoderUpgradeClient
from dokploy_wizard.proof.model_sync_upgrade_host_a_production import (
    ProductionUpgradeConfig,
    ProductionUpgradeHostAOperations,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_remote import (
    RemoteCoderTransport,
    RemoteWorkspaceTester,
)
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import (
    UpgradeHostABinding,
    UpgradeHostAError,
)


def build_production_operations(
    args: argparse.Namespace,
    binding: UpgradeHostABinding,
) -> ProductionUpgradeHostAOperations:
    """Bind production callbacks only after local Task 1 evidence is accepted."""

    wrapper = _canonical_wrapper(args.wrapper)
    env_file = _validated_env(args.env_file, binding)
    artifact_dir = args.artifact_dir.resolve(strict=True)
    if not artifact_dir.is_dir() or args.output.parent != args.artifact_dir:
        raise UpgradeHostAError("Task 18 artifact paths are invalid")
    if args.lifecycle_output.parent != args.artifact_dir:
        raise UpgradeHostAError("Task 18 lifecycle output is outside its artifact directory")
    host = os.environ.get(args.host_env, "")
    password = os.environ.get(args.password_env, "")
    if not host or not password:
        raise UpgradeHostAError("Host A external credentials are unavailable")
    namespace = binding.namespace
    transport = resolve_proof_transport(env_file)
    if (
        transport.coder_hostname is None
        or transport.coder_email is None
        or transport.coder_password is None
    ):
        raise UpgradeHostAError("Coder proof transport credentials are incomplete")
    coder_transport = RemoteCoderTransport(host, password, namespace.stack_name)
    token = coder_transport.login(
        transport.coder_email,
        transport.coder_password,
    )
    api = CoderMigrationApi(
        transport=coder_transport,
        session_token=token,
    )
    coder = CoderUpgradeClient(
        api,
        api.default_organization_id(),
        RemoteWorkspaceTester(host, password, namespace.stack_name, token),
    )
    return ProductionUpgradeHostAOperations(
        ProductionUpgradeConfig(
            host=host,
            password=password,
            wrapper=wrapper,
            env_file=env_file,
            artifact_dir=artifact_dir,
            binding=binding,
            namespace=namespace,
            transport=transport,
        ),
        coder,
    )


def _canonical_wrapper(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise UpgradeHostAError("Host A wrapper is absent") from error
    if not path.is_absolute() or path != resolved or not os.access(resolved, os.X_OK):
        raise UpgradeHostAError("Host A wrapper is not an executable canonical path")
    return resolved


def _validated_env(path: Path, binding: UpgradeHostABinding) -> Path:
    try:
        metadata = path.stat(follow_symlinks=False)
        content, mode = proof.read_bounded_regular_bytes(path, 256 * 1024, binding.env_mode)
    except (OSError, ValueError) as error:
        raise UpgradeHostAError("Host A proof env is absent or unsafe") from error
    if not stat.S_ISREG(metadata.st_mode) or sha256_bytes(content) != binding.env_sha256:
        raise UpgradeHostAError("Host A proof env does not match Task 1")
    if mode != binding.env_mode:
        raise UpgradeHostAError("Host A proof env mode does not match Task 1")
    return path.resolve(strict=True)
