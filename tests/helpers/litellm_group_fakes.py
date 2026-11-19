"""No-op access-group surface for LiteLLMAdminApi test fakes.

The wizard's workspace-key grant is a named access group (see
dokploy_wizard.litellm.admin: list_model_names / list_access_groups /
access_group_model_names / create_access_group / update_access_group).
Fakes in tests unrelated to groups inherit this stub so protocol
conformance does not force every file to re-declare the surface; the
stub reports "no sync models registered", which makes the manager's
group reconcile a documented early return. Group-specific tests
override the methods with recording behavior instead.
"""

from __future__ import annotations

from dokploy_wizard.litellm.admin import LiteLLMUserRecord


class LiteLLMGroupApiStub:
    def list_model_names(self) -> tuple[str, ...]:
        return ()

    def list_access_groups(self) -> tuple[str, ...]:
        return ()

    def access_group_model_names(self, *, name: str) -> tuple[str, ...] | None:
        return None

    def create_access_group(self, *, name: str, model_names: tuple[str, ...]) -> None:
        return None

    def update_access_group(self, *, name: str, model_names: tuple[str, ...]) -> None:
        return None

    def list_users(self) -> tuple[LiteLLMUserRecord, ...]:
        return ()

    def upsert_user(
        self, *, user_id: str, user_email: str, user_role: str, password: str
    ) -> None:
        return None
