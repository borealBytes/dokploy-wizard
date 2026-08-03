from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dokploy_wizard.core.models import SharedMailRelayServicePlan
from dokploy_wizard.dokploy.coder import _render_compose_file as render_coder_compose
from dokploy_wizard.dokploy.shared_core import _render_compose_file as render_shared_compose
from dokploy_wizard.state import (
    AppliedStateCheckpoint,
    RawEnvInput,
    StateValidationError,
    parse_env_file,
    resolve_desired_state,
)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def test_task5_runtime_images_are_digest_pinned_in_desired_applied_and_compose() -> None:
    raw = parse_env_file(FIXTURES_DIR / "full.env")
    raw = RawEnvInput(format_version=1, values={**raw.values, "PACKS": "nextcloud,coder"})
    desired = resolve_desired_state(raw)
    images = desired.runtime_images
    shared_plan = replace(
        desired.shared_core,
        mail_relay=SharedMailRelayServicePlan(
            service_name=f"{desired.stack_name}-shared-postfix",
            mail_hostname=f"mail.{desired.root_domain}",
            smtp_port=25,
            from_address=f"noreply@{desired.root_domain}",
        ),
    )

    shared = render_shared_compose(
        shared_plan,
        {},
        dict(raw.values),
        runtime_images=images,
    ).compose_file
    coder_allocation = next(
        item.postgres for item in desired.shared_core.allocations if item.pack_name == "coder"
    )
    assert coder_allocation is not None
    assert desired.shared_core.postgres is not None
    coder = render_coder_compose(
        stack_name=desired.stack_name,
        hostname=desired.hostnames["coder"],
        wildcard_hostname=desired.hostnames.get("coder-wildcard"),
        postgres_service_name=desired.shared_core.postgres.service_name,
        postgres=coder_allocation,
        image_digest=images.coder,
    ).compose_file
    applied = AppliedStateCheckpoint(
        format_version=1,
        desired_state_fingerprint=desired.fingerprint(),
        completed_steps=(),
        runtime_images=images,
    )

    for digest in (
        images.pgvector,
        images.redis,
        images.postfix,
        images.litellm,
    ):
        assert f"image: {digest}" in shared
    assert f"image: {images.coder}" in coder
    assert desired.to_dict()["runtime_images"] == images.to_dict()
    assert applied.to_dict()["runtime_images"] == images.to_dict()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("SHARED_POSTGRES_IMAGE", "pgvector/pgvector:pg16"),
        ("SHARED_REDIS_IMAGE", "redis:7-alpine"),
        ("SHARED_POSTFIX_IMAGE", "boky/postfix:latest"),
        ("LITELLM_IMAGE", "ghcr.io/berriai/litellm:main-latest"),
        ("LITELLM_IMAGE_TAG", "main-latest"),
        ("CODER_IMAGE", "ghcr.io/coder/coder:latest"),
    ],
)
def test_task5_tag_only_runtime_image_overrides_fail(key: str, value: str) -> None:
    raw = parse_env_file(FIXTURES_DIR / "full.env")
    values = {**raw.values, key: value}

    with pytest.raises(StateValidationError, match="digest"):
        resolve_desired_state(RawEnvInput(format_version=1, values=values))


def test_task5_digest_override_changes_desired_fingerprint() -> None:
    raw = parse_env_file(FIXTURES_DIR / "full.env")
    baseline = resolve_desired_state(raw)
    override = "docker.io/library/redis@sha256:" + "1" * 64
    changed = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={**raw.values, "SHARED_REDIS_IMAGE": override},
        )
    )

    assert changed.runtime_images.redis == override
    assert changed.fingerprint() != baseline.fingerprint()
