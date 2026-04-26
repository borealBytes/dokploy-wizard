# pyright: reportMissingImports=false

from __future__ import annotations

from pathlib import Path

from dokploy_wizard.cli import run_install_flow

from tests.integration.test_nextcloud_pack import (
    FIXTURES_DIR,
    FakeCloudflareBackend,
    FakeDokployBackend,
    FakeHeadscaleBackend,
    FakeNextcloudBackend,
    FakeSharedCoreBackend,
)


def test_nextcloud_bundle_contract_requires_onlyoffice_and_first_class_talk(
    tmp_path: Path,
) -> None:
    summary = run_install_flow(
        env_file=FIXTURES_DIR / "nextcloud.env",
        state_dir=tmp_path / "state",
        dry_run=False,
        bootstrap_backend=FakeDokployBackend(True, True),
        networking_backend=FakeCloudflareBackend(),
        shared_core_backend=FakeSharedCoreBackend(),
        headscale_backend=FakeHeadscaleBackend(),
        nextcloud_backend=FakeNextcloudBackend(),
    )

    nextcloud_summary = summary["nextcloud"]

    assert nextcloud_summary["outcome"] == "applied"
    assert set(nextcloud_summary) == {
        "enabled",
        "nextcloud",
        "notes",
        "onlyoffice",
        "outcome",
        "talk",
    }
    assert nextcloud_summary["nextcloud"]["service"]["resource_name"] == "nextcloud-stack-nextcloud"
    assert (
        nextcloud_summary["onlyoffice"]["service"]["resource_name"] == "nextcloud-stack-onlyoffice"
    )
    assert nextcloud_summary["talk"]["app_id"] == "spreed"
    assert nextcloud_summary["talk"]["enabled"] is True
    assert (
        nextcloud_summary["nextcloud"]["config"]["onlyoffice_url"] == "https://office.example.com"
    )
    assert (
        nextcloud_summary["onlyoffice"]["config"]["nextcloud_url"]
        == "https://nextcloud.example.com"
    )
    assert nextcloud_summary["onlyoffice"]["config"]["integration_secret_ref"] == (
        "nextcloud-stack-nextcloud-onlyoffice-jwt-secret"
    )
    assert set(nextcloud_summary["nextcloud"]["config"]) == {"onlyoffice_url", "postgres", "redis"}
    assert set(nextcloud_summary["onlyoffice"]["config"]) == {
        "integration_secret_ref",
        "nextcloud_url",
    }
    assert nextcloud_summary["nextcloud"]["health_check"]["passed"] is True
    assert nextcloud_summary["onlyoffice"]["health_check"]["passed"] is True
    assert nextcloud_summary["onlyoffice"]["document_server_check"] == {
        "command": "php occ onlyoffice:documentserver --check",
        "passed": True,
    }
    assert nextcloud_summary["talk"]["enabled_check"] == {
        "command": "php occ app:list --output=json",
        "passed": True,
    }
    assert nextcloud_summary["talk"]["signaling_check"] == {
        "command": "php occ talk:signaling:list --output=json",
        "passed": True,
    }
    assert nextcloud_summary["talk"]["stun_check"] == {
        "command": "php occ talk:stun:list --output=json",
        "passed": True,
    }
    assert nextcloud_summary["talk"]["turn_check"] == {
        "command": "php occ talk:turn:list --output=json",
        "passed": True,
    }
    assert any("Nextcloud, OnlyOffice, and Talk" in note for note in nextcloud_summary["notes"])
