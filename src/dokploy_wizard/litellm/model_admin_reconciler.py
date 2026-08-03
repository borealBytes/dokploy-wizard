from __future__ import annotations

from dataclasses import replace

from dokploy_wizard.litellm.model_admin_payload import model_uuid_for_source
from dokploy_wizard.litellm.model_admin_types import (
    LiteLLMModelAdminApi,
    LiteLLMModelAdminConflict,
    LiteLLMModelAdminError,
    LiteLLMModelAdminWriteAmbiguity,
    LiteLLMModelDeployment,
    LiteLLMModelRecord,
)


class LiteLLMModelAdminReconciler:
    def __init__(self, api: LiteLLMModelAdminApi) -> None:
        self._api = api

    def reconcile(
        self, deployments: tuple[LiteLLMModelDeployment, ...]
    ) -> tuple[LiteLLMModelRecord, ...]:
        existing = self._records_by_name(self._api.list_models())
        reconciled = tuple(
            self._reconcile_deployment(deployment, existing.get(deployment.model_name))
            for deployment in deployments
        )
        return reconciled

    def delete_owned(self, deployment: LiteLLMModelDeployment) -> None:
        records = self._records_by_name(self._api.list_models())
        record = records.get(deployment.model_name)
        if record is None or not _is_exact(record, deployment):
            raise LiteLLMModelAdminConflict(
                f"owned delete requires exact deployment {deployment.model_name}"
            )
        self._api.delete_model(deployment.model_id)

    def _reconcile_deployment(
        self,
        deployment: LiteLLMModelDeployment,
        existing: LiteLLMModelRecord | None,
    ) -> LiteLLMModelRecord:
        if existing is None:
            return self._create_with_ambiguity_check(deployment)
        if not _is_owned(existing):
            raise LiteLLMModelAdminConflict(
                f"foreign alias conflict for {deployment.model_name}"
            )
        if _is_exact(existing, deployment):
            return existing
        preserved = replace(
            deployment,
            server_model_info_extras=tuple(
                sorted(existing.server_model_info_extras.items(), key=lambda item: item[0])
            ),
        )
        return self._update_with_ambiguity_check(preserved)

    def _create_with_ambiguity_check(
        self, deployment: LiteLLMModelDeployment
    ) -> LiteLLMModelRecord:
        try:
            return self._api.create_model(deployment)
        except LiteLLMModelAdminWriteAmbiguity as exc:
            record = self._record_after_ambiguous_write(deployment)
            if _is_exact(record, deployment):
                return record
            raise LiteLLMModelAdminConflict(
                f"ambiguous create mismatch for {deployment.model_name}"
            ) from exc

    def _update_with_ambiguity_check(
        self, deployment: LiteLLMModelDeployment
    ) -> LiteLLMModelRecord:
        try:
            return self._api.update_model(deployment)
        except LiteLLMModelAdminWriteAmbiguity as exc:
            record = self._record_after_ambiguous_write(deployment)
            if _is_exact(record, deployment):
                return record
            raise LiteLLMModelAdminConflict(
                f"ambiguous update mismatch for {deployment.model_name}"
            ) from exc

    def _record_after_ambiguous_write(
        self, deployment: LiteLLMModelDeployment
    ) -> LiteLLMModelRecord:
        records = self._records_by_name(self._api.list_models())
        record = records.get(deployment.model_name)
        if record is None:
            raise LiteLLMModelAdminConflict(
                f"ambiguous write did not create {deployment.model_name}"
            )
        if not _is_owned(record):
            raise LiteLLMModelAdminConflict(
                f"ambiguous write found foreign alias {deployment.model_name}"
            )
        return record

    def _records_by_name(
        self, records: tuple[LiteLLMModelRecord, ...]
    ) -> dict[str, LiteLLMModelRecord]:
        by_name: dict[str, LiteLLMModelRecord] = {}
        for record in records:
            if record.model_name in by_name:
                raise LiteLLMModelAdminConflict(
                    f"duplicate LiteLLM deployment alias {record.model_name}"
                )
            by_name[record.model_name] = record
        return by_name


def _is_owned(record: LiteLLMModelRecord) -> bool:
    source_id = record.model_info.get("source_id")
    if not isinstance(source_id, str):
        return False
    try:
        expected_id = model_uuid_for_source(source_id)
    except LiteLLMModelAdminError:
        return False
    return (
        record.model_name == f"opencode-go/{source_id}"
        and record.model_id == expected_id
        and record.model_info.get("id") == expected_id
        and record.model_info.get("managed_by") == "dokploy-wizard"
        and record.model_info.get("managed_catalog") == "opencode-go"
    )


def _is_exact(record: LiteLLMModelRecord, deployment: LiteLLMModelDeployment) -> bool:
    expected_info = deployment.model_info.to_json()
    expected_routing = deployment.litellm_params
    return (
        _is_owned(record)
        and record.model_id == deployment.model_id
        and all(record.model_info.get(key) == value for key, value in expected_info.items())
        and record.litellm_params.model == expected_routing.model
        and record.litellm_params.api_base == expected_routing.api_base
        and (
            record.litellm_params.api_key is None
            or record.litellm_params.api_key == expected_routing.api_key
        )
    )
