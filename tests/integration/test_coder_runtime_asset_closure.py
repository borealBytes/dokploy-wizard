from __future__ import annotations

from pathlib import Path

from dokploy_wizard.dokploy.coder_runtime_asset_materialization import materialized_runtime_paths


def test_task6_runtime_producers_match_derived_image_and_no_template_uses_fictional_assets(
) -> None:
    # Given
    root = Path(__file__).resolve().parents[2]
    templates = tuple(
        (root / "templates" / "coder").glob("*/main.tf")
    )
    dockerfile = (root / "templates" / "coder" / "runtime" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    # When
    contents = tuple(template.read_text(encoding="utf-8") for template in templates)

    # Then
    assert len(templates) == 6
    assert all("/opt/dokploy-wizard/runtime/apps/" not in content for content in contents)
    assert all("/opt/dokploy-wizard/runtime/icons/" not in content for content in contents)
    assert materialized_runtime_paths() == frozenset(
        {
            "/opt/dokploy-wizard/runtime/install/opencode",
            "/opt/dokploy-wizard/runtime/install/node",
            "/opt/dokploy-wizard/runtime/install/proxy-node",
            "/opt/dokploy-wizard/runtime/install/zellij",
            "/opt/dokploy-wizard/runtime/install/pi",
                "/opt/dokploy-wizard/runtime/install/packages",
                "/opt/dokploy-wizard/runtime/install/hermes",
                "/opt/dokploy-wizard/runtime/install/kdense",
        }
    )
    assert "/opt/dokploy-wizard/runtime/install/opencode/bin/opencode" in dockerfile
    assert "/opt/dokploy-wizard/runtime/install/zellij/bin/zellij" in dockerfile
    assert "/opt/dokploy-wizard/runtime/install/node/bin/node" in dockerfile
    assert "/opt/dokploy-wizard/runtime/install/proxy-node/bin/node" in dockerfile
    assert "/opt/dokploy-wizard/runtime/install/pi/node_modules/.bin/pi" in dockerfile
    assert "/opt/dokploy-wizard/runtime/install/kdense/source/start.mjs" in dockerfile
    assert "/opt/dokploy-wizard/runtime/apps/" not in dockerfile
    assert "/opt/dokploy-wizard/runtime/icons/" not in dockerfile
