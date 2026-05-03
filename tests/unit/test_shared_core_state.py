# pyright: reportMissingImports=false

from __future__ import annotations

import pytest

from dokploy_wizard.core import SharedPostgresAllocation, SharedRedisAllocation
from dokploy_wizard.state import RawEnvInput, resolve_desired_state


def test_shared_core_plan_is_deterministic_and_services_are_planned_once() -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "shared-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_MATRIX": "true",
            "ENABLE_NEXTCLOUD": "true",
            "ENABLE_OPENCLAW": "true",
        },
    )

    desired_state = resolve_desired_state(raw_env)

    assert desired_state.shared_core.network_name == "shared-stack-shared"
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.postgres.service_name == "shared-stack-shared-postgres"
    assert desired_state.shared_core.redis is not None
    assert desired_state.shared_core.redis.service_name == "shared-stack-shared-redis"
    assert desired_state.selected_packs == ("matrix", "nextcloud", "openclaw")
    assert [allocation.pack_name for allocation in desired_state.shared_core.allocations] == [
        "matrix",
        "nextcloud",
        "openclaw",
    ]
    nextcloud_allocation = desired_state.shared_core.allocations[1]
    assert nextcloud_allocation.postgres is not None
    assert nextcloud_allocation.postgres.user_name == "shared_stack_nextcloud"
    assert nextcloud_allocation.postgres.password_secret_ref == (
        "shared-stack-nextcloud-postgres-password"
    )
    assert nextcloud_allocation.redis is not None
    assert nextcloud_allocation.redis.identity_name == "shared-stack-nextcloud-redis"


def test_shared_core_plan_is_empty_when_no_selected_pack_needs_it() -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "core-only",
            "ROOT_DOMAIN": "example.com",
        },
    )

    desired_state = resolve_desired_state(raw_env)

    assert desired_state.shared_core.network_name == "core-only-shared"
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.postgres.service_name == "core-only-shared-postgres"
    assert desired_state.shared_core.redis is None
    assert desired_state.shared_core.allocations == ()
    assert desired_state.shared_core.litellm is not None
    assert desired_state.shared_core.litellm.service_name == "core-only-shared-litellm"
    assert desired_state.enabled_packs == ()


def test_shared_core_plan_allocates_distinct_postgres_for_moodle_and_docuseal() -> None:
    raw_env = RawEnvInput(
        format_version=1,
        values={
            "STACK_NAME": "shared-stack",
            "ROOT_DOMAIN": "example.com",
            "ENABLE_MOODLE": "true",
            "ENABLE_DOCUSEAL": "true",
        },
    )

    desired_state = resolve_desired_state(raw_env)

    assert desired_state.enabled_packs == ("docuseal", "moodle")
    assert desired_state.shared_core.postgres is not None
    assert desired_state.shared_core.postgres.service_name == "shared-stack-shared-postgres"
    assert desired_state.shared_core.redis is None
    assert [allocation.pack_name for allocation in desired_state.shared_core.allocations] == [
        "docuseal",
        "moodle",
    ]

    docuseal_allocation, moodle_allocation = desired_state.shared_core.allocations

    assert docuseal_allocation.postgres == SharedPostgresAllocation(
        database_name="shared_stack_docuseal",
        user_name="shared_stack_docuseal",
        password_secret_ref="shared-stack-docuseal-postgres-password",
    )
    assert docuseal_allocation.redis is None
    assert moodle_allocation.postgres == SharedPostgresAllocation(
        database_name="shared_stack_moodle",
        user_name="shared_stack_moodle",
        password_secret_ref="shared-stack-moodle-postgres-password",
    )
    assert moodle_allocation.redis is None
    assert desired_state.shared_core.mail_relay is not None
    assert desired_state.shared_core.mail_relay.service_name == "shared-stack-shared-postfix"
    assert desired_state.shared_core.mail_relay.mail_hostname == "mail.example.com"
    assert desired_state.shared_core.mail_relay.smtp_port == 587
    assert desired_state.shared_core.mail_relay.from_address == "DoNotReply@example.com"


def test_admin_credential_rejection_for_postgres_allocations() -> None:
    with pytest.raises(ValueError, match="admin/root credentials"):
        SharedPostgresAllocation(
            database_name="nextcloud",
            user_name="postgres",
            password_secret_ref="nextcloud-postgres-password",
        )


def test_admin_identity_rejection_for_redis_allocations() -> None:
    with pytest.raises(ValueError, match="admin/root identities"):
        SharedRedisAllocation(
            identity_name="default",
            password_secret_ref="nextcloud-redis-password",
        )
