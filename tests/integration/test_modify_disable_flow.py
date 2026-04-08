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


def _seed_nextcloud_state(state_dir: Path) -> None:
    base_raw_input = parse_env_file(FIXTURES_DIR / "nextcloud.env")
    raw_input = RawEnvInput(
        format_version=base_raw_input.format_version,
        values={
            **base_raw_input.values,
            "CLOUDFLARE_MOCK_EXISTING_TUNNEL_ID": "nextcloud-stack-tunnel",
            "CLOUDFLARE_MOCK_EXISTING_HOSTNAMES": (
                "dokploy.example.com,headscale.example.com,nextcloud.example.com,office.example.com"
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
                "nextcloud",
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
                    "nextcloud-example-com",
                    "zone:zone-123:nextcloud.example.com",
                ),
                OwnedResource(
                    "cloudflare_dns_record",
                    "office-example-com",
                    "zone:zone-123:office.example.com",
                ),
                OwnedResource(
                    "shared_core_network",
                    "nextcloud-stack-core",
                    "stack:nextcloud-stack:shared-network",
                ),
                OwnedResource(
                    "shared_core_postgres",
                    "nextcloud-stack-postgres",
                    "stack:nextcloud-stack:shared-postgres",
                ),
                OwnedResource(
                    "shared_core_redis",
                    "nextcloud-stack-redis",
                    "stack:nextcloud-stack:shared-redis",
                ),
                OwnedResource(
                    "headscale_service",
                    "nextcloud-stack-headscale",
                    "stack:nextcloud-stack:headscale",
                ),
                OwnedResource(
                    "nextcloud_service",
                    "nextcloud-stack-nextcloud",
                    "stack:nextcloud-stack:nextcloud-service",
                ),
                OwnedResource(
                    "onlyoffice_service",
                    "nextcloud-stack-onlyoffice",
                    "stack:nextcloud-stack:onlyoffice-service",
                ),
                OwnedResource(
                    "nextcloud_volume",
                    "nextcloud-stack-nextcloud-data",
                    "stack:nextcloud-stack:nextcloud-volume",
                ),
                OwnedResource(
                    "onlyoffice_volume",
                    "nextcloud-stack-onlyoffice-data",
                    "stack:nextcloud-stack:onlyoffice-volume",
                ),
            ),
        ),
    )


def test_modify_disable_nextcloud_deletes_runtime_and_preserves_data(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _seed_nextcloud_state(state_dir)
    modify_env = tmp_path / "disable-nextcloud.env"
    base_env = (FIXTURES_DIR / "nextcloud.env").read_text(encoding="utf-8")
    updated_env = _replace_line(base_env, "ENABLE_NEXTCLOUD", "false")
    updated_env += (
        "\nCLOUDFLARE_MOCK_EXISTING_TUNNEL_ID=nextcloud-stack-tunnel"
        "\nCLOUDFLARE_MOCK_EXISTING_HOSTNAMES=dokploy.example.com,headscale.example.com,nextcloud.example.com,office.example.com"
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
        "nextcloud_service",
        "onlyoffice_service",
        "shared_core_network",
    }
    assert loaded.ownership_ledger is not None
    assert {resource.resource_type for resource in loaded.ownership_ledger.resources} == {
        "cloudflare_tunnel",
        "cloudflare_dns_record",
        "headscale_service",
        "nextcloud_volume",
        "onlyoffice_volume",
        "shared_core_postgres",
        "shared_core_redis",
    }
    assert loaded.applied_state is not None
    assert loaded.applied_state.completed_steps == (
        "preflight",
        "dokploy_bootstrap",
        "networking",
        "shared_core",
        "headscale",
    )
