# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, cast

import pytest

import dokploy_wizard.dokploy.moodle as moodle_module
from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.dokploy import (
    DokployApiError,
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployMoodleBackend,
    DokployProjectSummary,
)
from dokploy_wizard.dokploy.client import DokployScheduleRecord
from dokploy_wizard.dokploy.moodle import DokployMoodleApi, _local_https_health_check, _render_compose_file
from dokploy_wizard.packs.moodle import (
    MOODLE_DATA_RESOURCE_TYPE,
    MOODLE_SERVICE_RESOURCE_TYPE,
    MoodleError,
    MoodleResourceRecord,
    build_moodle_ledger,
    reconcile_moodle,
)
from dokploy_wizard.state import OwnedResource, OwnershipLedger, RawEnvInput, resolve_desired_state


@dataclass
class FakeMoodleBackend:
    existing_service: MoodleResourceRecord | None = None
    existing_data: MoodleResourceRecord | None = None
    health_ok: bool = True
    health_results: list[bool] | None = None
    ensure_calls: int = 0

    def get_service(self, resource_id: str) -> MoodleResourceRecord | None:
        if self.existing_service is not None and self.existing_service.resource_id == resource_id:
            return self.existing_service
        return None

    def find_service_by_name(self, resource_name: str) -> MoodleResourceRecord | None:
        if (
            self.existing_service is not None
            and self.existing_service.resource_name == resource_name
        ):
            return self.existing_service
        return None

    def create_service(self, **kwargs: object) -> MoodleResourceRecord:
        resource_name = str(kwargs["resource_name"])
        self.existing_service = MoodleResourceRecord(
            resource_id="moodle-service-1",
            resource_name=resource_name,
        )
        return self.existing_service

    def update_service(self, **kwargs: object) -> MoodleResourceRecord:
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> MoodleResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_id == resource_id:
            return self.existing_data
        return None

    def find_persistent_data_by_name(self, resource_name: str) -> MoodleResourceRecord | None:
        if self.existing_data is not None and self.existing_data.resource_name == resource_name:
            return self.existing_data
        return None

    def create_persistent_data(self, resource_name: str) -> MoodleResourceRecord:
        self.existing_data = MoodleResourceRecord(
            resource_id="moodle-data-1",
            resource_name=resource_name,
        )
        return self.existing_data

    def check_health(self, *, service: MoodleResourceRecord, url: str) -> bool:
        del service, url
        if self.health_results is not None:
            if self.health_results:
                return self.health_results.pop(0)
            return self.health_ok
        return self.health_ok

    def ensure_application_ready(self) -> tuple[str, ...]:
        self.ensure_calls += 1
        return ("Moodle bootstrap placeholder completed.",)


@dataclass
class FakeDokployMoodleApiClient:
    projects: list[DokployProjectSummary] = None  # type: ignore[assignment]
    schedules: list[DokployScheduleRecord] = None  # type: ignore[assignment]
    schedule_auth_error: str | None = None
    create_project_calls: int = 0
    create_compose_calls: int = 0
    update_compose_calls: int = 0
    deploy_calls: int = 0
    last_create_compose_file: str | None = None
    last_update_compose_file: str | None = None

    def __post_init__(self) -> None:
        if self.projects is None:
            self.projects = []
        if self.schedules is None:
            self.schedules = []

    def list_projects(self) -> tuple[DokployProjectSummary, ...]:
        return tuple(self.projects)

    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject:
        del description, env
        self.create_project_calls += 1
        self.projects.append(
            DokployProjectSummary(
                project_id="proj-1",
                name=name,
                environments=(
                    DokployEnvironmentSummary(
                        environment_id="env-1",
                        name="production",
                        is_default=True,
                        composes=(),
                    ),
                ),
            )
        )
        return DokployCreatedProject(project_id="proj-1", environment_id="env-1")

    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord:
        del app_name
        self.create_compose_calls += 1
        self.last_create_compose_file = compose_file
        record = DokployComposeRecord(compose_id="cmp-1", name=name)
        self.projects[0] = DokployProjectSummary(
            project_id="proj-1",
            name=self.projects[0].name,
            environments=(
                DokployEnvironmentSummary(
                    environment_id=environment_id,
                    name="production",
                    is_default=True,
                    composes=(
                        DokployComposeSummary(
                            compose_id=record.compose_id,
                            name=record.name,
                            status=None,
                        ),
                    ),
                ),
            ),
        )
        return record

    def update_compose(self, *, compose_id: str, compose_file: str) -> DokployComposeRecord:
        self.update_compose_calls += 1
        self.last_update_compose_file = compose_file
        return DokployComposeRecord(compose_id=compose_id, name="wizard-stack-moodle")

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        self.deploy_calls += 1
        return DokployDeployResult(success=True, compose_id=compose_id, message="queued")

    def list_compose_schedules(self, *, compose_id: str) -> tuple[DokployScheduleRecord, ...]:
        del compose_id
        if self.schedule_auth_error is not None:
            raise DokployApiError(self.schedule_auth_error)
        return tuple(self.schedules)

    def create_schedule(
        self,
        *,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> DokployScheduleRecord:
        del compose_id
        record = DokployScheduleRecord(
            schedule_id="sch-1",
            name=name,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
        )
        self.schedules = [record]
        return record

    def update_schedule(
        self,
        *,
        schedule_id: str,
        name: str,
        compose_id: str,
        service_name: str,
        cron_expression: str,
        timezone: str,
        shell_type: str,
        command: str,
        enabled: bool,
    ) -> DokployScheduleRecord:
        del compose_id
        record = DokployScheduleRecord(
            schedule_id=schedule_id,
            name=name,
            service_name=service_name,
            cron_expression=cron_expression,
            timezone=timezone,
            shell_type=shell_type,
            command=command,
            enabled=enabled,
        )
        self.schedules = [record]
        return record


def test_render_moodle_compose_includes_shared_postgres_data_and_forwarded_https() -> None:
    compose = _render_compose_file(
        stack_name="wizard-stack",
        hostname="moodle.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_moodle",
            user_name="wizard_stack_moodle",
            password_secret_ref="wizard-stack-moodle-postgres-password",
        ),
    )

    assert "image: moodlehq/moodle-php-apache:8.4-bullseye" in compose
    assert 'DOKPLOY_WIZARD_MOODLE_WWWROOT: "https://moodle.example.com"' in compose
    assert 'DOKPLOY_WIZARD_MOODLE_DATAROOT: "/var/moodledata/files"' in compose
    assert 'DOKPLOY_WIZARD_MOODLE_DBHOST: "wizard-stack-shared-postgres"' in compose
    assert 'DOKPLOY_WIZARD_MOODLE_DBNAME: "wizard_stack_moodle"' in compose
    assert 'DOKPLOY_WIZARD_MOODLE_DBUSER: "wizard_stack_moodle"' in compose
    assert 'DOKPLOY_WIZARD_MOODLE_DBPASS: "change-me"' in compose
    assert 'DOKPLOY_WIZARD_MOODLE_CONFIG_CACHE: "/var/moodledata/config.php"' in compose
    assert 'DOKPLOY_WIZARD_MOODLE_SOURCE_REF: "MOODLE_500_STABLE"' in compose
    assert (
        'DOKPLOY_WIZARD_MOODLE_SOURCE_ARCHIVE_URL: "https://github.com/moodle/moodle/archive/refs/heads/MOODLE_500_STABLE.tar.gz"'
        in compose
    )
    assert "wizard-stack-moodle-data:/var/moodledata" in compose
    assert "if [ ! -f /var/www/html/admin/cli/install.php ]; then" in compose
    assert 'curl -fsSL https://github.com/moodle/moodle/archive/refs/heads/MOODLE_500_STABLE.tar.gz -o "$${tmp_archive}"' in compose
    assert 'tar -xzf "$${tmp_archive}" -C /var/www/html --strip-components=1' in compose
    assert (
        'traefik.http.routers.wizard-stack-moodle.middlewares: "wizard-stack-moodle-forwarded-https"'
        in compose
    )
    assert (
        'traefik.http.middlewares.wizard-stack-moodle-forwarded-https.headers.customrequestheaders.X-Forwarded-Proto: "https"'
        in compose
    )
    assert (
        'traefik.http.middlewares.wizard-stack-moodle-forwarded-https.headers.customrequestheaders.X-Forwarded-Host: "moodle.example.com"'
        in compose
    )


def test_configure_moodle_smtp_uses_local_postfix_sender() -> None:
    commands: list[str] = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        moodle_module,
        "_run_container_shell",
        lambda container_name, shell_command, *, error_prefix: commands.append(shell_command),
    )

    moodle_module._configure_moodle_smtp(
        "moodle-container",
        smtp_host="wizard-stack-shared-postfix",
        smtp_port=587,
        from_address="DoNotReply@example.com",
    )
    monkeypatch.undo()

    assert commands == [
        "set -eu && php /var/www/html/admin/cli/cfg.php --name=smtphosts --set=wizard-stack-shared-postfix:587 && php /var/www/html/admin/cli/cfg.php --name=smtpsecure --set='' && php /var/www/html/admin/cli/cfg.php --name=smtpauthtype --set='' && php /var/www/html/admin/cli/cfg.php --name=smtpuser --set='' && php /var/www/html/admin/cli/cfg.php --name=smtppass --set='' && php /var/www/html/admin/cli/cfg.php --name=noreplyaddress --set=DoNotReply@example.com"
    ]


def test_dokploy_moodle_backend_create_and_update_paths_keep_single_compose_stable() -> None:
    create_client = FakeDokployMoodleApiClient()
    create_backend = DokployMoodleBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="moodle.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_moodle",
            user_name="wizard_stack_moodle",
            password_secret_ref="wizard-stack-moodle-postgres-password",
        ),
        client=cast(DokployMoodleApi, create_client),
    )

    created = create_backend.create_service(
        resource_name="wizard-stack-moodle",
        hostname="moodle.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_moodle",
            user_name="wizard_stack_moodle",
            password_secret_ref="wizard-stack-moodle-postgres-password",
        ),
        data_resource_name="wizard-stack-moodle-data",
    )

    assert created.resource_id == "dokploy-compose:cmp-1:service"
    assert create_client.create_project_calls == 1
    assert create_client.create_compose_calls == 1
    assert create_client.update_compose_calls == 0
    assert create_client.deploy_calls == 1
    assert create_client.last_create_compose_file is not None

    existing_project = DokployProjectSummary(
        project_id="proj-1",
        name="wizard-stack",
        environments=(
            DokployEnvironmentSummary(
                environment_id="env-1",
                name="production",
                is_default=True,
                composes=(
                    DokployComposeSummary(
                        compose_id="cmp-existing",
                        name="wizard-stack-moodle",
                        status="done",
                    ),
                ),
            ),
        ),
    )
    update_client = FakeDokployMoodleApiClient(projects=[existing_project])
    update_backend = DokployMoodleBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="moodle.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_moodle",
            user_name="wizard_stack_moodle",
            password_secret_ref="wizard-stack-moodle-postgres-password",
        ),
        client=cast(DokployMoodleApi, update_client),
    )

    updated = update_backend.create_service(
        resource_name="wizard-stack-moodle",
        hostname="moodle.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_moodle",
            user_name="wizard_stack_moodle",
            password_secret_ref="wizard-stack-moodle-postgres-password",
        ),
        data_resource_name="wizard-stack-moodle-data",
    )

    assert updated.resource_id == "dokploy-compose:cmp-existing:service"
    assert update_client.create_compose_calls == 0
    assert update_client.update_compose_calls == 1
    assert update_client.deploy_calls == 1
    assert update_client.last_update_compose_file is not None


def test_reconcile_moodle_runs_application_bootstrap_before_final_health_gate_on_first_apply() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MOODLE": "true",
            },
        )
    )
    backend = FakeMoodleBackend(health_ok=True, health_results=[False, True])

    phase = reconcile_moodle(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=backend,
    )

    assert backend.ensure_calls == 1
    assert phase.result.outcome == "applied"
    assert phase.result.health_check is not None
    assert phase.result.health_check.passed is True


def test_ensure_application_ready_installs_moodle_and_ensures_cron_on_first_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeDokployMoodleApiClient()
    backend = DokployMoodleBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="moodle.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_moodle",
            user_name="wizard_stack_moodle",
            password_secret_ref="wizard-stack-moodle-postgres-password",
        ),
        client=cast(DokployMoodleApi, client),
    )
    backend._applied_locator = moodle_module._ComposeLocator(
        project_id="proj-1",
        environment_id="env-1",
        compose_id="cmp-1",
    )
    backend._created_in_process = True

    monkeypatch.setattr(moodle_module, "_wait_for_container_name", lambda service_name: "moodle-container")
    monkeypatch.setattr(moodle_module, "_prepare_moodle_runtime", lambda container_name: None)
    monkeypatch.setattr(moodle_module, "_moodle_is_initialized", lambda container_name: False)
    install_calls: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(
        moodle_module,
        "_install_moodle",
        lambda *, container_name, hostname, postgres_service_name, postgres, admin_email, admin_password: install_calls.append(
            (container_name, hostname, postgres_service_name, admin_email)
        ),
    )
    persist_calls: list[str] = []
    monkeypatch.setattr(
        moodle_module,
        "_persist_moodle_config",
        lambda container_name: persist_calls.append(container_name),
    )
    repair_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        moodle_module,
        "_repair_moodle_config_file",
        lambda container_name, config_path: repair_calls.append((container_name, config_path)),
    )
    waited_urls: list[str] = []
    monkeypatch.setattr(
        moodle_module,
        "_wait_for_local_https_health",
        lambda url: waited_urls.append(url) or True,
    )

    notes = backend.ensure_application_ready()

    assert install_calls == [
        (
            "moodle-container",
            "moodle.example.com",
            "wizard-stack-shared-postgres",
            "admin@example.com",
        )
    ]
    assert repair_calls == [("moodle-container", "/var/www/html/config.php")]
    assert persist_calls == ["moodle-container"]
    assert waited_urls == ["https://moodle.example.com/login/index.php"]
    assert notes == (
        "Installed Moodle via admin/cli/install.php.",
        "Ensured managed Moodle cron schedule 'wizard-stack-moodle-cron'.",
    )
    assert client.schedules == [
        DokployScheduleRecord(
            schedule_id="sch-1",
            name="wizard-stack-moodle-cron",
            service_name="wizard-stack-moodle",
            cron_expression="* * * * *",
            timezone="UTC",
            shell_type="bash",
            command="php /var/www/html/admin/cli/cron.php >/dev/null 2>&1",
            enabled=True,
        )
    ]


def test_ensure_application_ready_degrades_gracefully_when_schedule_auth_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeDokployMoodleApiClient(schedule_auth_error="Dokploy API request failed: 401")
    backend = DokployMoodleBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="moodle.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_moodle",
            user_name="wizard_stack_moodle",
            password_secret_ref="wizard-stack-moodle-postgres-password",
        ),
        client=cast(DokployMoodleApi, client),
    )
    backend._applied_locator = moodle_module._ComposeLocator(
        project_id="proj-1",
        environment_id="env-1",
        compose_id="cmp-1",
    )

    monkeypatch.setattr(moodle_module, "_find_container_name", lambda service_name: "moodle-container")
    monkeypatch.setattr(moodle_module, "_prepare_moodle_runtime", lambda container_name: None)
    monkeypatch.setattr(moodle_module, "_moodle_is_initialized", lambda container_name: True)
    monkeypatch.setattr(moodle_module, "_wait_for_local_https_health", lambda url: True)

    notes = backend.ensure_application_ready()

    assert notes == (
        "Moodle already initialized; skipped CLI install.",
        "Skipped Moodle cron schedule reconciliation because Dokploy schedule auth is not available yet: Dokploy API request failed: 401",
    )
    assert client.schedules == []


def test_install_moodle_derives_admin_username_from_admin_email_local_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []
    monkeypatch.setattr(
        moodle_module,
        "_run_container_shell",
        lambda container_name, shell_command, *, error_prefix: commands.append(shell_command),
    )

    moodle_module._install_moodle(
        container_name="moodle-container",
        hostname="moodle.example.com",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_moodle",
            user_name="wizard_stack_moodle",
            password_secret_ref="wizard-stack-moodle-postgres-password",
        ),
        admin_email="Clayton.Superior+Ops@example.com",
        admin_password="ChangeMeSoon",
    )

    assert len(commands) == 1
    assert "--adminuser=clayton_superior_ops" in commands[0]
    assert "--adminuser=admin" not in commands[0]


def test_prepare_moodle_runtime_bootstraps_official_source_without_wiping_existing_docroot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []
    monkeypatch.setattr(
        moodle_module,
        "_run_container_shell",
        lambda container_name, shell_command, *, error_prefix: commands.append(shell_command),
    )

    moodle_module._prepare_moodle_runtime("moodle-container")

    assert len(commands) == 1
    assert "if [ ! -f /var/www/html/admin/cli/install.php ]; then" in commands[0]
    assert (
        "https://github.com/moodle/moodle/archive/refs/heads/MOODLE_500_STABLE.tar.gz"
        in commands[0]
    )
    assert 'curl -fsSL https://github.com/moodle/moodle/archive/refs/heads/MOODLE_500_STABLE.tar.gz -o "${tmp_archive}"' in commands[0]
    assert "rm -rf /var/www/html/*" not in commands[0]
    assert 'mkdir -p /var/www/html' in commands[0]
    assert 'tar -xzf "${tmp_archive}" -C /var/www/html --strip-components=1' in commands[0]
    assert 'rm -rf "${tmp_archive}"' in commands[0]
    assert "if [ -f /var/moodledata/config.php ]; then chmod 0644 /var/moodledata/config.php && rm -f /var/www/html/config.php && cp /var/moodledata/config.php /var/www/html/config.php && chmod 0644 /var/www/html/config.php; fi" in commands[0]
    assert "chown -R www-data:www-data /var/moodledata/files" in commands[0]
    assert "find /var/moodledata/files -type d -exec chmod 0770 {} +" in commands[0]
    assert "find /var/moodledata/files -type f -exec chmod 0660 {} +" in commands[0]
    assert "for plugin_parent in /var/www/html/admin/tool" in commands[0]
    assert "/var/www/html/local" in commands[0]
    assert "/var/www/html/mod" in commands[0]
    assert "/var/www/html/theme" in commands[0]
    assert 'chown root:www-data "${plugin_parent}"' in commands[0]
    assert 'chmod 2775 "${plugin_parent}"' in commands[0]
    assert "chmod -R 0777 /var/www/html" not in commands[0]
    assert "chown -R www-data:www-data /var/www/html" not in commands[0]
    assert 'php -r' in commands[0]
    assert '$CFG->sslproxy = true;' in commands[0]
    assert '$CFG->reverseproxy = true;' not in commands[0]


def test_compose_escape_shell_doubles_dollars_without_changing_shell_shape() -> None:
    shell_script = 'curl -o "${tmp_archive}" && tar -xzf "${tmp_archive}" -C "${tmp_extract}"'

    escaped = moodle_module._compose_escape_shell(shell_script)

    assert escaped == 'curl -o "$${tmp_archive}" && tar -xzf "$${tmp_archive}" -C "$${tmp_extract}"'


def test_persist_moodle_config_repairs_permissions_before_relink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []
    monkeypatch.setattr(
        moodle_module,
        "_run_container_shell",
        lambda container_name, shell_command, *, error_prefix: commands.append(shell_command),
    )

    moodle_module._persist_moodle_config("moodle-container")

    assert commands == [
        "set -eu && cp /var/www/html/config.php /var/moodledata/config.php && chmod 0644 /var/moodledata/config.php && chmod 0644 /var/www/html/config.php"
    ]


def test_repair_moodle_config_file_inserts_proxy_flags_before_setup_require(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []
    monkeypatch.setattr(
        moodle_module,
        "_run_container_shell",
        lambda container_name, shell_command, *, error_prefix: commands.append(shell_command),
    )

    moodle_module._repair_moodle_config_file("moodle-container", "/var/www/html/config.php")

    assert len(commands) == 1
    assert commands[0].startswith("set -eu && if [ -f /var/www/html/config.php ]; then php -r ")
    assert '$CFG->sslproxy = true;' in commands[0]
    assert '$CFG->reverseproxy = true;' not in commands[0]
    assert 'strpos($content,' in commands[0]
    assert '/lib/setup.php' in commands[0]
    assert 'Unable to patch Moodle proxy config' in commands[0]
    assert commands[0].endswith(" && chmod 0644 /var/www/html/config.php; fi")


def test_moodle_admin_username_derivation_has_safe_fallback_for_invalid_local_part() -> None:
    assert moodle_module._moodle_admin_username_from_email("Clayton.Superior+Ops@example.com") == (
        "clayton_superior_ops"
    )
    assert moodle_module._moodle_admin_username_from_email("...@example.com") == "admin"


def test_ensure_application_ready_is_rerun_safe_when_moodle_is_already_initialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeDokployMoodleApiClient(
        schedules=[
            DokployScheduleRecord(
                schedule_id="sch-1",
                name="wizard-stack-moodle-cron",
                service_name="wizard-stack-moodle",
                cron_expression="0 * * * *",
                timezone="America/Detroit",
                shell_type="bash",
                command="php /var/www/html/admin/cli/cron.php >/dev/null 2>&1",
                enabled=True,
            )
        ]
    )
    backend = DokployMoodleBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="moodle.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_moodle",
            user_name="wizard_stack_moodle",
            password_secret_ref="wizard-stack-moodle-postgres-password",
        ),
        client=cast(DokployMoodleApi, client),
    )
    backend._applied_locator = moodle_module._ComposeLocator(
        project_id="proj-1",
        environment_id="env-1",
        compose_id="cmp-1",
    )

    monkeypatch.setattr(moodle_module, "_find_container_name", lambda service_name: "moodle-container")
    monkeypatch.setattr(moodle_module, "_prepare_moodle_runtime", lambda container_name: None)
    monkeypatch.setattr(moodle_module, "_moodle_is_initialized", lambda container_name: True)
    install_calls: list[str] = []
    monkeypatch.setattr(
        moodle_module,
        "_install_moodle",
        lambda **kwargs: install_calls.append("called"),
    )
    monkeypatch.setattr(moodle_module, "_persist_moodle_config", lambda container_name: None)
    monkeypatch.setattr(moodle_module, "_wait_for_local_https_health", lambda url: True)

    notes = backend.ensure_application_ready()

    assert install_calls == []
    assert notes == (
        "Moodle already initialized; skipped CLI install.",
        "Ensured managed Moodle cron schedule 'wizard-stack-moodle-cron'.",
    )
    assert client.schedules == [
        DokployScheduleRecord(
            schedule_id="sch-1",
            name="wizard-stack-moodle-cron",
            service_name="wizard-stack-moodle",
            cron_expression="* * * * *",
            timezone="UTC",
            shell_type="bash",
            command="php /var/www/html/admin/cli/cron.php >/dev/null 2>&1",
            enabled=True,
        )
    ]


def test_dokploy_moodle_health_waits_for_public_route_on_first_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DokployMoodleBackend(
        api_url="https://dokploy.example.com/api",
        api_key="key-123",
        stack_name="wizard-stack",
        hostname="moodle.example.com",
        admin_email="admin@example.com",
        admin_password="ChangeMeSoon",
        postgres_service_name="wizard-stack-shared-postgres",
        postgres=SharedPostgresAllocation(
            database_name="wizard_stack_moodle",
            user_name="wizard_stack_moodle",
            password_secret_ref="wizard-stack-moodle-postgres-password",
        ),
        client=cast(DokployMoodleApi, FakeDokployMoodleApiClient()),
    )
    backend._created_in_process = True
    monkeypatch.setattr(moodle_module, "_local_https_health_check", lambda url: False)
    monkeypatch.setattr(moodle_module, "_public_https_health_check", lambda url: False)
    waited_urls: list[str] = []
    monkeypatch.setattr(
        moodle_module,
        "_wait_for_public_https_health",
        lambda url: waited_urls.append(url) or True,
    )

    ok = backend.check_health(
        service=MoodleResourceRecord("moodle-service-1", "wizard-stack-moodle"),
        url="https://moodle.example.com/login/index.php",
    )

    assert ok is True
    assert waited_urls == ["https://moodle.example.com/login/index.php"]


def test_local_https_health_check_uses_host_header_against_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    monkeypatch.setattr(moodle_module.ssl, "create_default_context", lambda: type("Ctx", (), {"check_hostname": True, "verify_mode": None})())

    def fake_urlopen(req: Any, timeout: int, context: object) -> FakeResponse:
        captured["full_url"] = req.full_url
        captured["host"] = req.headers.get("Host")
        captured["timeout"] = timeout
        captured["check_hostname"] = getattr(context, "check_hostname", None)
        return FakeResponse()

    monkeypatch.setattr(moodle_module.urlrequest, "urlopen", fake_urlopen)

    ok = _local_https_health_check("https://moodle.example.com/login/index.php?test=1")

    assert ok is True
    assert captured == {
        "full_url": "https://127.0.0.1/login/index.php?test=1",
        "host": "moodle.example.com",
        "timeout": 15,
        "check_hostname": False,
    }


def test_reconcile_moodle_creates_service_and_data() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MOODLE": "true",
            },
        )
    )

    phase = reconcile_moodle(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeMoodleBackend(),
    )

    assert phase.result.outcome == "applied"
    assert phase.result.hostname == "moodle.example.com"
    assert phase.service_resource_id == "moodle-service-1"
    assert phase.data_resource_id == "moodle-data-1"
    assert phase.result.service is not None
    assert phase.result.persistent_data is not None
    assert phase.result.health_check is not None
    assert phase.result.health_check.passed is True
    assert phase.result.config is not None
    assert phase.result.config.access_url == "https://moodle.example.com"
    assert phase.result.config.postgres.database_name == "wizard_stack_moodle"


def test_reconcile_moodle_returns_plan_only_shape_for_dry_run() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MOODLE": "true",
            },
        )
    )

    phase = reconcile_moodle(
        dry_run=True,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(format_version=1, resources=()),
        backend=FakeMoodleBackend(),
    )

    assert phase.result.outcome == "plan_only"
    assert phase.service_resource_id is None
    assert phase.data_resource_id is None
    assert phase.result.service is not None
    assert phase.result.service.action == "create"
    assert phase.result.persistent_data is not None
    assert phase.result.persistent_data.action == "create"
    assert phase.result.health_check is not None
    assert phase.result.health_check.url == "https://moodle.example.com/login/index.php"
    assert phase.result.health_check.passed is None
    assert phase.result.config is not None
    assert phase.result.config.postgres.user_name == "wizard_stack_moodle"


def test_reconcile_moodle_fails_when_hostname_missing() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MOODLE": "true",
            },
        )
    )
    desired_state = replace(desired_state, hostnames={})

    with pytest.raises(MoodleError, match="canonical Moodle hostname"):
        reconcile_moodle(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeMoodleBackend(),
        )


def test_reconcile_moodle_fails_when_shared_postgres_allocation_missing() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MOODLE": "true",
            },
        )
    )
    desired_state = replace(
        desired_state,
        shared_core=replace(desired_state.shared_core, allocations=()),
    )

    with pytest.raises(MoodleError, match="shared-core postgres allocation is missing"):
        reconcile_moodle(
            dry_run=False,
            desired_state=desired_state,
            ownership_ledger=OwnershipLedger(format_version=1, resources=()),
            backend=FakeMoodleBackend(),
        )


def test_reconcile_moodle_reuses_owned_resources_and_reports_already_present() -> None:
    desired_state = resolve_desired_state(
        RawEnvInput(
            format_version=1,
            values={
                "STACK_NAME": "wizard-stack",
                "ROOT_DOMAIN": "example.com",
                "ENABLE_MOODLE": "true",
            },
        )
    )
    backend = FakeMoodleBackend(
        existing_service=MoodleResourceRecord(
            resource_id="moodle-service-1",
            resource_name="wizard-stack-moodle",
        ),
        existing_data=MoodleResourceRecord(
            resource_id="moodle-data-1",
            resource_name="wizard-stack-moodle-data",
        ),
        health_results=[False, True],
    )

    phase = reconcile_moodle(
        dry_run=False,
        desired_state=desired_state,
        ownership_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type=MOODLE_SERVICE_RESOURCE_TYPE,
                    resource_id="moodle-service-1",
                    scope="stack:wizard-stack:moodle:service",
                ),
                OwnedResource(
                    resource_type=MOODLE_DATA_RESOURCE_TYPE,
                    resource_id="moodle-data-1",
                    scope="stack:wizard-stack:moodle:data",
                ),
            ),
        ),
        backend=backend,
    )

    assert phase.result.outcome == "already_present"
    assert phase.result.service is not None
    assert phase.result.service.action == "update_owned"
    assert phase.result.persistent_data is not None
    assert phase.result.persistent_data.action == "reuse_owned"
    assert phase.result.health_check is not None
    assert phase.result.health_check.passed is True
    assert backend.ensure_calls == 1


def test_build_moodle_ledger_replaces_prior_owned_resources_cleanly() -> None:
    ledger = build_moodle_ledger(
        existing_ledger=OwnershipLedger(
            format_version=1,
            resources=(
                OwnedResource(
                    resource_type=MOODLE_SERVICE_RESOURCE_TYPE,
                    resource_id="old-service",
                    scope="stack:wizard-stack:moodle:service",
                ),
                OwnedResource(
                    resource_type=MOODLE_DATA_RESOURCE_TYPE,
                    resource_id="old-data",
                    scope="stack:wizard-stack:moodle:data",
                ),
                OwnedResource(
                    resource_type="cloudflare_tunnel",
                    resource_id="tunnel-1",
                    scope="account:account-123",
                ),
            ),
        ),
        stack_name="wizard-stack",
        service_resource_id="new-service",
        data_resource_id="new-data",
    )

    assert {(item.resource_type, item.resource_id, item.scope) for item in ledger.resources} == {
        (MOODLE_SERVICE_RESOURCE_TYPE, "new-service", "stack:wizard-stack:moodle:service"),
        (MOODLE_DATA_RESOURCE_TYPE, "new-data", "stack:wizard-stack:moodle:data"),
        ("cloudflare_tunnel", "tunnel-1", "account:account-123"),
    }
