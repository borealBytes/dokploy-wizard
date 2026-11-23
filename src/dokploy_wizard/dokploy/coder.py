# mypy: ignore-errors
# ruff: noqa: E501
"""Dokploy-backed Coder runtime backend."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib import error as urlerror
from urllib import parse
from urllib import request as urlrequest

from dokploy_wizard.core.models import SharedPostgresAllocation
from dokploy_wizard.dokploy.client import (
    DokployApiClient,
    DokployApiError,
    DokployComposeRecord,
    DokployCreatedProject,
    DokployDeployResult,
    DokployEnvironmentSummary,
    DokployProjectSummary,
)
from dokploy_wizard.dokploy.compose_noop import (
    apply_compose_noop_guard,
    apply_rendered_compose_to_existing,
    persist_compose_artifact_hash,
)
from dokploy_wizard.dokploy.container_resolution import resolve_compose_container_name
from dokploy_wizard.dokploy.env_spec import DokployEnvSpec, DokployEnvVar, RenderedCompose
from dokploy_wizard.identity import display_name_from_email, username_from_email
from dokploy_wizard.litellm.admin import (
    LITELLM_USER_ROLE_ADMIN,
    LITELLM_USER_ROLE_INTERNAL,
    WORKSPACE_KEY_DENY_MODEL,
    WORKSPACE_KEY_GRANTED_MODELS,
    LiteLLMAdminApi,
    LiteLLMGatewayManager,
    LiteLLMUserAssignment,
)
from dokploy_wizard.litellm.model_catalog import DEFAULT_LOCAL_CANONICAL_ALIAS
from dokploy_wizard.packs.coder import (
    CoderError,
    CoderResourceRecord,
    coder_identity_timer_resource,
)
from dokploy_wizard.state import load_state_dir
from dokploy_wizard.state.models import (
    FilexProvisionerState,
    GiteaProvisionerState,
    OwnedResource,
)
from dokploy_wizard.verification import make_verification_result, redact_text


class DokployCoderApi(Protocol):
    def list_projects(self) -> tuple[DokployProjectSummary, ...]: ...
    def create_project(
        self, *, name: str, description: str | None, env: str | None
    ) -> DokployCreatedProject: ...
    def create_compose(
        self, *, name: str, environment_id: str, compose_file: str, app_name: str
    ) -> DokployComposeRecord: ...
    def update_compose(
        self, *, compose_id: str, compose_file: str | None = None, env: str | None = None
    ) -> DokployComposeRecord: ...
    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult: ...


@dataclass(frozen=True)
class _ComposeLocator:
    project_id: str
    environment_id: str
    compose_id: str


_DEFAULT_AI_DEFAULT_PROVIDER = "opencode-go"
_DEFAULT_AI_DEFAULT_MODEL = "deepseek-v4.1-flash"
_DEFAULT_HERMES_INFERENCE_PROVIDER = "dokploy-litellm"
_DEFAULT_HERMES_MODEL = "opencode-go/deepseek-v4.1-flash"
# The Hermes Desktop tile serves the browser-hosted Desktop, which merged upstream on
# 2026-10-25 and has not shipped in a release yet, so that workspace tracks the channel
# carrying it. Anything older has no `hermes webapp` at all.
_DEFAULT_HERMES_DESKTOP_CHANNEL = "main"
_HERMES_CHANNEL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9._-]*")
_DEFAULT_AI_DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
_DEFAULT_LITELLM_INTERNAL_PORT = 4000
_COPILOT_BYOK_SETTING_KEY = "github.copilot.chat.customOAIModels"
_COPILOT_BYOK_PROVIDER_LABEL = "Dokploy LiteLLM"
_COPILOT_BYOK_CHAT_ONLY_LIMITATION = (
    "Official Copilot BYOK models apply to VS Code chat and agents only; "
    "inline completions still use Copilot-managed models."
)
_CODER_STATE_REQUIRED_FOR_MODIFY = frozenset({"applied-state.json", "ownership-ledger.json"})
# Coder enforces this on templates, workspaces, and agent names alike, and the CLI only
# says so after the template has already been packaged and the push attempted.
_CODER_MAX_NAME_LENGTH = 32



class DokployCoderBackend:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        stack_name: str,
        hostname: str,
        wildcard_hostname: str,
        admin_email: str,
        admin_password: str,
        postgres_service_name: str,
        postgres: SharedPostgresAllocation,
        ai_default_provider: str = _DEFAULT_AI_DEFAULT_PROVIDER,
        ai_default_model: str = _DEFAULT_AI_DEFAULT_MODEL,
        hermes_inference_provider: str = _DEFAULT_HERMES_INFERENCE_PROVIDER,
        hermes_model: str = _DEFAULT_HERMES_MODEL,
        hermes_desktop_channel: str = _DEFAULT_HERMES_DESKTOP_CHANNEL,
        ai_default_base_url: str = _DEFAULT_AI_DEFAULT_BASE_URL,
        ai_default_api_key: str | None = None,
        workspace_issuer_api_key: str | None = None,
        litellm_allow_new_users: bool = False,
        litellm_allowed_users: tuple[str, ...] = (),
        filex_provisioner: FilexProvisionerState | None = None,
        gitea_provisioner: GiteaProvisionerState | None = None,
        gitea_hostname: str = "",
        filex_hostname: str = "",
        litellm_admin_api: LiteLLMAdminApi | None = None,
        state_dir: Path = Path(".dokploy-wizard-state"),
        client: DokployCoderApi | None = None,
    ) -> None:
        self._stack_name = stack_name
        self._compose_name = _service_name(stack_name)
        self._hostname = hostname
        self._wildcard_hostname = wildcard_hostname
        self._admin_email = admin_email
        self._admin_password = admin_password
        self._postgres_service_name = postgres_service_name
        self._postgres = postgres
        self._ai_default_provider = ai_default_provider.strip() or _DEFAULT_AI_DEFAULT_PROVIDER
        self._ai_default_model = _normalize_ai_default_model(ai_default_model)
        self._hermes_inference_provider = (
            hermes_inference_provider.strip() or _DEFAULT_HERMES_INFERENCE_PROVIDER
        )
        self._hermes_model = _normalize_hermes_model_ref(hermes_model)
        self._hermes_desktop_channel = _normalize_hermes_channel(hermes_desktop_channel)
        self._ai_default_base_url = ai_default_base_url
        self._ai_default_api_key = ai_default_api_key
        # Lets each Coder workspace mint its own LiteLLM child key at startup
        # (see the template MINTPY block); None keeps the shared baked key.
        self._workspace_issuer_api_key = workspace_issuer_api_key
        # Per-user model access policy baked into the template's MINTPY lane:
        # who may mint a key carrying the operator's models. The seeded admin
        # login is always exempt and is derived from `admin_email` below.
        self._litellm_allow_new_users = bool(litellm_allow_new_users)
        self._litellm_allowed_users = tuple(
            user.strip().lower() for user in litellm_allowed_users if user.strip()
        )
        # When the FileX pack reconciled, its provisioner account rides into the
        # template so every workspace boot can materialize the owner's account
        # and per-(user, workspace) credentials; None keeps ~/shared unmounted.
        self._filex_provisioner = filex_provisioner
        # The Gitea pack reconciles before Coder too, so its provisioner token
        # can ride into the template (GITEAPROV boot lane) and the coder phase
        # can run the identity sync (deleted-user disable / return re-enable).
        self._gitea_provisioner = gitea_provisioner
        self._gitea_hostname = gitea_hostname
        # One-ID lanes: the coder phase pre-stands each roster person's FileX
        # account and gateway user, so identities exist before first boot.
        # Empty hostname / absent api (packs off, no key material) makes each
        # lane quietly inert.
        self._filex_hostname = filex_hostname
        self._litellm_admin_api = litellm_admin_api
        self._state_dir = state_dir
        self._client = client or DokployApiClient(api_url=api_url, api_key=api_key)
        self._applied_locator: _ComposeLocator | None = None
        self._created_in_process = False
        # Protocol member (`CoderBackend.apply_time_owned_resources`): set
        # inside `ensure_application_ready`; initialized empty so the engine
        # harvest before any apply reads as "nothing claimed", not an
        # AttributeError.
        self._apply_time_owned_resources: list[OwnedResource] = []

    def _filex_provisioner_state(self) -> FilexProvisionerState | None:
        """The provisioner record to bake, re-reading state when needed.

        Lifecycle builds every backend up front, but the FileX phase runs (and
        creates its provisioner document) before the Coder phase in the same
        process. A backend constructed earlier would otherwise push a template
        with no door and only gain the mount on a second pass, so the document
        is re-read from the state directory at push time.
        """
        if self._filex_provisioner is None:
            from dokploy_wizard.state.store import load_filex_provisioner

            self._filex_provisioner = load_filex_provisioner(self._state_dir)
        return self._filex_provisioner

    def _filex_internal_url(self) -> str:
        """Workspace-reachable FileX base URL, or empty when the pack is off.

        The service rides the shared overlay (see the FileX pack compose), so
        the service DNS name answers from inside any workspace; the S3 door the
        ~/shared mount rides lives at {url}/s3.
        """
        if self._filex_provisioner_state() is None:
            return ""
        # The FileX pack's service name is its own ({stack}-filex); the local
        # _service_name here belongs to the Coder pack.
        return f"http://{self._stack_name}-filex:{_FILEX_INTERNAL_PORT}"

    def _gitea_provisioner_state(self) -> GiteaProvisionerState | None:
        """The Gitea provisioner record, re-reading state when needed.

        Same mid-run convergence as the FileX document: the Gitea phase seeds
        its provisioner before the Coder phase pushes the template in the same
        process, so the document is re-read from the state directory at push
        time instead of only at backend construction.
        """
        if self._gitea_provisioner is None:
            from dokploy_wizard.state.store import load_gitea_provisioner

            self._gitea_provisioner = load_gitea_provisioner(self._state_dir)
        return self._gitea_provisioner

    def _gitea_internal_url(self) -> str:
        """Workspace-reachable Gitea base URL, or empty when the pack is off.

        The compose exposes 3000 on the shared overlay (external network), so
        the service DNS name answers from inside any workspace.
        """
        if self._gitea_provisioner_state() is None:
            return ""
        return f"http://{self._stack_name}-gitea:{_GITEA_INTERNAL_PORT}"

    def _sync_gitea_identities(
        self, session_token: str, coder_users: Mapping[str, str] | None = None
    ) -> list[str]:
        """Align Gitea wizard-sprung accounts with the Coder user roster.

        Ownership lives in the state ledger (`gitea-identities.json`): Gitea's
        admin list cannot tell a wizard account from a manual one once emails
        carry real values, and Coder deleted users are unqueryable (measured),
        so the ledger is the only durable "this account is mine" signal. The
        sync claims every Gitea row whose username and primary email match a
        current Coder user, tracks it, and only ever flips accounts it knows.
        Missing accounts spring here too (pre-flight create), so a new person
        can sign into Gitea before their first workspace boot.
        """
        provisioner = self._gitea_provisioner_state()
        if provisioner is None or not self._gitea_hostname:
            return []
        return _gitea_identity_lane(
            state_dir=self._state_dir,
            coder_hostname=self._hostname,
            session_token=session_token,
            gitea_hostname=self._gitea_hostname,
            provisioner=provisioner,
            coder_users=coder_users,
            password=self._admin_password,
        )

    def _sync_filex_identities(self, coder_users: Mapping[str, str]) -> list[str]:
        """Pre-stand each roster person's FileX account (one-ID plan).

        The boot lane keeps its adopt-or-create rules untouched; this pass
        only removes the surprise of a first boot with no account at all.
        Inert when the pack is off (no provisioner document, no hostname).
        """
        provisioner = self._filex_provisioner_state()
        if provisioner is None or not self._filex_hostname:
            return []
        return _sync_filex_identities(
            hostname=self._filex_hostname,
            provisioner_token=provisioner.token,
            coder_users=coder_users,
            password=self._admin_password,
        )

    def _sync_litellm_identities(self, coder_users: Mapping[str, str]) -> list[str]:
        """Upsert each roster person as a gateway user (one-ID plan).

        Every login gets `user_id=<login>`, the person's real email, the
        stack password, and `internal_user` — the deployer's own login alone
        rides `proxy_admin`. Workspace keys can attribute to the row from
        the first mint because the row predates the boot.
        """
        if self._litellm_admin_api is None or not coder_users:
            return []
        return _litellm_identity_lane(
            coder_users=coder_users,
            admin_email=self._admin_email,
            password=self._admin_password,
            api=self._litellm_admin_api,
        )

    @property
    def apply_time_owned_resources(self) -> tuple[OwnedResource, ...]:
        """Ledger rows for host resources claimed during this apply.

        The identity timer units are installed from inside
        `ensure_application_ready`, but the coder ledger is rebuilt by the
        engine after the apply returns — so the engine harvests this tuple
        and folds it into `build_coder_ledger` (a mid-apply direct ledger
        write would be clobbered by that rebuild). Initialized empty in the
        constructor so reads before any apply return no rows.
        """
        return tuple(self._apply_time_owned_resources)

    def get_service(self, resource_id: str) -> CoderResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "service")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return CoderResourceRecord(
            resource_id=resource_id, resource_name=_service_name(self._stack_name)
        )

    def find_service_by_name(self, resource_name: str) -> CoderResourceRecord | None:
        if resource_name != _service_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return CoderResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"), resource_name=resource_name
        )

    def create_service(self, **kwargs: object) -> CoderResourceRecord:
        resource_name = str(kwargs["resource_name"])
        hostname = str(kwargs["hostname"])
        wildcard_hostname = str(kwargs["wildcard_hostname"])
        postgres_service_name = str(kwargs["postgres_service_name"])
        postgres = kwargs["postgres"]
        data_resource_name = str(kwargs["data_resource_name"])
        if resource_name != _service_name(self._stack_name):
            raise CoderError("Coder service name does not match the active Dokploy plan.")
        if hostname != self._hostname or wildcard_hostname != self._wildcard_hostname:
            raise CoderError("Coder hostnames no longer match the active Dokploy plan.")
        if postgres_service_name != self._postgres_service_name or postgres != self._postgres:
            raise CoderError("Coder postgres inputs no longer match the active Dokploy plan.")
        if data_resource_name != _data_name(self._stack_name):
            raise CoderError("Coder data resource name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return CoderResourceRecord(
            resource_id=_resource_id(locator.compose_id, "service"), resource_name=resource_name
        )

    def update_service(self, **kwargs: object) -> CoderResourceRecord:
        kwargs.pop("resource_id", None)
        return self.create_service(**kwargs)

    def get_persistent_data(self, resource_id: str) -> CoderResourceRecord | None:
        compose_id = _parse_resource_id(resource_id, "data")
        if compose_id is None:
            return None
        locator = self._find_compose_locator()
        if locator is None or locator.compose_id != compose_id:
            return None
        return CoderResourceRecord(
            resource_id=resource_id, resource_name=_data_name(self._stack_name)
        )

    def find_persistent_data_by_name(self, resource_name: str) -> CoderResourceRecord | None:
        if resource_name != _data_name(self._stack_name):
            return None
        if self._created_in_process:
            return None
        locator = self._find_compose_locator()
        if locator is None:
            return None
        return CoderResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"), resource_name=resource_name
        )

    def create_persistent_data(self, resource_name: str) -> CoderResourceRecord:
        if resource_name != _data_name(self._stack_name):
            raise CoderError("Coder data name does not match the active Dokploy plan.")
        locator = self._ensure_compose_applied()
        return CoderResourceRecord(
            resource_id=_resource_id(locator.compose_id, "data"), resource_name=resource_name
        )

    def check_health(self, *, service: CoderResourceRecord, url: str) -> bool:
        del service
        if _local_https_health_check(url):
            return True
        if _public_https_health_check(url):
            return True
        if self._created_in_process:
            return _wait_for_public_https_health(url)
        return False

    def ensure_application_ready(self) -> tuple[str, ...]:
        notes: list[str] = []
        if self._created_in_process:
            _wait_for_coder_bootstrap_api_ready(self._hostname)
        bootstrap_ready = False
        if not self._created_in_process:
            bootstrap_ready = self._verify_current_compose_application().passed
        first_user_provisioned = False
        if not bootstrap_ready and not _coder_first_user_exists(self._hostname):
            _create_coder_first_user(
                hostname=self._hostname,
                email=self._admin_email,
                password=self._admin_password,
            )
            first_user_provisioned = True
            notes.append(f"Provisioned initial Coder admin for '{self._admin_email}'.")
        session_token = _coder_login(
            hostname=self._hostname,
            email=self._admin_email,
            password=self._admin_password,
        )
        container_name = _coder_container_name(_service_name(self._stack_name))
        if container_name is None:
            raise CoderError("Coder container is not running; cannot finish application bootstrap.")
        hermes_litellm_base_url = _litellm_internal_base_url(self._stack_name)
        litellm_fallback_models_json = _shell_double_quote_escape(
            _litellm_workspace_fallback_models_json(
                default_alias=f"{self._ai_default_provider}/{self._ai_default_model}"
            )
        )
        _sync_hermes_workspace_secrets(
            container_name=container_name,
            hostname=self._hostname,
            session_token=session_token,
            hermes_inference_provider=self._hermes_inference_provider,
            hermes_model=self._hermes_model,
            ai_default_base_url=hermes_litellm_base_url,
            ai_default_api_key=self._ai_default_api_key,
        )
        shared_network_name = _shared_network_name(self._stack_name)
        # One template, one world: the collapsed desktop template is the only thing
        # the wizard seeds. Everything else the wizard ever pushed is retired below,
        # and workspaces still riding a retired template are migrated in place (the
        # home volume is named off the workspace id, so an in-place template switch
        # keeps /home while delete-plus-recreate would not).
        seeded = _seed_template(
            container_name=container_name,
            hostname=self._hostname,
            session_token=session_token,
            template_name=_default_template_name(),
            template_dir=_default_template_dir(),
            replacements={
                    "__DOKPLOY_WIZARD_SHARED_NETWORK_NAME__": shared_network_name,
                    "__DOKPLOY_WIZARD_HERMES_INFERENCE_PROVIDER__": _shell_double_quote_escape(
                        self._hermes_inference_provider
                    ),
                    "__DOKPLOY_WIZARD_HERMES_MODEL__": _shell_double_quote_escape(
                        self._hermes_model
                    ),
                    "__DOKPLOY_WIZARD_HERMES_BASE_URL__": _shell_double_quote_escape(
                        hermes_litellm_base_url
                    ),
                    "__DOKPLOY_WIZARD_HERMES_API_KEY__": _shell_double_quote_escape(
                        self._ai_default_api_key or ""
                    ),
                    "__DOKPLOY_WIZARD_CODER_PUBLIC_ORIGIN__": f"https://{self._hostname}",
                    "__DOKPLOY_WIZARD_LITELLM_ISSUER_KEY__": _shell_double_quote_escape(
                        self._workspace_issuer_api_key or ""
                    ),
                    "__DOKPLOY_WIZARD_LITELLM_FALLBACK_MODELS_JSON__": litellm_fallback_models_json,
                    # Per-user LiteLLM workspace-key policy (MINTPY lane): the
                    # flag grants every owner, the allowlist names logins, and
                    # the seeded admin login — derived from the same email the
                    # first-user lane used — is always exempt. A denied owner
                    # mints against the deny model instead of the operator's
                    # models (the boot stays healthy; inference fails honestly
                    # at the gateway instead of leaking the shared baked key).
                    "__DOKPLOY_WIZARD_LITELLM_ALLOW_NEW_USERS__": (
                        "true" if self._litellm_allow_new_users else "false"
                    ),
                    "__DOKPLOY_WIZARD_LITELLM_ALLOWED_USERS__": _shell_double_quote_escape(
                        ",".join(self._litellm_allowed_users)
                    ),
                    "__DOKPLOY_WIZARD_LITELLM_ADMIN_LOGIN__": _shell_double_quote_escape(
                        username_from_email(self._admin_email)
                    ),
                    "__DOKPLOY_WIZARD_LITELLM_DENY_MODEL__": _shell_double_quote_escape(
                        WORKSPACE_KEY_DENY_MODEL
                    ),
                    # The allow-side grant is the named access group (single
                    # source: litellm.admin). The group carries every synced
                    # model, reconciled on the shared-core pass, so a per-person
                    # model story later is a group swap on the person, not a
                    # key re-mint.
                    "__DOKPLOY_WIZARD_LITELLM_GRANTED_MODELS__": _shell_double_quote_escape(
                        ",".join(WORKSPACE_KEY_GRANTED_MODELS)
                    ),
                    # ~/shared: the FileX internal door (empty when the pack is off,
                    # which the startup script treats as "no mount, no warnings") and
                    # the provisioner token. The *_SENTINEL__ markers below are
                    # deliberately NOT mapped — same trick as the issuer guard: a
                    # guard comparing against a second copy of the replaced marker
                    # would compare the value to itself and skip forever.
                    "__DOKPLOY_WIZARD_FILEX_INTERNAL_URL__": self._filex_internal_url(),
                    "__DOKPLOY_WIZARD_FILEX_PROVISIONER_TOKEN__": _shell_double_quote_escape(
                        provisioner.token if (provisioner := self._filex_provisioner_state()) else ""
                    ),
                    # The lane's per-person FileX accounts sign in with the stack
                    # admin password (the gitea lane's choice, one credential),
                    # so the derived-password seed is gone.
                    "__DOKPLOY_WIZARD_FILEX_ACCOUNT_PASSWORD__": _shell_double_quote_escape(
                        self._admin_password or "ChangeMeSoon"
                    ),
                    # Gitea identity lane: the overlay door + provisioner token
                    # for the GITEAPROV boot lane (account spring with the stack
                    # admin password, SSH key). The *_SENTINEL__ markers stay
                    # unmapped (same guard trick).
                    "__DOKPLOY_WIZARD_GITEA_INTERNAL_URL__": self._gitea_internal_url(),
                    "__DOKPLOY_WIZARD_GITEA_PROVISIONER_TOKEN__": _shell_double_quote_escape(
                        gitea_provisioner.token
                        if (gitea_provisioner := self._gitea_provisioner_state())
                        else ""
                    ),
                    "__DOKPLOY_WIZARD_GITEA_ACCOUNT_PASSWORD__": _shell_double_quote_escape(
                        self._admin_password or "ChangeMeSoon"
                    ),
                    "__DOKPLOY_WIZARD_OPENCODE_SERVER_PASSWORD__": _shell_double_quote_escape(
                        self._admin_password or "ChangeMeSoon"
                    ),
                    # The official webapp signs in with the same admin password: the
                    # tile is opened in a browser, where a second distinct secret is
                    # one more thing to lose.
                    "__DOKPLOY_WIZARD_HERMES_WEBAPP_PASSWORD__": _shell_double_quote_escape(
                        self._admin_password or "ChangeMeSoon"
                    ),
                    # Only this template needs the newer channel: the Webapp artifact it
                    # serves does not exist on the release channel, and the other Hermes
                    # templates stay on their proven build.
                    "__DOKPLOY_WIZARD_HERMES_INSTALL_CHANNEL__": _shell_double_quote_escape(
                        self._hermes_desktop_channel
                    ),
                },
        )
        if seeded:
            notes.append(f"Seeded default Coder template '{_default_template_name()}'.")
        migrated = _migrate_retired_template_workspaces(
            container_name=container_name,
            hostname=self._hostname,
            session_token=session_token,
            template_name=_default_template_name(),
        )
        notes.extend(
            f"Migrated Coder workspace '{name}' onto template '{_default_template_name()}'."
            for name in migrated
        )
        try:
            retired_templates = _retire_removed_templates(
                container_name=container_name,
                hostname=self._hostname,
                session_token=session_token,
            )
        except CoderError as e:
            note = f"Skipped retiring removed Coder templates: {_safe_progress_reason(str(e))}"
            _emit_coder_progress(note)
            notes.append(note)
            retired_templates = ()
        notes.extend(f"Retired removed Coder template '{name}'." for name in retired_templates)
        # One-ID identity sync: one roster read feeds all three surfaces, and
        # each surface fails alone (the doctrine the Gitea lane established —
        # identity convenience must never take a coder pass down). An empty
        # roster (unreadable users API) skips every lane with one honest note.
        roster: dict[str, str] = {}
        try:
            roster = _coder_roster(coder_hostname=self._hostname, session_token=session_token)
        except Exception as exc:  # noqa: BLE001 - see above
            note = f"Skipped identity sync: Coder roster unreadable ({_safe_progress_reason(str(exc))})"
            _emit_coder_progress(note)
            notes.append(note)
        if roster:
            for label, lane in (
                ("Gitea", lambda: self._sync_gitea_identities(session_token, coder_users=roster)),
                ("FileX", lambda: self._sync_filex_identities(roster)),
                ("LiteLLM", lambda: self._sync_litellm_identities(roster)),
            ):
                try:
                    notes.extend(lane())
                except Exception as exc:  # noqa: BLE001 - see above
                    note = f"Skipped {label} identity sync: {_safe_progress_reason(str(exc))}"
                    _emit_coder_progress(note)
                    notes.append(note)
        # The identity timer is what turns "we ran a coder pass" into "new Coder
        # users get FileX/Gitea/gateway accounts automatically": a host systemd
        # pair that re-runs exactly the lanes above every few minutes (see the
        # `sync-identities` command). Its ledger row rides `apply_time_owned_resources`
        # because the engine rebuilds this phase's ledger after the apply.
        timer_notes, timer_installed = _install_identity_timer(state_dir=self._state_dir)
        for timer_note in timer_notes:
            _emit_coder_progress(timer_note)
        notes.extend(timer_notes)
        self._apply_time_owned_resources = (
            [coder_identity_timer_resource(self._stack_name)] if timer_installed else []
        )
        workspace_name = _default_workspace_name(self._hostname)
        # The adopt-or-create lane rides every coder pass, healthy included: on
        # a box upgraded from a pre-fix wizard the compose application verifies
        # clean while legacy date-stamped workspaces still stand, and skipping
        # the lane there pinned the legacy names (and the retired templates
        # holding them) in place forever — measured on a box running Coder
        # v2.38.3. On a converged stack the lane is a read-only exists noop;
        # the template-version convergence inside it is the same check this
        # path used directly, so the ~/shared bootstrap still lands without
        # waiting for the one pass where bootstrap verification fails.
        try:
            outcome = _ensure_default_workspace(
                container_name=container_name,
                hostname=self._hostname,
                session_token=session_token,
                workspace_name=workspace_name,
                template_name=_default_template_name(),
            )
            if outcome.action == "created":
                if first_user_provisioned:
                    notes.append(
                        f"Created default Coder workspace '{workspace_name}' for '{self._admin_email}'."
                    )
                else:
                    notes.append(f"Created missing default Coder workspace '{workspace_name}'.")
            elif outcome.action == "adopted":
                notes.append(
                    f"Renamed legacy default Coder workspace '{outcome.adopted_from}' "
                    f"to '{workspace_name}'."
                )
            notes.extend(
                f"Pruned duplicate wizard-created Coder workspace '{name}'."
                for name in outcome.pruned
            )
            if outcome.updated:
                notes.append(
                    f"Updated Coder workspace '{workspace_name}' onto the active "
                    f"template version."
                )
        except CoderError as e:
            notes.append(f"Coder default workspace step did not complete: {e}")
        return tuple(notes)

    def _find_compose_locator(self) -> _ComposeLocator | None:
        if self._applied_locator is not None:
            return self._applied_locator
        try:
            projects = self._client.list_projects()
        except DokployApiError as error:
            raise CoderError(str(error)) from error
        for project in projects:
            if project.name != self._stack_name:
                continue
            environment = _pick_environment(project)
            if environment is None:
                continue
            for compose in environment.composes:
                if compose.name == self._compose_name:
                    locator = _ComposeLocator(
                        project_id=project.project_id,
                        environment_id=environment.environment_id,
                        compose_id=compose.compose_id,
                    )
                    self._applied_locator = locator
                    return locator
        return None

    def _ensure_compose_applied(self) -> _ComposeLocator:
        if self._applied_locator is not None and self._created_in_process:
            return self._applied_locator
        compose_file = _render_compose_file(
            stack_name=self._stack_name,
            hostname=self._hostname,
            wildcard_hostname=self._wildcard_hostname,
            postgres_service_name=self._postgres_service_name,
            postgres=self._postgres,
        )
        try:
            if self._applied_locator is not None:
                self._applied_locator = self._apply_existing_compose(
                    locator=self._applied_locator,
                    compose_file=compose_file,
                )
                return self._applied_locator
            projects = self._client.list_projects()
            for project in projects:
                if project.name != self._stack_name:
                    continue
                environment = _pick_environment(project)
                if environment is None:
                    break
                for compose in environment.composes:
                    if compose.name == self._compose_name:
                        locator = _ComposeLocator(
                            project_id=project.project_id,
                            environment_id=environment.environment_id,
                            compose_id=compose.compose_id,
                        )
                        self._applied_locator = self._apply_existing_compose(
                            locator=locator,
                            compose_file=compose_file,
                        )
                        return self._applied_locator
                created = self._client.create_compose(
                    name=self._compose_name,
                    environment_id=environment.environment_id,
                    compose_file="services: {}\n",
                    app_name=self._compose_name,
                )
                apply_rendered_compose_to_existing(
                    client=self._client,
                    compose_id=created.compose_id,
                    rendered_compose=compose_file,
                )
                deployment = self._client.deploy_compose(
                    compose_id=created.compose_id,
                    title="dokploy-wizard coder reconcile",
                    description="Create Coder compose app",
                )
                if not deployment.success:
                    msg = "Dokploy deploy for compose service 'coder' did not report success."
                    raise RuntimeError(msg)
                locator = _ComposeLocator(
                    project_id=project.project_id,
                    environment_id=environment.environment_id,
                    compose_id=created.compose_id,
                )
                self._applied_locator = locator
                self._created_in_process = True
                self._persist_compose_hash_if_checkpoint_present(compose_file)
                return locator
            created_project = self._client.create_project(
                name=self._stack_name, description="Managed by dokploy-wizard", env=None
            )
            created_compose = self._client.create_compose(
                name=self._compose_name,
                environment_id=created_project.environment_id,
                compose_file="services: {}\n",
                app_name=self._compose_name,
            )
            apply_rendered_compose_to_existing(
                client=self._client,
                compose_id=created_compose.compose_id,
                rendered_compose=compose_file,
            )
            deployment = self._client.deploy_compose(
                compose_id=created_compose.compose_id,
                title="dokploy-wizard coder reconcile",
                description="Create Coder compose app",
            )
            if not deployment.success:
                msg = "Dokploy deploy for compose service 'coder' did not report success."
                raise RuntimeError(msg)
        except (DokployApiError, RuntimeError) as error:
            raise CoderError(str(error)) from error
        locator = _ComposeLocator(
            project_id=created_project.project_id,
            environment_id=created_project.environment_id,
            compose_id=created_compose.compose_id,
        )
        self._applied_locator = locator
        self._created_in_process = True
        self._persist_compose_hash_if_checkpoint_present(compose_file)
        return locator

    def _apply_existing_compose(
        self, *, locator: _ComposeLocator, compose_file: RenderedCompose
    ) -> _ComposeLocator:
        if not self._has_applied_state_checkpoint():
            updated = apply_rendered_compose_to_existing(
                client=self._client,
                compose_id=locator.compose_id,
                rendered_compose=compose_file,
            )
            deployment = self._client.deploy_compose(
                compose_id=updated.compose_id,
                title="dokploy-wizard coder reconcile",
                description="Update Coder compose app",
            )
            if not deployment.success:
                msg = "Dokploy deploy for compose service 'coder' did not report success."
                raise RuntimeError(msg)
            self._created_in_process = True
            return _ComposeLocator(
                project_id=locator.project_id,
                environment_id=locator.environment_id,
                compose_id=updated.compose_id,
            )
        apply_result = apply_compose_noop_guard(
            rendered_compose=compose_file,
            service_key=self._compose_name,
            state_dir=self._state_dir,
            client=self._client,
            locator=locator,
            compose_id=locator.compose_id,
            title="dokploy-wizard coder reconcile",
            description="Update Coder compose app",
            verify_current=self._verify_current_compose_application,
            locator_factory=lambda compose_id: _ComposeLocator(
                project_id=locator.project_id,
                environment_id=locator.environment_id,
                compose_id=compose_id,
            ),
        )
        self._created_in_process = apply_result.status == "applied"
        return apply_result.locator

    def _has_applied_state_checkpoint(self) -> bool:
        return load_state_dir(self._state_dir).applied_state is not None

    def _persist_compose_hash_if_checkpoint_present(self, compose_file: RenderedCompose) -> None:
        if not self._has_applied_state_checkpoint():
            return
        persist_compose_artifact_hash(
            state_dir=self._state_dir,
            service_key=self._compose_name,
            rendered_compose=compose_file,
        )

    def _verify_current_compose_application(self):
        service_name = _service_name(self._stack_name)
        health_url = f"https://{self._hostname}/healthz"
        if not self.check_health(
            service=CoderResourceRecord(
                resource_id=_resource_id(self._compose_name, "service"),
                resource_name=service_name,
            ),
            url=health_url,
        ):
            return make_verification_result(
                service_name=self._compose_name,
                tier="app",
                passed=False,
                detail=f"Coder health checks failed for '{health_url}'.",
            )
        container_name = _coder_container_name(service_name)
        if container_name is None:
            return make_verification_result(
                service_name=self._compose_name,
                tier="bootstrap",
                passed=False,
                detail="Coder container is not running.",
            )
        try:
            _wait_for_coder_bootstrap_api_ready(self._hostname)
            if not _coder_first_user_exists(self._hostname):
                return make_verification_result(
                    service_name=self._compose_name,
                    tier="bootstrap",
                    passed=False,
                    detail=f"Coder first user '{self._admin_email}' is not provisioned.",
                )
            session_token = _coder_login(
                hostname=self._hostname,
                email=self._admin_email,
                password=self._admin_password,
            )
            workspace_name = _default_workspace_name(self._hostname)
            missing_templates: list[str] = []
            workspace_missing = False
            for attempt in range(3):
                template_names = {
                    item.get("name")
                    for item in _list_templates(
                        container_name=container_name,
                        hostname=self._hostname,
                        session_token=session_token,
                    )
                    if isinstance(item.get("name"), str)
                }
                missing_templates = [
                    template_name
                    for template_name in _required_template_names()
                    if template_name not in template_names
                ]
                workspace_names = _list_workspaces(
                    container_name=container_name,
                    hostname=self._hostname,
                    session_token=session_token,
                )
                # A legacy date-stamped default counts as present: reconcile adopts
                # (renames) it into the stable name on the same pass, and a hard
                # failure here would fight that in-flight adoption.
                workspace_missing = workspace_name not in workspace_names and not (
                    _legacy_default_workspace_names(self._hostname, workspace_names)
                )
                if not missing_templates and not workspace_missing:
                    break
                if attempt < 2:
                    time.sleep(2.0)

            if missing_templates:
                return make_verification_result(
                    service_name=self._compose_name,
                    tier="bootstrap",
                    passed=False,
                    detail=(
                        "Coder templates are missing or not visible: "
                        + ", ".join(missing_templates)
                        + "."
                    ),
                )
            if workspace_missing:
                return make_verification_result(
                    service_name=self._compose_name,
                    tier="bootstrap",
                    passed=False,
                    detail=f"Coder default workspace '{workspace_name}' is missing.",
                )
        except CoderError as error:
            return make_verification_result(
                service_name=self._compose_name,
                tier="bootstrap",
                passed=False,
                detail=str(error),
            )
        return make_verification_result(
            service_name=self._compose_name,
            tier="bootstrap",
            passed=True,
            detail=(
                "Coder container/API, first user bootstrap, seeded templates, "
                "and default workspace are ready."
            ),
        )


def _resource_id(compose_id: str, kind: str) -> str:
    return f"dokploy-compose:{compose_id}:{kind}"


def _parse_resource_id(resource_id: str, kind: str) -> str | None:
    prefix = "dokploy-compose:"
    suffix = f":{kind}"
    if not resource_id.startswith(prefix) or not resource_id.endswith(suffix):
        return None
    compose_id = resource_id.removeprefix(prefix).removesuffix(suffix)
    return compose_id or None


def _pick_environment(project: DokployProjectSummary) -> DokployEnvironmentSummary | None:
    if not project.environments:
        return None
    for environment in project.environments:
        if environment.is_default:
            return environment
    return project.environments[0]


# The FileX pack's listening port; the ~/shared mount talks to it over the
# shared overlay (http://{stack}-filex:5212/s3 for the S3 door).
_FILEX_INTERNAL_PORT = 5212


def _service_name(stack_name: str) -> str:
    return f"{stack_name}-coder"


def _data_name(stack_name: str) -> str:
    return f"{stack_name}-coder-data"


def _shared_network_name(stack_name: str) -> str:
    return f"{stack_name}-shared"


def _litellm_internal_base_url(stack_name: str) -> str:
    return f"http://{stack_name}-shared-litellm:{_DEFAULT_LITELLM_INTERNAL_PORT}"


def _normalize_hermes_channel(value: str) -> str:
    """The update channel name, or the default.

    The value is interpolated into an installer argument and a startup script, so it is
    a name from the release archive (`stable`, `main`, ...) and nothing that needs shell
    quoting. A nonsense name would be refused by the installer at workspace boot, minutes
    later and far from the env file, so it is corrected here instead.
    """

    candidate = value.strip()
    if not _HERMES_CHANNEL_PATTERN.fullmatch(candidate):
        return _DEFAULT_HERMES_DESKTOP_CHANNEL
    return candidate


def _normalize_hermes_model_ref(model_ref: str) -> str:
    normalized = model_ref.strip()
    if normalized in {"", "unsloth-active", "local/unsloth-active"}:
        return DEFAULT_LOCAL_CANONICAL_ALIAS
    return normalized


def _normalize_ai_default_model(model_ref: str) -> str:
    normalized = model_ref.strip()
    if normalized == "":
        return _DEFAULT_AI_DEFAULT_MODEL
    provider_prefix = f"{_DEFAULT_AI_DEFAULT_PROVIDER}/"
    if normalized.startswith(provider_prefix):
        return normalized.removeprefix(provider_prefix)
    return normalized


def _wildcard_suffix(wildcard_hostname: str) -> str:
    if not wildcard_hostname.startswith("*."):
        raise CoderError("Coder wildcard hostname must start with '*.'")
    return wildcard_hostname.removeprefix("*.")


def _render_compose_file(
    *,
    stack_name: str,
    hostname: str,
    wildcard_hostname: str,
    postgres_service_name: str,
    postgres: SharedPostgresAllocation,
) -> RenderedCompose:
    service_name = _service_name(stack_name)
    data_name = _data_name(stack_name)
    shared_network = _shared_network_name(stack_name)
    wildcard_suffix = _wildcard_suffix(wildcard_hostname)
    wildcard_host_pattern = re.escape(wildcard_suffix).replace("\\", "\\\\")
    pg_url_env = "CODER_PG_CONNECTION_URL"
    pg_url = (
        f"postgres://{postgres.user_name}:change-me@{postgres_service_name}:5432/"
        f"{postgres.database_name}?sslmode=disable"
    )
    compose_file = (
        "services:\n"
        f"  {service_name}:\n"
        "    image: ghcr.io/coder/coder:latest\n"
        "    restart: unless-stopped\n"
        '    user: "0:0"\n'
        "    environment:\n"
        "      CODER_HTTP_ADDRESS: 0.0.0.0:3000\n"
        f"      CODER_ACCESS_URL: {_yaml_quote(f'https://{hostname}/')}\n"
        f"      CODER_WILDCARD_ACCESS_URL: {_yaml_quote(wildcard_hostname)}\n"
        f"      CODER_PG_CONNECTION_URL: \"{_required_placeholder(pg_url_env)}\"\n"
        '      CODER_DERP_FORCE_WEBSOCKETS: "true"\n'
        f"      CODER_PROXY_TRUSTED_HEADERS: {_yaml_quote('X-Forwarded-For')}\n"
        f"      CODER_PROXY_TRUSTED_ORIGINS: {_yaml_quote('10.0.0.0/8,172.16.0.0/12,192.168.0.0/16')}\n"
        f"      CODER_CACHE_DIRECTORY: {_yaml_quote('/home/coder/.cache')}\n"
        "    labels:\n"
        '      traefik.enable: "true"\n'
        f'      traefik.http.routers.{service_name}.entrypoints: "websecure"\n'
        f'      traefik.http.routers.{service_name}.rule: "Host(`{hostname}`)"\n'
        f'      traefik.http.routers.{service_name}.middlewares: "{service_name}-forwarded-https,{service_name}-forwarded-host"\n'
        f'      traefik.http.routers.{service_name}.tls: "true"\n'
        f'      traefik.http.routers.{service_name}-wildcard.entrypoints: "websecure"\n'
        f'      traefik.http.routers.{service_name}-wildcard.rule: "HostRegexp(`(?i)^[a-z0-9-]+(?:--[a-z0-9-]+){{2,}}\\\\.{wildcard_host_pattern}$`)"\n'
        f'      traefik.http.routers.{service_name}-wildcard.middlewares: "{service_name}-forwarded-https"\n'
        f'      traefik.http.routers.{service_name}-wildcard.tls: "true"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Proto: "https"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-https.headers.customrequestheaders.X-Forwarded-Port: "443"\n'
        f'      traefik.http.middlewares.{service_name}-forwarded-host.headers.customrequestheaders.X-Forwarded-Host: "{hostname}"\n'
        f'      traefik.http.services.{service_name}.loadbalancer.server.port: "3000"\n'
        "    healthcheck:\n"
        "      test: ['CMD-SHELL', 'wget -qO- http://127.0.0.1:3000/healthz >/dev/null']\n"
        "      interval: 30s\n"
        "      timeout: 5s\n"
        "      retries: 10\n"
        f"    volumes:\n      - {data_name}:/home/coder\n"
        "      - /var/run/docker.sock:/var/run/docker.sock\n"
        "    expose:\n"
        "      - '3000'\n"
        "    networks:\n"
        "      - default\n"
        "      - dokploy-network\n"
        f"      - {shared_network}\n"
        "volumes:\n"
        f"  {data_name}:\n"
        "networks:\n"
        "  dokploy-network:\n"
        "    external: true\n"
        f"  {shared_network}:\n"
        f"    name: {shared_network}\n"
        "    external: true\n"
    )
    return RenderedCompose(
        compose_file=compose_file,
        env_specs=(
            _coder_env_spec(
                name=pg_url_env,
                value=pg_url,
                target_services=(service_name,),
                source="coder-postgres-url",
            ),
        ),
    )


def _coder_env_spec(
    *, name: str, value: str, target_services: tuple[str, ...], source: str
) -> DokployEnvSpec:
    return DokployEnvSpec(
        variable=DokployEnvVar(name=name, value=value, sensitive=True, source=source),
        owner="coder",
        target_services=target_services,
        placeholder=_required_placeholder(name),
        required=True,
    )


def _required_placeholder(name: str) -> str:
    return f"${{{name}:?{name} is required}}"


def _local_https_health_check(url: str) -> bool:
    parsed = parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        return False
    connection = http.client.HTTPSConnection(
        "127.0.0.1",
        443,
        timeout=10,
        context=ssl._create_unverified_context(),
    )
    try:
        connection.request(
            "GET",
            parsed.path or "/",
            headers={"Host": parsed.netloc},
        )
        response = connection.getresponse()
        response.read()
        return response.status == 200
    except OSError:
        return False
    finally:
        connection.close()


def _public_https_health_check(url: str) -> bool:
    try:
        context = ssl._create_unverified_context()
        with urlrequest.urlopen(url, timeout=10, context=context) as response:  # noqa: S310
            response.read()
            return response.status == 200
    except (urlerror.HTTPError, urlerror.URLError, OSError, TimeoutError):
        return False


def _wait_for_public_https_health(
    url: str, *, attempts: int = 19, delay_seconds: float = 5.0
) -> bool:
    for attempt in range(attempts):
        if _public_https_health_check(url):
            return True
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return False


def _wait_for_coder_bootstrap_api_ready(
    hostname: str, *, attempts: int = 12, delay_seconds: float = 5.0
) -> None:
    for attempt in range(attempts):
        try:
            _coder_request(hostname=hostname, method="GET", path="/api/v2/users/first")
            return
        except _CoderHTTPError as exc:
            if exc.status == 404:
                return
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
                continue
            raise CoderError(
                f"Coder bootstrap API did not become ready before first-user setup (HTTP {exc.status})."
            ) from exc
    raise CoderError("Coder bootstrap API did not become ready before first-user setup.")


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _coder_first_user_exists(hostname: str) -> bool:
    try:
        _coder_request(hostname=hostname, method="GET", path="/api/v2/users/first")
    except _CoderHTTPError as exc:
        if exc.status == 404:
            return False
        raise CoderError(f"Unable to determine Coder bootstrap state: HTTP {exc.status}") from exc
    return True


def _create_coder_first_user(
    *, hostname: str, email: str, password: str, attempts: int = 12, delay_seconds: float = 5.0
) -> None:
    path = "/api/v2/users/first"
    payload: dict[str, object] = {
        "email": email,
        "username": _username_from_email(email),
        "name": _display_name_from_email(email),
        "password": password,
    }
    for attempt in range(attempts):
        try:
            _coder_request(
                hostname=hostname,
                method="POST",
                path=path,
                payload=payload,
                expected_statuses={201},
            )
            return
        except _CoderHTTPError as exc:
            if exc.status == 404 and attempt < attempts - 1:
                time.sleep(delay_seconds)
                continue
            raise CoderError(
                f"Coder bootstrap first-user creation failed at POST {path}: HTTP {exc.status}."
            ) from exc
    raise CoderError(f"Coder bootstrap first-user creation failed at POST {path}.")


def _coder_login(*, hostname: str, email: str, password: str) -> str:
    response = _coder_request(
        hostname=hostname,
        method="POST",
        path="/api/v2/users/login",
        payload={"email": email, "password": password},
        expected_statuses={200, 201},
    )
    token = response.get("session_token")
    if not isinstance(token, str) or token == "":
        raise CoderError("Coder login response did not include a session token.")
    return token


def _coder_logout(*, hostname: str, session_token: str) -> None:
    """Delete a login-session token created by `_coder_login` (best effort).

    `/api/v2/users/login` mints a PERSISTENT session (unlike the one Coder's
    own UI cookie flow issues at sign-in): leaving every sync pass logged in
    piles a visible row onto the user's tokens page each run. Measured on the
    box at the v2.39.4 pin: POST /api/v2/users/logout answers 200 "Logged
    out!" and the token is dead on reuse (401); DELETE answers 404. The DELETE
    fallback keeps the lane honest on newer builds that renamed the verb, and
    a miss either way is a leak to tolerate, not a reason to fail a pass.
    """
    for method in ("POST", "DELETE"):
        try:
            _coder_request(
                hostname=hostname,
                method=method,
                path="/api/v2/users/logout",
                expected_statuses={200, 204},
                session_token=session_token,
            )
            return
        except (CoderError, _CoderHTTPError):
            continue


def _coder_request(
    *,
    hostname: str,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    expected_statuses: set[int] | None = None,
    session_token: str | None = None,
) -> dict[str, object]:
    if expected_statuses is None:
        expected_statuses = {200}
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Host": hostname,
    }
    if session_token is not None:
        # Measured on the box at the v2.39.4 pin: `Coder-Session-Token` is the
        # accepted spelling (`CoderSessionToken` and ?session_token= → 401).
        headers["Coder-Session-Token"] = session_token
    req = urlrequest.Request(
        f"https://127.0.0.1{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        context = ssl._create_unverified_context()
        opener = urlrequest.build_opener(urlrequest.HTTPSHandler(context=context))
        with opener.open(req, timeout=20) as response:
            raw = response.read().decode("utf-8", "ignore")
            if response.status not in expected_statuses:
                raise CoderError(f"Coder request {method} {path} returned HTTP {response.status}.")
            return {} if raw == "" else json.loads(raw)
    except urlerror.HTTPError as exc:
        raise _CoderHTTPError(status=exc.code) from exc


# Gitea's compose exposes 3000 on the shared overlay (no publish); workspaces
# reach the identity lane door at that service DNS name.
_GITEA_INTERNAL_PORT = 3000
# Gitea's admin user list defaults to 30 rows per page; paginate to stay
# correct at stack scale (the box scale is tiny, the cap keeps bugs bounded).
_GITEA_PAGE_SIZE = 30
_GITEA_MAX_PAGES = 20


def _gitea_api_request(
    *,
    hostname: str,
    method: str,
    path: str,
    token: str,
    payload: dict[str, object] | None = None,
) -> tuple[int | None, object]:
    """One Gitea API call with the provisioner token through the loopback edge.

    Same door the Gitea pack itself uses: UFW blocks host->overlay and the
    HTTPS-only edge answers on loopback with the pack hostname as Host.
    """
    headers = {
        "Host": hostname,
        "Accept": "application/json",
        "Authorization": f"Token {token}",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(
        f"https://127.0.0.1{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        context = ssl._create_unverified_context()
        opener = urlrequest.build_opener(urlrequest.HTTPSHandler(context=context))
        with opener.open(req, timeout=20) as response:
            raw = response.read().decode("utf-8", "ignore")
            return response.status, (json.loads(raw) if raw else None)
    except urlerror.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "ignore") or "null")
        except Exception:
            return exc.code, None
    except (urlerror.URLError, TimeoutError, OSError):
        return None, None


def _gitea_admin_user_rows(*, hostname: str, token: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    page = 1
    while page <= _GITEA_MAX_PAGES:
        status, payload = _gitea_api_request(
            hostname=hostname,
            method="GET",
            path=f"/api/v1/admin/users?page={page}",
            token=token,
        )
        if status != 200 or not isinstance(payload, list):
            raise CoderError(f"Gitea admin user list failed (HTTP {status}).")
        rows.extend(row for row in payload if isinstance(row, dict))
        if len(payload) < _GITEA_PAGE_SIZE:
            break
        page += 1
    return rows


def _coder_roster(*, coder_hostname: str, session_token: str) -> dict[str, str]:
    """Current (non-deleted) Coder users as ``login -> real email``.

    Deleted rows carry status ``deleted`` on the list (measured, v2.39.4) and
    every identity surface treats them as absent; disable-not-delete then
    lives on each surface's own claim mechanism.
    """
    users = _coder_request(
        hostname=coder_hostname,
        method="GET",
        path="/api/v2/users",
        session_token=session_token,
    )
    rows = users.get("users")
    roster: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or str(row.get("status") or "") == "deleted":
            continue
        login = str(row.get("username") or "").lower()
        email = str(row.get("email") or "").lower()
        if login and email:
            roster[login] = email
    return roster


# The pre-email-fix lane value (see FILEXPROV): an account row riding this
# email is the lane's own legacy twin, not a manual account — the lane's
# rules own it, the identity pre-flight never touches it.
_FILEX_LEGACY_EMAIL_SUFFIX = "@coder.wizard.local"


def _filex_api_request(
    *,
    hostname: str,
    method: str,
    path: str,
    token: str,
    body: dict[str, object] | None = None,
) -> tuple[int | None, object]:
    """One FileX API call with the provisioner token through the loopback edge.

    Same door the FileX pack itself uses (UFW blocks host->overlay; the
    HTTPS-only edge answers on loopback with the pack hostname as Host).
    """
    headers = {"Host": hostname, "Accept": "application/json", "Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(f"https://127.0.0.1{path}", data=data, method=method, headers=headers)
    try:
        context = ssl._create_unverified_context()
        opener = urlrequest.build_opener(urlrequest.HTTPSHandler(context=context))
        with opener.open(req, timeout=20) as response:
            raw = response.read().decode("utf-8", "ignore")
            return response.status, (json.loads(raw) if raw else None)
    except urlerror.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "ignore") or "null")
        except Exception:
            return exc.code, None
    except (urlerror.URLError, TimeoutError, OSError):
        return None, None


def _sync_filex_identities(
    *,
    hostname: str,
    provisioner_token: str,
    coder_users: Mapping[str, str],
    password: str,
) -> list[str]:
    """Pre-stand each roster person's FileX account (the one-ID plan).

    The lookup mirrors the boot lane exactly — find the person's row by their
    real email; a username owned by any other email is a manual account (or
    a legacy twin) and keeps its own secret and meaning; only a fully free
    login gets created, with the stack password the lane would have used. A
    person whose account already stands changes nothing here, so the pre-flight
    and the boot lane converge on the same row and never twin.
    """
    status, users = _filex_api_request(
        hostname=hostname, method="GET", path="/api/admin/users", token=provisioner_token
    )
    if status != 200:
        raise CoderError(f"FileX identity pre-flight list failed (HTTP {status}).")
    rows = (
        users
        if isinstance(users, list)
        else (users or {}).get("users", [])
        if isinstance(users, dict)
        else []
    )
    emails = {
        str(row.get("email") or "").lower() for row in rows if isinstance(row, dict)
    }
    names = {
        str(row.get("username") or "").lower() for row in rows if isinstance(row, dict)
    }
    notes: list[str] = []
    for login in sorted(coder_users):
        email = coder_users[login]
        if email in emails:
            continue
        if login in names:
            # Manual owner or legacy twin: the lane's rules already cover it.
            continue
        status, _body = _filex_api_request(
            hostname=hostname,
            method="POST",
            path="/api/admin/users",
            token=provisioner_token,
            body={"email": email, "username": login, "role": "user", "password": password},
        )
        if status not in (200, 201):
            raise CoderError(f"FileX identity pre-flight create for '{login}' failed (HTTP {status}).")
        notes.append(f"Created FileX account '{login}' (identity pre-flight).")
    return notes


def _identity_sync_actions(
    *,
    gitea_rows: Sequence[object],
    coder_users: Mapping[str, str],
    provisioner_username: str,
    tracked: set[str],
) -> tuple[list[tuple[str, bool]], set[str]]:
    """Accounts to flip, as (username, desired-active) pairs, plus the claim set.

    Scope: only wizard-sprung accounts. A Gitea row is claimed as wizard-owned
    when its username and primary email exactly match a current (non-deleted)
    Coder user; the ledger's tracked set carries that claim forward, so an
    account stays managed after its Coder user is deleted (measured: v2.39.4
    drops deleted rows from the default list, and they are unqueryable by any
    status/type/name filter — the roster has no durable deleted-user signal,
    which is exactly why the ledger is the ownership authority). Manual
    accounts, the provisioner itself, and accounts never claimed are never
    touched. A tracked login present in Coder's users list must be active in
    Gitea; absent (deleted) must be inactive. A tracked login that has gone
    from BOTH rosters — no Gitea row and no Coder user — is untracked: an
    operator teardown (e.g. the demo prop removed after a screenshot run)
    deletes the account outright, and a claim over a nonexistent account is
    ledger noise, not ownership. The history rule that motivated carrying
    claims through deletion still holds while the Gitea row exists to be
    disabled, re-enabled, or audited.
    """
    actions: list[tuple[str, bool]] = []
    claimed = set(tracked)
    seen: set[str] = set()
    for row in gitea_rows:
        if not isinstance(row, dict):
            continue
        username = str(row.get("username") or "")
        login = username.lower()
        email = str(row.get("email") or "").lower()
        if not login or login == provisioner_username.lower():
            continue
        seen.add(login)
        if email and coder_users.get(login) == email:
            claimed.add(login)
        if login not in claimed:
            # Not ours this pass and never claimed before: a manual account
            # (or one we have not adopted). Leave its secret and meaning alone.
            continue
        desired_active = login in coder_users
        if bool(row.get("active")) is not desired_active:
            actions.append((username, desired_active))
    claimed &= seen | {
        # A claimed login with no Gitea row at all (deleted outright) stays
        # claimed only while its Coder user lives — the workspace boot lane
        # will spring the account again, and the claim must survive that gap.
        login
        for login in claimed & tracked
        if login in coder_users
    }
    return actions, claimed


def _prestand_gitea_identities(
    *,
    gitea_hostname: str,
    provisioner_token: str,
    coder_users: Mapping[str, str],
    password: str,
) -> tuple[list[str], set[str]]:
    """Spring missing roster accounts into Gitea (the one-ID pre-flight lane).

    Mirrors `_sync_filex_identities` across the Gitea surface: email-first
    lookup, the same manual-owner/legacy-twin skip, and the same stack-password
    seed — with one addition the FileX lane does not need: the create carries
    `must_change_password: False` because a pending must-change turns every
    basic-auth call into a 403 (measured). An admin-API create verifies and
    primaries the given email instantly (measured), so the person signs in
    with username or email from the first second and the sync lane's own
    email match claims the row on this very pass. The workspace boot lane
    keeps its spring untouched: this only removes the surprise of an account
    that does not exist yet. Returns notes plus the logins created this pass
    (the caller unions them into the ledger claims — the sync above pruned
    against the pre-create snapshot, so a fresh row would otherwise be lost).
    """
    rows = _gitea_admin_user_rows(hostname=gitea_hostname, token=provisioner_token)
    emails = {
        str(row.get("email") or "").lower() for row in rows if isinstance(row, dict)
    }
    names = {
        str(row.get("username") or "").lower() for row in rows if isinstance(row, dict)
    }
    notes: list[str] = []
    created: set[str] = set()
    for login in sorted(coder_users):
        email = coder_users[login]
        if email in emails:
            continue
        if login in names:
            # Manual owner or legacy twin: the sync and boot lanes' rules cover it.
            continue
        status, _body = _gitea_api_request(
            hostname=gitea_hostname,
            method="POST",
            path="/api/v1/admin/users",
            token=provisioner_token,
            payload={
                "username": login,
                "email": email,
                "password": password,
                "must_change_password": False,
                "send_notify_email": False,
            },
        )
        if status in (200, 201):
            notes.append(f"Created Gitea account '{login}' (identity pre-flight).")
            created.add(login)
        elif status in (409, 422):
            # Raced with a boot lane or a manual create; leave the row alone.
            notes.append(
                f"Skipped Gitea account for '{login}': identity already taken (HTTP {status})."
            )
        else:
            raise CoderError(f"Gitea identity pre-flight create for '{login}' failed (HTTP {status}).")
    return notes, created


def _sync_gitea_identities(
    *,
    coder_hostname: str,
    session_token: str,
    gitea_hostname: str,
    provisioner_token: str,
    provisioner_username: str,
    tracked: set[str],
    coder_users: Mapping[str, str] | None = None,
    password: str = "",
) -> tuple[list[str], set[str]]:
    """Keep wizard-sprung Gitea accounts aligned with the Coder roster.

    ``coder_users`` (login -> email) may be supplied by the caller when the
    roster was already read for another identity surface; fetching it here is
    the fallback. ``password`` seeds accounts this pass springs (empty keeps
    the lane to flips-only, for callers with no secret to hand). Returns the
    human-readable notes and the updated claim set to persist in the state
    ledger.
    """
    roster = (
        dict(coder_users)
        if coder_users is not None
        else _coder_roster(coder_hostname=coder_hostname, session_token=session_token)
    )
    gitea_rows = _gitea_admin_user_rows(hostname=gitea_hostname, token=provisioner_token)
    notes: list[str] = []
    actions, claimed = _identity_sync_actions(
        gitea_rows=gitea_rows,
        coder_users=roster,
        provisioner_username=provisioner_username,
        tracked=tracked,
    )
    for username, desired_active in actions:
        status, _payload = _gitea_api_request(
            hostname=gitea_hostname,
            method="PATCH",
            path=f"/api/v1/admin/users/{parse.quote(username, safe='')}",
            token=provisioner_token,
            payload={"active": desired_active},
        )
        if status != 200:
            raise CoderError(f"Gitea identity sync for '{username}' returned HTTP {status}.")
        if desired_active:
            notes.append(f"Re-enabled Gitea account '{username}' (Coder user returned).")
        else:
            notes.append(f"Disabled Gitea account '{username}' (Coder user deleted).")
    if password:
        preflight_notes, created = _prestand_gitea_identities(
            gitea_hostname=gitea_hostname,
            provisioner_token=provisioner_token,
            coder_users=roster,
            password=password,
        )
        notes.extend(preflight_notes)
        # Freshly created rows were not in the snapshot the claim prune above
        # worked from; union them in so this pass's ledger write keeps them.
        claimed |= created
    return notes, claimed


def _gitea_identity_lane(
    *,
    state_dir: Path,
    coder_hostname: str,
    session_token: str,
    gitea_hostname: str,
    provisioner: GiteaProvisionerState,
    coder_users: Mapping[str, str] | None,
    password: str,
) -> list[str]:
    """The Gitea identity surface end to end: sync, spring, persist claims.

    Shared by the coder phase backend and the `sync-identities` command so the
    timer pass and the wizard pass can never drift on what "in sync" means.
    """
    from dokploy_wizard.state.models import STATE_FORMAT_VERSION, GiteaIdentityLedger
    from dokploy_wizard.state.store import load_gitea_identities, write_gitea_identities

    stored = load_gitea_identities(state_dir)
    tracked = set(stored.usernames) if stored is not None else set()
    notes, updated = _sync_gitea_identities(
        coder_hostname=coder_hostname,
        session_token=session_token,
        gitea_hostname=gitea_hostname,
        provisioner_token=provisioner.token,
        provisioner_username=provisioner.username,
        tracked=tracked,
        coder_users=coder_users,
        password=password,
    )
    if updated != tracked:
        write_gitea_identities(
            state_dir,
            GiteaIdentityLedger(
                format_version=STATE_FORMAT_VERSION,
                usernames=tuple(sorted(updated)),
            ),
        )
    return notes


def _litellm_identity_lane(
    *,
    coder_users: Mapping[str, str],
    admin_email: str,
    password: str,
    api: LiteLLMAdminApi,
) -> list[str]:
    """The gateway identity surface: upsert every roster person as a user.

    Password re-sync is drift-only inside `ensure_user_identities`, so a
    person's personal password survives later passes (measured on the box).
    """
    admin_login = username_from_email(admin_email).lower()
    assignments = tuple(
        LiteLLMUserAssignment(
            user_id=login,
            user_email=email,
            user_role=(
                LITELLM_USER_ROLE_ADMIN
                if login == admin_login
                else LITELLM_USER_ROLE_INTERNAL
            ),
            password=password,
        )
        for login, email in sorted(coder_users.items())
    )
    manager = LiteLLMGatewayManager(api=api, sleep_fn=lambda _: None)
    return manager.ensure_user_identities(assignments)


_CODER_IDENTITY_TIMER_NAME = "dokploy-wizard-identity"
_SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")


def _identity_timer_unit_texts(*, env_file: Path, state_dir: Path) -> tuple[str, str]:
    """Render the (service, timer) unit texts for the identity reconciliation pair.

    The service invokes the package the same way `bin/dokploy-wizard` does
    (module invocation over the repo's `src`), so the timer rides whichever
    checkout produced it — on the box, the `/root/dokploy-wizard` bundle the
    remote helper maintains. `ConditionPathExists` keeps a vanished env file
    (operator cleanup, partial uninstall) from spamming the journal: the unit
    refuses cleanly and the next wizard pass re-decides.
    """
    repo_src = Path(__file__).resolve().parents[2]
    service = (
        "[Unit]\n"
        "Description=Dokploy Wizard identity reconciliation (Coder roster -> FileX, Gitea, LiteLLM)\n"
        f"ConditionPathExists={env_file}\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Environment=PYTHONPATH={repo_src}\n"
        f"ExecStart=/usr/bin/python3 -m dokploy_wizard sync-identities "
        f"--env-file {env_file} --state-dir {state_dir}\n"
        "Nice=10\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Dokploy Wizard identity reconciliation timer\n"
        "\n"
        "[Timer]\n"
        # Boot delay lets Dokploy/Coder/Gitea/traefik finish coming up on a
        # power-cycled host; the short period is what makes "new Coder user ->
        # accounts appear" feel automatic without being noisy on the box.
        "OnBootSec=3min\n"
        "OnUnitActiveSec=5min\n"
        "AccuracySec=30s\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def _systemctl(*args: str) -> bool:
    result = subprocess.run(
        ["systemctl", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _install_identity_timer(*, state_dir: Path) -> tuple[list[str], bool]:
    """Install or refresh the identity timer pair; report (notes, ledger-owned).

    Idempotent by content: units are rewritten only when the rendered text
    changed, and the ledger row is owned whenever the pair stands on disk —
    enable failures warn but do not drop ownership (the files are ours to
    remove, whoever enabled them).
    """
    resolved_state = state_dir.expanduser().resolve()
    env_file = resolved_state.parent / ".install.env"
    service_path = _SYSTEMD_UNIT_DIR / f"{_CODER_IDENTITY_TIMER_NAME}.service"
    timer_path = _SYSTEMD_UNIT_DIR / f"{_CODER_IDENTITY_TIMER_NAME}.timer"
    present = service_path.is_file() and timer_path.is_file()
    if os.geteuid() != 0:
        # A pass without root cannot install or refresh the pair, but when the
        # pair already stands on disk it is still ours: ownership rides the
        # files (the uninstaller removes them with root later), and dropping
        # the row here would strand an enabled timer against a torn-down stack.
        return (
            ["Identity timer skipped: not running as root (every coder pass still syncs)."],
            present,
        )
    if not env_file.is_file() and not present:
        return (
            ["Identity timer skipped: no .install.env beside the state directory to bake into the unit."],
            False,
        )
    service_text, timer_text = _identity_timer_unit_texts(env_file=env_file, state_dir=resolved_state)
    notes: list[str] = []
    changed = False
    for path, text in ((service_path, service_text), (timer_path, timer_text)):
        try:
            if path.is_file() and path.read_text(encoding="utf-8") == text:
                continue
            path.write_text(text, encoding="utf-8")
            changed = True
        except OSError as exc:
            # Same rule as the non-root branch: a failed refresh of a pair
            # that already stands leaves the old (owned) units in place.
            return ([f"Identity timer skipped: cannot write '{path.name}' ({exc})."], present)
    if changed and not _systemctl("daemon-reload"):
        notes.append(
            "Identity timer warning: systemctl daemon-reload failed; the new unit text lands on next boot."
        )
    if _systemctl("enable", "--now", f"{_CODER_IDENTITY_TIMER_NAME}.timer"):
        notes.append(f"Enabled the identity reconciliation timer ({_CODER_IDENTITY_TIMER_NAME}.timer).")
    else:
        notes.append(
            f"Identity timer warning: systemctl enable --now {_CODER_IDENTITY_TIMER_NAME}.timer failed; "
            "run it by hand to activate the pair."
        )
    return notes, True


def sync_stack_identities(
    *,
    coder_hostname: str,
    admin_email: str,
    admin_password: str,
    gitea_hostname: str,
    filex_hostname: str,
    state_dir: Path,
    litellm_admin_api: LiteLLMAdminApi | None = None,
) -> list[str]:
    """Reconcile every identity surface against the live Coder roster.

    The standalone twin of the coder phase's identity block — the exact body
    the host timer runs, and what `sync-identities` exposes for operators.
    One login, one roster read, then the three lanes each fail alone (the
    doctrine the Gitea lane established). The login session created here is
    logged out on the way: `/api/v2/users/login` mints a persistent session
    per call, and a timer pass must not pile them up (measured: each login
    row shows in the Coder access tokens UI).
    """
    from dokploy_wizard.state.store import load_filex_provisioner, load_gitea_provisioner

    session_token = _coder_login(hostname=coder_hostname, email=admin_email, password=admin_password)
    notes: list[str] = []
    try:
        try:
            roster = _coder_roster(coder_hostname=coder_hostname, session_token=session_token)
        except Exception as exc:  # noqa: BLE001 - lane doctrine
            notes.append(f"Skipped identity sync: Coder roster unreadable ({_safe_progress_reason(str(exc))})")
            return notes
        if not roster:
            notes.append("Skipped identity sync: Coder roster is empty.")
            return notes
        gitea_provisioner = load_gitea_provisioner(state_dir) if gitea_hostname else None
        if gitea_provisioner is not None:
            try:
                notes.extend(
                    _gitea_identity_lane(
                        state_dir=state_dir,
                        coder_hostname=coder_hostname,
                        session_token=session_token,
                        gitea_hostname=gitea_hostname,
                        provisioner=gitea_provisioner,
                        coder_users=roster,
                        password=admin_password,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - lane doctrine
                notes.append(f"Skipped Gitea identity sync: {_safe_progress_reason(str(exc))}")
        else:
            notes.append("Gitea identity sync skipped: no provisioner token in state (pack off?).")
        filex_provisioner = load_filex_provisioner(state_dir) if filex_hostname else None
        if filex_provisioner is not None:
            try:
                notes.extend(
                    _sync_filex_identities(
                        hostname=filex_hostname,
                        provisioner_token=filex_provisioner.token,
                        coder_users=roster,
                        password=admin_password,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - lane doctrine
                notes.append(f"Skipped FileX identity sync: {_safe_progress_reason(str(exc))}")
        else:
            notes.append("FileX identity sync skipped: no provisioner token in state (pack off?).")
        if litellm_admin_api is not None:
            try:
                notes.extend(
                    _litellm_identity_lane(
                        coder_users=roster,
                        admin_email=admin_email,
                        password=admin_password,
                        api=litellm_admin_api,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - lane doctrine
                notes.append(f"Skipped LiteLLM identity sync: {_safe_progress_reason(str(exc))}")
        return notes
    finally:
        _coder_logout(hostname=coder_hostname, session_token=session_token)


def _coder_container_name(service_name: str) -> str | None:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.service={service_name}",
            "--format",
            "{{.Names}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to locate Coder container: {(result.stderr or result.stdout).strip()}"
        )
    return resolve_compose_container_name(service_name, result.stdout.splitlines())


def _default_template_dir() -> Path:
    return _default_hermes_desktop_template_dir()


def _default_template_name() -> str:
    return _default_hermes_desktop_template_name()


def _default_hermes_desktop_template_dir() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "templates"
        / "coder"
        / "default-ubuntu-vscode-hermes-desktop"
    )


def _default_hermes_desktop_template_name() -> str:
    return "ubuntu-vscode-hermes-desktop"


_LEGACY_DEFAULT_WORKSPACE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LEGACY_DEFAULT_WORKSPACE_SUFFIX = "-workspace-"
_ISO_DATE_LENGTH = len("2026-04-18")


def _default_workspace_root_token(hostname: str) -> str:
    root_domain = (
        hostname.split(".", 1)[1] if hostname.startswith("coder.") and "." in hostname else hostname
    )
    return re.sub(r"[^a-z0-9]+", "", root_domain.lower())


def _default_workspace_name(hostname: str) -> str:
    # Stable by design. The pre-Group-4 name embedded today's date, so every
    # post-midnight rerun minted a second full Hermes stack. Legacy date-stamped
    # names are adopted (renamed) or pruned by `_ensure_default_workspace`.
    suffix = "-workspace"
    max_root_length = max(1, _CODER_MAX_NAME_LENGTH - len(suffix))
    normalized_root = (_default_workspace_root_token(hostname) or "workspace")[:max_root_length]
    return f"{normalized_root}{suffix}"


def _legacy_default_workspace_names(
    hostname: str, workspace_names: Sequence[str]
) -> tuple[str, ...]:
    """Wizard-created date-stamped names (the pre-Group-4 pattern), oldest first.

    The prefix mirrors the old truncation exactly: the dated suffix consumed 21 of
    the 32 allowed characters, so legacy roots were clamped to 11 characters.
    """

    legacy_max_root = max(
        1, _CODER_MAX_NAME_LENGTH - len(_LEGACY_DEFAULT_WORKSPACE_SUFFIX) - _ISO_DATE_LENGTH
    )
    legacy_root = (_default_workspace_root_token(hostname) or "workspace")[:legacy_max_root]
    prefix = f"{legacy_root}{_LEGACY_DEFAULT_WORKSPACE_SUFFIX}"
    return tuple(
        sorted(
            name
            for name in workspace_names
            if name.startswith(prefix)
            and _LEGACY_DEFAULT_WORKSPACE_DATE_RE.match(name[len(prefix) :])
        )
    )


def _required_template_names() -> tuple[str, ...]:
    # The collapse: one seeded template. Everything the wizard ever pushed before
    # it lives in `_retired_template_names()` and is removed on the next pass.
    return (_default_template_name(),)


def _retired_template_names() -> tuple[str, ...]:
    # Workspaces still on a retired template are migrated in place first (see
    # `_migrate_retired_template_workspaces`), so the delete never orphans one.
    return (
        "ubuntu-vscode-openwork",
        "ubuntu-vscode-pi-web",
        "ubuntu-vscode-hermes",
        "ubuntu-vscode",
        "ubuntu-vscode-opencode-web",
        "ubuntu-vscode-kdense-byok",
        "ubuntu-vscode-opencode-hermes-pi",
    )


def _ensure_template_name_length(template_name: str) -> None:
    if len(template_name) > _CODER_MAX_NAME_LENGTH:
        raise CoderError(
            f"Coder template name '{template_name}' is {len(template_name)} characters; "
            f"Coder accepts at most {_CODER_MAX_NAME_LENGTH}, so no workspace can ever "
            "be created from it."
        )


def _seed_template(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    template_name: str,
    template_dir: Path,
    replacements: dict[str, str] | None,
) -> bool:
    _ensure_template_name_length(template_name)
    _emit_coder_progress(f"Checking Coder template '{template_name}'.")
    desired_version_name = _template_version_name(
        template_dir=template_dir,
        replacements=replacements,
    )
    if (
        _active_template_version_name(
            container_name=container_name,
            hostname=hostname,
            session_token=session_token,
            template_name=template_name,
        )
        == desired_version_name
    ):
        _emit_coder_progress(
            f"Coder template '{template_name}' already has the desired active version."
        )
        return False
    if desired_version_name in _template_version_names(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
        template_name=template_name,
    ):
        _emit_coder_progress(
            f"Coder template '{template_name}' already has desired version '{desired_version_name}'."
        )
        return False
    _copy_template_into_container(
        container_name=container_name,
        template_dir=template_dir,
        template_name=template_name,
        replacements=replacements,
    )
    _emit_coder_progress(
        f"Pushing Coder template '{template_name}' as version '{desired_version_name}'."
    )
    try:
        _push_default_template(
            container_name=container_name,
            hostname=hostname,
            session_token=session_token,
            template_name=template_name,
            template_version_name=desired_version_name,
        )
    except CoderError as e:
        _emit_coder_progress(
            f"Coder template '{template_name}' push failed: {_safe_progress_reason(str(e), limit=6000)}"
        )
        raise
    _emit_coder_progress(f"Finished Coder template '{template_name}' push.")
    return True


def _emit_coder_progress(message: str) -> None:
    print(f"[dokploy-wizard] {message}", file=sys.stderr)


def _safe_progress_reason(reason: str, *, limit: int = 360) -> str:
    safe = " ".join(redact_text(reason).split())
    if len(safe) <= limit:
        return safe
    # Clip the middle, not the end. A terraform diagnosis arrives after the banner and
    # the streamed log, so a head-only clip reliably threw away the one line that said
    # what was wrong and kept only "exit status 1".
    keep = max(1, (limit - 4) // 2)
    return f"{safe[:keep]} … {safe[-keep:]}"


def _template_version_name(*, template_dir: Path, replacements: dict[str, str] | None) -> str:
    digest = hashlib.sha256()
    with _rendered_template_dir(template_dir=template_dir, replacements=replacements) as rendered_dir:
        for path in sorted(path for path in rendered_dir.rglob("*") if path.is_file()):
            digest.update(path.relative_to(rendered_dir).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return f"dokploy-wizard-{digest.hexdigest()[:16]}"


def _copy_template_into_container(
    *,
    container_name: str,
    template_dir: Path,
    template_name: str,
    replacements: dict[str, str] | None,
) -> None:
    subprocess.run(
        ["docker", "exec", container_name, "rm", "-rf", f"/tmp/{template_name}"],
        check=False,
        capture_output=True,
        text=True,
    )
    with _rendered_template_dir(template_dir=template_dir, replacements=replacements) as copy_source:
        _docker_copy_template_dir(
            container_name=container_name,
            template_name=template_name,
            template_dir=copy_source,
        )


@contextmanager
def _rendered_template_dir(
    *, template_dir: Path, replacements: dict[str, str] | None
) -> Iterator[Path]:
    if not template_dir.exists():
        raise CoderError(f"Default Coder template directory is missing: {template_dir}")
    if not replacements:
        yield template_dir
        return
    with tempfile.TemporaryDirectory(prefix="dokploy-wizard-coder-template-") as tmp_dir:
        rendered_dir = Path(tmp_dir) / template_dir.name
        shutil.copytree(template_dir, rendered_dir)
        rendered_main_tf = rendered_dir / "main.tf"
        contents = rendered_main_tf.read_text(encoding="utf-8")
        for placeholder, value in replacements.items():
            contents = contents.replace(placeholder, value)
        rendered_main_tf.write_text(contents, encoding="utf-8")
        yield rendered_dir


def _docker_copy_template_dir(
    *, container_name: str, template_name: str, template_dir: Path
) -> None:
    result = subprocess.run(
        ["docker", "cp", str(template_dir), f"{container_name}:/tmp/{template_name}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to copy default Coder template into container: {(result.stderr or result.stdout).strip()}"
        )


def _coder_cli_url() -> str:
    return "http://127.0.0.1:3000"


def _push_default_template(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    template_name: str,
    template_version_name: str | None = None,
) -> None:
    command = [
        "docker",
        "exec",
        "-e",
        f"CODER_URL={_coder_cli_url()}",
        "-e",
        f"CODER_SESSION_TOKEN={session_token}",
        container_name,
        "/opt/coder",
        "templates",
        "push",
        template_name,
    ]
    if template_version_name:
        command.extend(["--name", template_version_name])
    command.extend(
        [
            "--directory",
            f"/tmp/{template_name}",
            "--ignore-lockfile",
            "--yes",
        ]
    )
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output = "\n".join(part for part in (result.stderr, result.stdout) if part).strip()
        if template_version_name and _is_duplicate_template_version_error(
            output=output,
            template_version_name=template_version_name,
        ):
            return
        raise CoderError(
            f"Unable to push default Coder template '{template_name}': {output}"
        )


def _is_duplicate_template_version_error(*, output: str, template_version_name: str) -> bool:
    if not output:
        return False
    normalized_output = output.lower()
    if template_version_name.lower() not in normalized_output:
        return False
    if "template version" not in normalized_output:
        return False
    return (
        "already exists" in normalized_output
        or "already in use" in normalized_output
    )


def _sync_hermes_workspace_secrets(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    hermes_inference_provider: str,
    hermes_model: str,
    ai_default_base_url: str,
    ai_default_api_key: str | None,
) -> None:
    managed_values = (
        (
            "hermes-inference-provider",
            "HERMES_INFERENCE_PROVIDER",
            hermes_inference_provider,
            "Hermes provider for wizard-managed workspaces.",
        ),
        (
            "hermes-model",
            "HERMES_MODEL",
            hermes_model,
            "Hermes model for wizard-managed workspaces.",
        ),
        (
            "hermes-openai-api-base",
            "OPENAI_API_BASE",
            ai_default_base_url,
            "Hermes LiteLLM base URL for wizard-managed workspaces.",
        ),
    )
    for secret_name, env_name, value, description in managed_values:
        try:
            _upsert_coder_secret(
                container_name=container_name,
                hostname=hostname,
                session_token=session_token,
                secret_name=secret_name,
                env_name=env_name,
                value=value,
                description=description,
            )
        except CoderError as error:
            if "unknown flag: --env" in str(error):
                return
            raise
    if ai_default_api_key:
        try:
            _upsert_coder_secret(
                container_name=container_name,
                hostname=hostname,
                session_token=session_token,
                secret_name="hermes-openai-api-key",
                env_name="OPENAI_API_KEY",
                value=ai_default_api_key,
                description="Hermes LiteLLM virtual key for wizard-managed workspaces.",
            )
        except CoderError as error:
            if "unknown flag: --env" not in str(error):
                raise


def _upsert_coder_secret(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    secret_name: str,
    env_name: str,
    value: str,
    description: str,
) -> None:
    command_prefix = [
        "docker",
        "exec",
        "-i",
        "-e",
        f"CODER_URL={_coder_cli_url()}",
        "-e",
        f"CODER_SESSION_TOKEN={session_token}",
        container_name,
        "/opt/coder",
        "secret",
    ]
    update_result = subprocess.run(
        [
            *command_prefix,
            "update",
            secret_name,
            "--env",
            env_name,
            "--description",
            description,
        ],
        input=value,
        check=False,
        capture_output=True,
        text=True,
    )
    if update_result.returncode == 0:
        return
    create_result = subprocess.run(
        [
            *command_prefix,
            "create",
            secret_name,
            "--env",
            env_name,
            "--description",
            description,
        ],
        input=value,
        check=False,
        capture_output=True,
        text=True,
    )
    if create_result.returncode != 0:
        raise CoderError(
            f"Unable to sync Coder secret '{secret_name}': {(create_result.stderr or create_result.stdout).strip()}"
        )


def _shell_double_quote_escape(value: str) -> str:
    """Escape a substitution for templates' Terraform *quoted string* context.

    Placeholders like ``VAR="__PLACEHOLDER__"`` are evaluated by Terraform's
    string lexer before the startup script ever reaches bash, so a backslash
    escape (``\\"``) would be consumed by Terraform and let embedded quotes
    terminate the literal. Doubling dollars keeps generated ``${...}`` inert;
    plain quotes are legal here (JSON fallback lists rely on it).
    """

    return value.replace("$", "$$")


def _litellm_workspace_fallback_models_json(*, default_alias: str) -> str:
    """Last-resort workspace model list when the gateway is unreachable at boot.

    Everything else comes from the gateway's live /v1/models (which the sync
    sidecar keeps authoritative), so baking pinned catalog samples here would
    only resurrect stale aliases. The default alias route always exists.
    """

    return json.dumps([default_alias])


def _copilot_byok_contract() -> dict[str, str]:
    return {
        "setting": _COPILOT_BYOK_SETTING_KEY,
        "provider_label": _COPILOT_BYOK_PROVIDER_LABEL,
        "scope": "chat-and-agents-only",
        "limitation": _COPILOT_BYOK_CHAT_ONLY_LIMITATION,
        "reference": "https://code.visualstudio.com/docs/copilot/customization/language-models",
    }


def _copilot_byok_openai_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/v1") or normalized.endswith("/v1/chat/completions"):
        return normalized
    return f"{normalized}/v1"


def _copilot_byok_model_ids(*, default_alias: str, fallback_models_json: str) -> tuple[str, ...]:
    model_ids: list[str] = []
    try:
        parsed = json.loads(fallback_models_json)
    except json.JSONDecodeError:
        parsed = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str) and item.strip():
                model_ids.append(item.strip())
    if default_alias and default_alias not in model_ids:
        model_ids.insert(0, default_alias)
    return tuple(dict.fromkeys(model_ids))


def _copilot_byok_settings_json(
    *,
    base_url: str,
    api_key: str,
    default_alias: str,
    fallback_models_json: str,
) -> str:
    openai_base_url = _copilot_byok_openai_base_url(base_url)
    model_ids = _copilot_byok_model_ids(
        default_alias=default_alias, fallback_models_json=fallback_models_json
    )
    custom_models = {
        model_id: {
            "name": f"{_COPILOT_BYOK_PROVIDER_LABEL}: {model_id}",
            "model": model_id,
            "url": openai_base_url,
            "apiKey": api_key,
            "keyStorage": "dokploy-litellm",
            "requiresAPIKey": bool(api_key),
            "toolCalling": True,
            "vision": False,
            "thinking": False,
            "maxInputTokens": 131072,
            "maxOutputTokens": 8192,
        }
        for model_id in model_ids
    }
    settings = {
        _COPILOT_BYOK_SETTING_KEY: custom_models,
    }
    return json.dumps(settings, indent=2, sort_keys=True)


def _copilot_byok_compatibility_probe() -> str:
    return (
        f"official-byok-supported: target-setting={_COPILOT_BYOK_SETTING_KEY}; "
        "scope=chat-and-agents-only; "
        "requires VS Code/code-server build with Copilot Chat BYOK/OpenAI-compatible model support; "
        "no secrets required for this dry-run probe"
    )


def _coder_state_gap_diagnosis(state_files: tuple[str, ...]) -> str:
    present = set(state_files)
    missing = sorted(_CODER_STATE_REQUIRED_FOR_MODIFY - present)
    if not missing:
        return "state-complete: modify can perform normal non-destructive Coder template reseed"
    return (
        "state-incomplete: focused-coder-template-reseed-required; "
        f"missing={','.join(missing)}; do-not-reinstall"
    )


def _coder_live_template_update_command_bundle() -> tuple[str, ...]:
    return (
        "./bin/dokploy-wizard inspect-state --env-file ./.install.env --state-dir .dokploy-wizard-state",
        (
            "python3 - <<'PY'\n"
            "# Focused Coder template reseed only. Do not print DOKPLOY_API_KEY, Coder session tokens, or LiteLLM keys.\n"
            "# 1. Load .install.env and .dokploy-wizard-state.\n"
            "# 2. If applied-state.json and ownership-ledger.json exist, run non-interactive modify to invoke DokployCoderBackend.ensure_application_ready().\n"
            "# 3. If state is incomplete, authenticate to live Coder with redacted operator credentials, copy templates/coder/* into the running Coder container, and run /opt/coder templates push for the four wizard-managed templates only.\n"
            "# 4. Query template versions and assert github.copilot.chat.customOAIModels exists in all four payloads.\n"
            "print('focused-coder-template-reseed <redacted credentials>')\n"
            "PY"
        ),
    )


def _list_workspaces(*, container_name: str, hostname: str, session_token: str) -> tuple[str, ...]:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL={_coder_cli_url()}",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "list",
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to list Coder workspaces: {(result.stderr or result.stdout).strip()}"
        )
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise CoderError("Coder workspace list returned invalid JSON.") from exc
    if not isinstance(payload, list):
        raise CoderError("Coder workspace list returned an unexpected payload shape.")
    names: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def _list_templates(
    *, container_name: str, hostname: str, session_token: str
) -> tuple[dict[str, object], ...]:
    del hostname
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL={_coder_cli_url()}",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "templates",
            "list",
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to list Coder templates: {(result.stderr or result.stdout).strip()}"
        )
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise CoderError("Coder template list returned invalid JSON.") from exc
    if not isinstance(payload, list):
        raise CoderError("Coder template list returned an unexpected payload shape.")
    templates: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        template = item.get("Template")
        templates.append(template if isinstance(template, dict) else item)
    return tuple(templates)


def _retire_removed_templates(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
) -> tuple[str, ...]:
    existing = {
        item.get("name")
        for item in _list_templates(
            container_name=container_name,
            hostname=hostname,
            session_token=session_token,
        )
        if isinstance(item.get("name"), str)
    }
    retired: list[str] = []
    for template_name in _retired_template_names():
        if template_name not in existing:
            continue
        if _delete_template(
            container_name=container_name,
            session_token=session_token,
            template_name=template_name,
        ):
            retired.append(template_name)
    return tuple(retired)


def _delete_template(
    *,
    container_name: str,
    session_token: str,
    template_name: str,
) -> bool:
    _emit_coder_progress(f"Retiring removed Coder template '{template_name}'.")
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL={_coder_cli_url()}",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "templates",
            "delete",
            template_name,
            "--yes",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output = "\n".join(part for part in (result.stderr, result.stdout) if part).strip()
        if _is_missing_template_error(output=output):
            return False
        raise CoderError(
            f"Unable to retire Coder template '{template_name}': {_safe_progress_reason(output)}"
        )
    return True


def _is_missing_template_error(*, output: str) -> bool:
    normalized_output = output.lower()
    return "template not found" in normalized_output or "no templates found" in normalized_output


def _active_template_version_name(
    *, container_name: str, hostname: str, session_token: str, template_name: str
) -> str | None:
    for item in _list_template_versions(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
        template_name=template_name,
    ):
        if item.get("active") is True:
            name = item.get("name")
            if isinstance(name, str) and name:
                return name
            return None
    return None


def _template_version_names(
    *, container_name: str, hostname: str, session_token: str, template_name: str
) -> tuple[str, ...]:
    names: list[str] = []
    for item in _list_template_versions(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
        template_name=template_name,
    ):
        name = item.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def _list_template_versions(
    *, container_name: str, hostname: str, session_token: str, template_name: str
) -> tuple[dict[str, object], ...]:
    template_exists = False
    for item in _list_templates(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
    ):
        name = item.get("name")
        if isinstance(name, str) and name == template_name:
            template_exists = True
            break
    if not template_exists:
        return ()
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL={_coder_cli_url()}",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "templates",
            "versions",
            "list",
            template_name,
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to list Coder template versions for '{template_name}': {(result.stderr or result.stdout).strip()}"
        )
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise CoderError(f"Coder template version list for '{template_name}' returned invalid JSON.") from exc
    versions: list[dict[str, object]]
    if isinstance(payload, list):
        versions = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        raw_versions = payload.get("versions")
        if not isinstance(raw_versions, list):
            raise CoderError(
                f"Coder template version list for '{template_name}' returned an unexpected payload shape."
            )
        versions = [item for item in raw_versions if isinstance(item, dict)]
    else:
        raise CoderError(
            f"Coder template version list for '{template_name}' returned an unexpected payload shape."
        )
    return tuple(versions)


def _create_default_workspace(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    workspace_name: str,
    template_name: str,
) -> None:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL={_coder_cli_url()}",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "create",
            workspace_name,
            "--template",
            template_name,
            "--use-parameter-defaults",
            "--yes",
            "--no-wait",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to create default Coder workspace '{workspace_name}': {(result.stderr or result.stdout).strip()}"
        )


@dataclass(frozen=True)
class DefaultWorkspaceOutcome:
    """What `_ensure_default_workspace` did: ``exists``, ``created``, or ``adopted``.

    ``adopted_from`` carries the legacy date-stamped name that was renamed to the
    stable default name; ``pruned`` lists wizard-created date-stamped duplicates
    that were deleted after the stable workspace was in place. ``updated`` is
    true when the workspace was out of date on the active template version and
    the wizard ran ``coder update`` to move it onto that version — the step
    that makes a template change (a new ~/shared block, a new container flag)
    land inside running workspaces on the same modify pass.
    """

    action: str
    adopted_from: str | None = None
    pruned: tuple[str, ...] = ()
    updated: bool = False


def _workspace_template_name(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    workspace_name: str,
) -> str | None:
    """Best-effort template name for a workspace; None when the list shape is unknown.

    Read from ``coder list``, not ``coder show``: the show subcommand's ``--output``
    flag is not portable across Coder builds (v2.38.3 rejects it outright, measured
    on a box upgraded from an older wizard), and silently returning None there let
    every legacy workspace escape the migrate lane, blocking template retirement
    forever. The list payload is the shape the rest of this module already parses
    on every pass (flat ``owner_name``/``template_name``).
    """

    del hostname
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL={_coder_cli_url()}",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "list",
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    for item in payload:
        if not isinstance(item, dict) or item.get("name") != workspace_name:
            continue
        direct = item.get("template_name")
        if isinstance(direct, str) and direct:
            return direct
        latest_build = item.get("latest_build")
        if isinstance(latest_build, dict):
            build_template_name = latest_build.get("template_name")
            if isinstance(build_template_name, str) and build_template_name:
                return build_template_name
            template = latest_build.get("template")
            if isinstance(template, dict):
                name = template.get("name")
                if isinstance(name, str) and name:
                    return name
    return None


def _rename_workspace(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    workspace_name: str,
    new_workspace_name: str,
) -> None:
    del hostname
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL={_coder_cli_url()}",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "update",
            workspace_name,
            "--name",
            new_workspace_name,
            "--yes",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to rename Coder workspace '{workspace_name}' to '{new_workspace_name}': "
            f"{(result.stderr or result.stdout).strip()}"
        )


def _migrate_retired_template_workspaces(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    template_name: str,
) -> tuple[str, ...]:
    """Move every workspace off a retiring template, preserving the workspace itself.

    The home volume is `coder-<workspace-id>-home` with `ignore_changes = all`, so
    the id must survive: `coder update --template` re-bases the workspace in place,
    while a delete-and-recreate would hand the user an empty home.
    """

    retired = set(_retired_template_names())
    migrated: list[str] = []
    for workspace_name in _list_workspaces(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
    ):
        observed = _workspace_template_name(
            container_name=container_name,
            hostname=hostname,
            session_token=session_token,
            workspace_name=workspace_name,
        )
        if observed is None or observed not in retired:
            continue
        _update_workspace_template(
            container_name=container_name,
            hostname=hostname,
            session_token=session_token,
            workspace_name=workspace_name,
            template_name=template_name,
        )
        migrated.append(workspace_name)
    return tuple(migrated)


def _update_workspace_template(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    workspace_name: str,
    template_name: str,
) -> None:
    del hostname
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL={_coder_cli_url()}",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "update",
            workspace_name,
            "--template",
            template_name,
            "--yes",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to move Coder workspace '{workspace_name}' onto template "
            f"'{template_name}': {(result.stderr or result.stdout).strip()}"
        )


def _delete_workspace(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    workspace_name: str,
) -> None:
    del hostname
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL={_coder_cli_url()}",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "delete",
            workspace_name,
            "--yes",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CoderError(
            f"Unable to delete Coder workspace '{workspace_name}': "
            f"{(result.stderr or result.stdout).strip()}"
        )


def _ensure_default_workspace(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    workspace_name: str,
    template_name: str,
) -> DefaultWorkspaceOutcome:
    """Adopt-or-name the single default workspace and prune wizard-created duplicates.

    The name is stable, so a rerun is a noop. An existing legacy date-stamped
    workspace on the default template is adopted via rename instead of being
    duplicated; leftover date-stamped duplicates on that template are pruned once
    the stable workspace is in place. A legacy-shaped name on a foreign template
    is left alone: the date shape is the wizard's own fingerprint, but a template
    the wizard did not seed is the user's. A legacy name whose template cannot be
    determined is still treated as wizard-created, so a CLI shape change can never
    resurrect the dated-duplicate bug.
    """

    workspace_names = _list_workspaces(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
    )
    legacy_names = _legacy_default_workspace_names(hostname, workspace_names)
    adopted_from: str | None = None
    if workspace_name in workspace_names:
        action = "exists"
    else:
        action = "created"
        for candidate in reversed(legacy_names):  # newest date first
            if _workspace_is_wizard_owned(
                container_name=container_name,
                hostname=hostname,
                session_token=session_token,
                workspace_name=candidate,
                template_name=template_name,
            ):
                _rename_workspace(
                    container_name=container_name,
                    hostname=hostname,
                    session_token=session_token,
                    workspace_name=candidate,
                    new_workspace_name=workspace_name,
                )
                adopted_from = candidate
                action = "adopted"
                break
        if action == "created":
            _create_default_workspace(
                container_name=container_name,
                hostname=hostname,
                session_token=session_token,
                workspace_name=workspace_name,
                template_name=template_name,
            )
    pruned: list[str] = []
    for candidate in legacy_names:
        if candidate == adopted_from:
            continue
        if _workspace_is_wizard_owned(
            container_name=container_name,
            hostname=hostname,
            session_token=session_token,
            workspace_name=candidate,
            template_name=template_name,
        ):
            _delete_workspace(
                container_name=container_name,
                hostname=hostname,
                session_token=session_token,
                workspace_name=candidate,
            )
            pruned.append(candidate)
    updated = _update_workspace_if_outdated(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
        workspace_name=workspace_name,
        template_name=template_name,
    )
    return DefaultWorkspaceOutcome(
        action=action,
        adopted_from=adopted_from,
        pruned=tuple(pruned),
        updated=updated,
    )


def _update_workspace_if_outdated(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    workspace_name: str,
    template_name: str,
) -> bool:
    """Bring the wizard's default workspace onto the active template version.

    A template push only creates a version; the running workspace keeps its
    build until something updates it. Without this step the changes a modify
    pass just pushed (the ~/shared bootstrap is the concrete case) would sit
    unreachable until a human ran ``coder update`` — so the wizard owns the
    last mile for the workspace it owns. Only the default workspace on the
    default template: anything else belongs to the user, mid-session rebuilds
    included.
    """
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"CODER_URL={_coder_cli_url()}",
            "-e",
            f"CODER_SESSION_TOKEN={session_token}",
            container_name,
            "/opt/coder",
            "list",
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, list):
        return False
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("name") != workspace_name or item.get("template_name") != template_name:
            continue
        if item.get("outdated") is not True:
            return False
        owner = item.get("owner_name")
        target = f"{owner}/{workspace_name}" if isinstance(owner, str) and owner else workspace_name
        update = subprocess.run(
            [
                "docker",
                "exec",
                "-e",
                f"CODER_URL={_coder_cli_url()}",
                "-e",
                f"CODER_SESSION_TOKEN={session_token}",
                container_name,
                "/opt/coder",
                "update",
                target,
                "--yes",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if update.returncode != 0:
            raise CoderError(
                f"Unable to update Coder workspace '{target}' onto the active template "
                f"version: {(update.stderr or update.stdout).strip()}"
            )
        return True
    return False


def _workspace_is_wizard_owned(
    *,
    container_name: str,
    hostname: str,
    session_token: str,
    workspace_name: str,
    template_name: str,
) -> bool:
    observed_template = _workspace_template_name(
        container_name=container_name,
        hostname=hostname,
        session_token=session_token,
        workspace_name=workspace_name,
    )
    return observed_template is None or observed_template == template_name


def _username_from_email(email: str) -> str:
    return username_from_email(email)


def _display_name_from_email(email: str) -> str:
    return display_name_from_email(email)


@dataclass(frozen=True)
class _CoderHTTPError(RuntimeError):
    status: int
