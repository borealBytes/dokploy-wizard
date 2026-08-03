from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path

import pytest

from dokploy_wizard.litellm.opencode_go_cutover import OpenCodeGoCutoverCoordinator
from dokploy_wizard.litellm.opencode_go_cutover_types import (
    CutoverContext,
    CutoverImage,
    OpenCodeGoCutoverBlockedError,
    OpenCodeGoCutoverCrash,
)
from tests.unit._opencode_go_reconciler_support import (
    MemoryModelAdminApi,
    reconciliation_input,
    sample_catalog_model,
)


@dataclass(frozen=True, slots=True)
class _RecordingDeployment:
    events: list[str]

    def deploy_transitional(self) -> None:
        self.events.append("deploy_transitional")

    def verify_transitional(self) -> None:
        self.events.append("verify_transitional")

    def deploy_dynamic(self) -> None:
        self.events.append("deploy_dynamic")

    def verify_dynamic(self, aliases: tuple[str, ...]) -> None:
        self.events.append(f"verify_dynamic:{','.join(aliases)}")


def test_forward_resume_blocks_when_journal_images_change(tmp_path: Path) -> None:
    # Given
    model = sample_catalog_model()
    model_name = f"opencode-go/{model.source_id}"
    image = CutoverImage(
        compose_sha256=sha256(b"compose").hexdigest(),
        config_sha256=sha256(b"config").hexdigest(),
        model_set_sha256=sha256(model_name.encode()).hexdigest(),
    )
    context = CutoverContext(
        owner_id="wizard-owner",
        catalog_id="opencode-go",
        compose_id="compose-1",
        pre_image=image,
        transitional_image=image,
        dynamic_image=CutoverImage(
            compose_sha256=sha256(b"dynamic-compose").hexdigest(),
            config_sha256=sha256(b"dynamic-config").hexdigest(),
            model_set_sha256=sha256(model_name.encode()).hexdigest(),
        ),
        static_aliases=(model_name,),
    )
    api = MemoryModelAdminApi([], [], [], [])
    deployment = _RecordingDeployment([])

    def crash_at_dynamic_deployed(checkpoint: str) -> None:
        if checkpoint == "dynamic_deployed":
            raise OpenCodeGoCutoverCrash(checkpoint)

    crashing = OpenCodeGoCutoverCoordinator(
        api=api,
        state_root=tmp_path,
        deployment=deployment,
        crash_hook=crash_at_dynamic_deployed,
    )
    changed_image = replace(
        image,
        compose_sha256=sha256(b"changed-static-compose").hexdigest(),
    )
    changed_context = replace(
        context,
        pre_image=changed_image,
        transitional_image=changed_image,
    )

    # When
    with pytest.raises(OpenCodeGoCutoverCrash):
        crashing.run(reconciliation_input((model,)), context)
    with pytest.raises(OpenCodeGoCutoverBlockedError, match="identity changed"):
        OpenCodeGoCutoverCoordinator(
            api=api,
            state_root=tmp_path,
            deployment=deployment,
        ).run(reconciliation_input((model,)), changed_context)

    # Then
    assert api.mutations == [f"create:{model_name}"]
    assert deployment.events == [
        "deploy_transitional",
        "verify_transitional",
        "deploy_dynamic",
    ]
