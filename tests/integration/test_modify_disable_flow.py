# pyright: reportMissingImports=false

from __future__ import annotations

from pathlib import Path

from dokploy_wizard.cli import run_modify_flow
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    OwnedResource,
    OwnershipLedger,
    RawEnvInput,
    load_state_dir,
    parse_env_file,
    resolve_desired_state,
    write_applied_checkpoint,
    write_ownership_ledger,
    write_target_state,
)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _replace_line(content: str, key: str, value: str) -> str:
    prefix = f"{key}="
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{key}={value}"
            return "\n".join(lines) + "\n"
    return content + f"\n{key}={value}\n"


def _seed_matrix_state(state_dir: Path) -> None:
    base_raw_input = parse_env_file(FIXTURES_DIR / "nextcloud.env")
    raw_input = RawEnvInput(
        format_version=base_raw_input.format_version,
        values={
            **base_raw_input.values,
            "ENABLE_MATRIX": "true",
            "CLOUDFLARE_MOCK_EXISTING_TUNNEL_ID": "nextcloud-stack-tunnel",
            "CLOUDFLARE_MOCK_EXISTING_HOSTNAMES": (
                "dokploy.example.com,headscale.example.com,matrix.example.com"
            ),
            "HEADSCALE_MOCK_EXISTING_SERVICE_ID": "nextcloud-stack-headscale",
        },
    )
    desired_state = resolve_desired_state(raw_input)
    write_target_state(state_dir, raw_input, desired_state)
    write_applied_checkpoint(
        state_dir,
        AppliedStateCheckpoint(
            format_version=1,
            desired_state_fingerprint=desired_state.fingerprint(),
            completed_steps=(
                "preflight",
                "dokploy_bootstrap",
                "networking",
                "shared_core",
                "headscale",
                "matrix",
            ),
        ),
    )
    write_ownership_ledger(
        state_dir,
        OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource("cloudflare_tunnel", "nextcloud-stack-tunnel", "account:account-123"),
                OwnedResource(
                    "cloudflare_dns_record",
                    "dokploy-example-com",
                    "zone:zone-123:dokploy.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "headscale-example-com",
                    "zone:zone-123:headscale.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "matrix-example-com",
                    "zone:zone-123:matrix.example.com",
                ),
                OwnedResource(
                    "shared_core_network",
                    "nextcloud-stack-shared",
                    "stack:nextcloud-stack:shared-network",
                ),
                OwnedResource(
                    "shared_core_postgres",
                    "nextcloud-stack-shared-postgres",
                    "stack:nextcloud-stack:shared-postgres",
                ),
                OwnedResource(
                    "shared_core_redis",
                    "nextcloud-stack-shared-redis",
                    "stack:nextcloud-stack:shared-redis",
                ),
                OwnedResource(
                    "shared_core_litellm",
                    "nextcloud-stack-shared-litellm",
                    "stack:nextcloud-stack:shared-litellm",
                ),
                OwnedResource(
                    "headscale_service",
                    "nextcloud-stack-headscale",
                    "stack:nextcloud-stack:headscale",
                ),
                OwnedResource(
                    "matrix_service",
                    "nextcloud-stack-matrix",
                    "stack:nextcloud-stack:matrix-service",
                ),
                OwnedResource(
                    "matrix_data",
                    "nextcloud-stack-matrix-data",
                    "stack:nextcloud-stack:matrix-data",
                ),
            ),
        ),
    )


def test_modify_disable_matrix_deletes_runtime_and_preserves_data(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _seed_matrix_state(state_dir)
    modify_env = tmp_path / "disable-matrix.env"
    base_env = (FIXTURES_DIR / "nextcloud.env").read_text(encoding="utf-8")
    updated_env = _replace_line(base_env, "ENABLE_MATRIX", "false")
    updated_env += (
        "\nCLOUDFLARE_MOCK_EXISTING_TUNNEL_ID=nextcloud-stack-tunnel"
        "\nCLOUDFLARE_MOCK_EXISTING_HOSTNAMES=dokploy.example.com,headscale.example.com,matrix.example.com"
        "\nHEADSCALE_MOCK_EXISTING_SERVICE_ID=nextcloud-stack-headscale\n"
    )
    modify_env.write_text(updated_env, encoding="utf-8")

    summary = run_modify_flow(
        env_file=modify_env,
        state_dir=state_dir,
        dry_run=False,
    )

    loaded = load_state_dir(state_dir)
    assert summary["lifecycle"]["mode"] == "modify"
    assert summary["disable_teardown"]["planned_deletions"]
    deleted_types = {
        item["resource_type"]
        for item in summary["disable_teardown"]["executed"]["deleted_resources"]
    }
    assert deleted_types == {
        "cloudflare_dns_record",
        "matrix_service",
    }
    assert loaded.ownership_ledger is not None
    assert {resource.resource_type for resource in loaded.ownership_ledger.resources} == {
        "cloudflare_tunnel",
        "cloudflare_dns_record",
        "headscale_service",
        "matrix_data",
        "shared_core_litellm",
        "shared_core_network",
        "shared_core_postgres",
    }
    assert loaded.applied_state is not None
    assert loaded.applied_state.completed_steps == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "headscale",
    )
