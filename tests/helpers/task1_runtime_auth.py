from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from dokploy_wizard.proof.model_sync_task1_context import (
    PreparedTask1ProofContext,
    derive_task1_proof_context,
)
from dokploy_wizard.proof.model_sync_task1_context_schema import task1_env_bytes
from dokploy_wizard.proof.model_sync_task1_materialization import (
    materialize_task1_external_files,
)


@dataclass(frozen=True, slots=True)
class Task1RuntimeAuthFixture:
    preparation: PreparedTask1ProofContext
    upload_bytes: bytes
    upload_mode: int


def prepare_task1_runtime_auth_fixture(tmp_path: Path) -> Task1RuntimeAuthFixture:
    source_values = {
        "CLOUDFLARE_ACCOUNT_ID": "original-account",
        "CLOUDFLARE_API_TOKEN": "cloudflare-token",
        "CLOUDFLARE_ZONE_ID": "original-zone",
        "DOKPLOY_ADMIN_EMAIL": "admin@example.test",
        "DOKPLOY_ADMIN_PASSWORD": "admin-password",
        "PACKS": "coder",
        "ROOT_DOMAIN": "example.test",
    }
    source_bytes = task1_env_bytes(source_values)
    source_path = tmp_path / "operator.env"
    source_path.write_bytes(source_bytes)
    source_path.chmod(0o600)
    preparation = derive_task1_proof_context(
        source_values=source_values,
        source_bytes=source_bytes,
        source_path=source_path,
        proof_directory=tmp_path / "proof",
        attempt_token="0123456789abcdef0123456789abcdef",
    )
    materialize_task1_external_files(preparation.materialization)
    upload_path = preparation.uploaded_env_file
    return Task1RuntimeAuthFixture(
        preparation=preparation,
        upload_bytes=upload_path.read_bytes(),
        upload_mode=stat.S_IMODE(upload_path.stat().st_mode),
    )
