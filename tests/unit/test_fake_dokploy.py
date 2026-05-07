# pyright: reportMissingImports=false

from __future__ import annotations

from .fake_dokploy import ComposeMutationCounts, FakeDokployApiClient


def test_fake_dokploy_lists_seeded_existing_service_for_already_present_paths() -> None:
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name="wizard-stack-my-farm-advisor",
        compose_id="cmp-existing",
        project_name="wizard-stack",
        status="done",
        compose_file="services: {}\n",
    )

    projects = client.list_projects()

    assert len(projects) == 1
    assert projects[0].name == "wizard-stack"
    assert projects[0].environments[0].composes[0].compose_id == "cmp-existing"
    assert projects[0].environments[0].composes[0].name == "wizard-stack-my-farm-advisor"
    assert projects[0].environments[0].composes[0].status == "done"
    assert client.compose_id_for("wizard-stack-my-farm-advisor") == "cmp-existing"
    client.assert_unchanged_service("wizard-stack-my-farm-advisor")


def test_fake_dokploy_proves_zero_update_and_deploy_calls_for_unchanged_service() -> None:
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name="wizard-stack-shared",
        compose_id="cmp-shared",
        project_name="wizard-stack",
    )

    client.assert_mutation_counts("wizard-stack-shared", create=0, update=0, deploy=0)
    assert client.mutation_counts("wizard-stack-shared") == ComposeMutationCounts()


def test_fake_dokploy_proves_single_update_deploy_pair_for_changed_service() -> None:
    client = FakeDokployApiClient()
    client.seed_existing_service(
        service_name="wizard-stack-coder",
        compose_id="cmp-coder",
        project_name="wizard-stack",
        status="running",
    )

    updated = client.update_compose(
        compose_id="cmp-coder",
        compose_file="services:\n  coder:\n    image: ghcr.io/coder/coder:latest\n",
    )
    deploy = client.deploy_compose(
        compose_id=updated.compose_id,
        title="dokploy-wizard coder reconcile",
        description="Update Coder compose app",
    )

    assert deploy.success is True
    assert client.compose_files_by_name["wizard-stack-coder"].startswith("services:\n  coder:")
    client.assert_single_update_deploy_pair("wizard-stack-coder")


def test_fake_dokploy_tracks_mutations_per_service_independently() -> None:
    client = FakeDokployApiClient(project_name="wizard-stack")

    farm = client.create_compose(
        name="wizard-stack-my-farm-advisor",
        environment_id="env-1",
        compose_file="farm-compose",
        app_name="wizard-stack-my-farm-advisor",
    )
    client.deploy_compose(
        compose_id=farm.compose_id,
        title="farm create",
        description="Create farm compose app",
    )
    openclaw = client.create_compose(
        name="wizard-stack-openclaw",
        environment_id="env-1",
        compose_file="openclaw-compose",
        app_name="wizard-stack-openclaw",
    )
    client.update_compose(compose_id=openclaw.compose_id, compose_file="openclaw-compose-v2")
    client.deploy_compose(
        compose_id=openclaw.compose_id,
        title="openclaw update",
        description="Update openclaw compose app",
    )

    client.assert_mutation_counts("wizard-stack-my-farm-advisor", create=1, update=0, deploy=1)
    client.assert_mutation_counts("wizard-stack-openclaw", create=1, update=1, deploy=1)
