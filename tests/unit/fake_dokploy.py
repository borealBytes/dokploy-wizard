from __future__ import annotations

from dataclasses import dataclass, field

from dokploy_wizard.dokploy.client import (
    DokployComposeRecord,
    DokployComposeSummary,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)


@dataclass(frozen=True)
class ComposeMutationCounts:
    create: int = 0
    update: int = 0
    deploy: int = 0


@dataclass
class FakeDokployApiClient:
    project_name: str | None = None
    created_project: DokployCreatedProject = DokployCreatedProject(
        project_id="proj-1",
        environment_id="env-1",
    )
    environment_name: str = "production"
    compose_names_by_id: dict[str, str] = field(default_factory=dict)
    compose_files_by_name: dict[str, str] = field(default_factory=dict)
    compose_status_by_name: dict[str, str | None] = field(default_factory=dict)
    create_calls_by_name: dict[str, int] = field(default_factory=dict)
    update_calls_by_name: dict[str, int] = field(default_factory=dict)
    deploy_calls_by_name: dict[str, int] = field(default_factory=dict)
    create_project_calls: int = 0
    next_compose_number: int = 1

    def seed_existing_service(
        self,
        *,
        service_name: str,
        compose_id: str,
        project_name: str,
        status: str | None = "done",
        compose_file: str | None = None,
    ) -> None:
        self.project_name = project_name
        self.compose_names_by_id[compose_id] = service_name
        self.compose_status_by_name[service_name] = status
        if compose_file is not None:
            self.compose_files_by_name[service_name] = compose_file

    def list_projects(self) -> tuple[DokployProjectSummary, ...]:
        if self.project_name is None:
            return ()
        return (
            DokployProjectSummary(
                project_id=self.created_project.project_id,
                name=self.project_name,
                environments=(
                    DokployEnvironmentSummary(
                        environment_id=self.created_project.environment_id,
                        name=self.environment_name,
                        is_default=True,
                        composes=tuple(
                            DokployComposeSummary(
                                compose_id=compose_id,
                                name=service_name,
                                status=self.compose_status_by_name.get(service_name),
                            )
                            for compose_id, service_name in self.compose_names_by_id.items()
                        ),
                    ),
                ),
            ),
        )

    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject:
        del description, env
        self.create_project_calls += 1
        self.project_name = name
        return self.created_project

    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord:
        del environment_id, app_name
        compose_id = f"cmp-{self.next_compose_number}"
        self.next_compose_number += 1
        self.compose_names_by_id[compose_id] = name
        self.compose_files_by_name[name] = compose_file
        self.compose_status_by_name.setdefault(name, None)
        self.create_calls_by_name[name] = self.create_calls_by_name.get(name, 0) + 1
        return DokployComposeRecord(compose_id=compose_id, name=name)

    def update_compose(self, *, compose_id: str, compose_file: str) -> DokployComposeRecord:
        service_name = self.compose_names_by_id[compose_id]
        self.compose_files_by_name[service_name] = compose_file
        self.update_calls_by_name[service_name] = self.update_calls_by_name.get(service_name, 0) + 1
        return DokployComposeRecord(compose_id=compose_id, name=service_name)

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult:
        del title, description
        service_name = self.compose_names_by_id[compose_id]
        self.deploy_calls_by_name[service_name] = self.deploy_calls_by_name.get(service_name, 0) + 1
        return DokployDeployResult(success=True, compose_id=compose_id, message="queued")

    def compose_id_for(self, service_name: str) -> str:
        for compose_id, name in self.compose_names_by_id.items():
            if name == service_name:
                return compose_id
        raise KeyError(service_name)

    def mutation_counts(self, service_name: str) -> ComposeMutationCounts:
        return ComposeMutationCounts(
            create=self.create_calls_by_name.get(service_name, 0),
            update=self.update_calls_by_name.get(service_name, 0),
            deploy=self.deploy_calls_by_name.get(service_name, 0),
        )

    def assert_mutation_counts(
        self,
        service_name: str,
        *,
        create: int = 0,
        update: int = 0,
        deploy: int = 0,
    ) -> None:
        counts = self.mutation_counts(service_name)
        assert counts == ComposeMutationCounts(create=create, update=update, deploy=deploy)

    def assert_unchanged_service(self, service_name: str) -> None:
        self.assert_mutation_counts(service_name, create=0, update=0, deploy=0)

    def assert_single_update_deploy_pair(self, service_name: str) -> None:
        self.assert_mutation_counts(service_name, create=0, update=1, deploy=1)
