from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from urllib import request

import pytest

from dokploy_wizard.litellm.catalog_json import JsonValue
from dokploy_wizard.litellm.model_admin import (
    LiteLLMModelAdminApi,
    LiteLLMModelAdminClient,
    LiteLLMModelAdminConflict,
    LiteLLMModelAdminError,
    LiteLLMModelDeployment,
    LiteLLMModelRecord,
    build_owned_model_deployment,
)
from dokploy_wizard.litellm.opencode_go_cutover import OpenCodeGoCutoverCoordinator
from dokploy_wizard.litellm.opencode_go_cutover_types import (
    CutoverContext,
    CutoverImage,
    OpenCodeGoCutoverBlockedError,
    OpenCodeGoCutoverCrash,
)
from dokploy_wizard.litellm.opencode_go_reconciler import OpenCodeGoDatabaseReconciler
from tests.unit._opencode_go_reconciler_support import (
    MemoryModelAdminApi,
    reconciliation_input,
    record_for,
    sample_catalog_model,
    stale_record,
)


@dataclass(frozen=True, slots=True)
class _RecordingCutoverDeployment:
    events: list[str]
    dynamic_failure: bool = False

    def deploy_transitional(self) -> None:
        self.events.append("deploy_transitional")

    def verify_transitional(self) -> None:
        self.events.append("verify_transitional")

    def deploy_dynamic(self) -> None:
        self.events.append("deploy_dynamic")
        if self.dynamic_failure:
            raise RuntimeError("dynamic deployment failed")

    def verify_dynamic(self, aliases: tuple[str, ...]) -> None:
        self.events.append(f"verify_dynamic:{','.join(aliases)}")


def _cutover_context(model_name: str) -> CutoverContext:
    image = CutoverImage(
        compose_sha256=sha256(b"compose").hexdigest(),
        config_sha256=sha256(b"config").hexdigest(),
        model_set_sha256=sha256(model_name.encode()).hexdigest(),
    )
    return CutoverContext(
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


def test_db_cutover_happy_persists_rows_then_switches_dynamic(tmp_path: Path) -> None:
    # Given
    model = sample_catalog_model()
    api = MemoryModelAdminApi([], [], [], [])
    deployment = _RecordingCutoverDeployment([])
    coordinator = OpenCodeGoCutoverCoordinator(
        api=api,
        state_root=tmp_path,
        deployment=deployment,
    )

    # When
    receipt = coordinator.run(
        reconciliation_input((model,)),
        _cutover_context(f"opencode-go/{model.source_id}"),
    )

    # Then
    assert receipt.status == "complete"
    assert receipt.rows[0].status == "verified"
    assert api.mutations == [f"create:opencode-go/{model.source_id}"]
    assert deployment.events == [
        "deploy_transitional",
        "verify_transitional",
        "deploy_dynamic",
        f"verify_dynamic:opencode-go/{model.source_id}",
    ]
    assert (tmp_path / "opencode-go" / "litellm-cutover-receipt-v1.json").exists()


def test_rollback_blocked_no_db_mutation_after_dynamic_deploy_failure(tmp_path: Path) -> None:
    # Given
    model = sample_catalog_model()
    api = MemoryModelAdminApi([], [], [], [])
    deployment = _RecordingCutoverDeployment([], dynamic_failure=True)
    coordinator = OpenCodeGoCutoverCoordinator(
        api=api,
        state_root=tmp_path,
        deployment=deployment,
    )

    # When
    with pytest.raises(OpenCodeGoCutoverBlockedError, match="dynamic deployment"):
        coordinator.run(
            reconciliation_input((model,)),
            _cutover_context(f"opencode-go/{model.source_id}"),
        )

    # Then
    assert api.mutations == [f"create:opencode-go/{model.source_id}"]
    assert deployment.events == [
        "deploy_transitional",
        "verify_transitional",
        "deploy_dynamic",
        "deploy_transitional",
        "verify_transitional",
    ]


def test_row_crash_boundary_forward_resume_fingerprint_mismatch_blocks_without_mutation(
    tmp_path: Path,
) -> None:
    # Given
    model = sample_catalog_model()
    api = MemoryModelAdminApi([], [], [], [])
    deployment = _RecordingCutoverDeployment([])
    coordinator = OpenCodeGoCutoverCoordinator(
        api=api,
        state_root=tmp_path,
        deployment=deployment,
        crash_hook=lambda checkpoint: (
            (_ for _ in ()).throw(OpenCodeGoCutoverCrash(checkpoint))
            if checkpoint == "row_submitted"
            else None
        ),
    )

    # When
    with pytest.raises(OpenCodeGoCutoverCrash):
        coordinator.run(
            reconciliation_input((model,)),
            _cutover_context(f"opencode-go/{model.source_id}"),
        )
    api.rows[0] = record_for(
        build_owned_model_deployment(
            sample_catalog_model("drifted"),
            bootstrap_static=False,
        )
    )
    with pytest.raises(OpenCodeGoCutoverBlockedError, match="fingerprint"):
        coordinator.run(
            reconciliation_input((model,)),
            _cutover_context(f"opencode-go/{model.source_id}"),
        )

    # Then
    assert api.mutations == [f"create:opencode-go/{model.source_id}"]


def test_dynamic_deployed_crash_resumes_forward_without_db_mutation(tmp_path: Path) -> None:
    # Given
    model = sample_catalog_model()
    api = MemoryModelAdminApi([], [], [], [])
    deployment = _RecordingCutoverDeployment([])
    context = _cutover_context(f"opencode-go/{model.source_id}")
    crashing = OpenCodeGoCutoverCoordinator(
        api=api,
        state_root=tmp_path,
        deployment=deployment,
        crash_hook=lambda checkpoint: (
            (_ for _ in ()).throw(OpenCodeGoCutoverCrash(checkpoint))
            if checkpoint == "dynamic_deployed"
            else None
        ),
    )

    # When
    with pytest.raises(OpenCodeGoCutoverCrash):
        crashing.run(reconciliation_input((model,)), context)
    resumed = OpenCodeGoCutoverCoordinator(
        api=api,
        state_root=tmp_path,
        deployment=deployment,
    ).run(reconciliation_input((model,)), context)

    # Then
    assert resumed.status == "complete"
    assert api.mutations == [f"create:opencode-go/{model.source_id}"]
    assert deployment.events == [
        "deploy_transitional",
        "verify_transitional",
        "deploy_dynamic",
        "deploy_dynamic",
        f"verify_dynamic:opencode-go/{model.source_id}",
    ]


def test_malformed_top_level_blocked_is_rejected_before_reconciliation() -> None:
    # Given
    model = sample_catalog_model()
    deployment = build_owned_model_deployment(model, bootstrap_static=False)

    def request_fn(_: request.Request) -> JsonValue:
        return {
            "data": [
                {
                    "blocked": False,
                    "model_name": deployment.model_name,
                    "litellm_params": deployment.litellm_params.to_json(),
                    "model_info": deployment.model_info.to_json(),
                }
            ]
        }

    client = LiteLLMModelAdminClient(
        api_url="http://litellm.invalid",
        master_key="test-master-key",
        request_fn=request_fn,
    )

    # When
    with pytest.raises(LiteLLMModelAdminError, match="nested in model_info"):
        OpenCodeGoDatabaseReconciler(client).reconcile(reconciliation_input((model,)))


def test_absent_delete_400_accepts_only_proven_absence() -> None:
    # Given
    model = sample_catalog_model()
    stale = sample_catalog_model("retired-model")
    retired = build_owned_model_deployment(stale, bootstrap_static=False)
    api = MemoryModelAdminApi(
        [record_for(retired)], [], [], [], delete_returns_400=True
    )
    request = reconciliation_input(
        (model,),
        missing=(stale_record(stale.source_id, (model.source_id,)),),
    )

    # When
    result = OpenCodeGoDatabaseReconciler(api).reconcile(request)

    # Then
    assert tuple(item.kind for item in result.applied) == ("create", "delete")
    assert api.mutations[-1] == f"delete:{retired.model_id}"


def test_delete_400_with_surviving_row_blocks_without_compensation() -> None:
    # Given
    model = sample_catalog_model()
    stale = sample_catalog_model("retired-model")
    retired = build_owned_model_deployment(stale, bootstrap_static=False)
    api = MemoryModelAdminApi(
        [record_for(retired)],
        [],
        [],
        [],
        delete_returns_400=True,
        delete_preserves_row=True,
    )
    reconciliation = reconciliation_input(
        (model,),
        missing=(stale_record(stale.source_id, (model.source_id,)),),
    )

    # When
    with pytest.raises(LiteLLMModelAdminConflict, match="surviving alias"):
        OpenCodeGoDatabaseReconciler(api).reconcile(reconciliation)

    # Then
    assert api.mutations[-1] == f"delete:{retired.model_id}"


def test_visibility_failure_blocks_before_mutation() -> None:
    # Given
    model = sample_catalog_model()
    deployment = build_owned_model_deployment(model, bootstrap_static=False)
    api = _VisibilityFailureApi(record_for(deployment))

    # When
    with pytest.raises(LiteLLMModelAdminConflict, match="visibility changed"):
        OpenCodeGoDatabaseReconciler(api).reconcile(reconciliation_input((model,)))

    # Then
    assert api.mutations == []


def test_unsupported_routing_field_blocks_before_reconciliation() -> None:
    # Given
    model = sample_catalog_model()
    deployment = build_owned_model_deployment(model, bootstrap_static=False)

    def request_fn(_: request.Request) -> JsonValue:
        params = deployment.litellm_params.to_json()
        params["unknown"] = True
        return {
            "data": [
                {
                    "model_name": deployment.model_name,
                    "litellm_params": params,
                    "model_info": deployment.model_info.to_json(),
                }
            ]
        }

    client = LiteLLMModelAdminClient(
        api_url="http://litellm.invalid",
        master_key="test-master-key",
        request_fn=request_fn,
    )

    # When
    with pytest.raises(LiteLLMModelAdminError, match="exactly three keys"):
        OpenCodeGoDatabaseReconciler(client).reconcile(reconciliation_input((model,)))


@dataclass(frozen=True, slots=True)
class _VisibilityFailureApi(LiteLLMModelAdminApi):
    stable: LiteLLMModelRecord
    mutations: list[str] = field(default_factory=list)
    list_calls: list[int] = field(default_factory=list)

    def list_models(self) -> tuple[LiteLLMModelRecord, ...]:
        self.list_calls.append(len(self.list_calls))
        if len(self.list_calls) == 1:
            return (self.stable,)
        return ()

    def create_model(self, _: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        raise AssertionError("visibility failure must prevent creation")

    def update_model(self, _: LiteLLMModelDeployment) -> LiteLLMModelRecord:
        raise AssertionError("visibility failure must prevent updates")

    def delete_model(self, _: str) -> None:
        raise AssertionError("visibility failure must prevent deletion")
