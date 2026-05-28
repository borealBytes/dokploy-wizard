"""Central Dokploy compose environment specification and reconciliation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from dokploy_wizard.dokploy.client import DokployComposeRecord, DokployDeployResult

_REQUIRED_PLACEHOLDER_RE = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<operator>:?\?)[^}]*\}"
)
_SERVICE_LINE_RE = re.compile(r"^(?P<indent>\s*)(?P<name>[A-Za-z0-9_.-]+):\s*(?:#.*)?$")
_WIZARD_ENV_MARKER_PREFIX = "# dokploy-wizard-env"
_EMPTY_COMPOSE_FILE = "services: {}\n"


class DokployEnvValidationError(ValueError):
    """Raised when rendered compose and env specs violate the secret contract."""


@dataclass(frozen=True)
class DokployEnvVar:
    """One Dokploy compose environment variable value."""

    name: str
    value: str
    sensitive: bool
    source: str


@dataclass(frozen=True)
class DokployEnvSpec:
    """Ownership and exposure policy for one Dokploy compose environment value."""

    variable: DokployEnvVar
    owner: str
    target_services: tuple[str, ...]
    placeholder: str | None = None
    required: bool = True
    dokploy_scope: str = "compose"
    ownership_marker: str = "dokploy-wizard"
    redacted_fingerprint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_services", tuple(self.target_services))
        if self.redacted_fingerprint is None:
            fingerprint = hashlib.sha256(self.variable.value.encode("utf-8")).hexdigest()[:12]
            object.__setattr__(self, "redacted_fingerprint", f"sha256:{fingerprint}")

    @property
    def name(self) -> str:
        return self.variable.name

    @property
    def value(self) -> str:
        return self.variable.value

    @property
    def sensitive(self) -> bool:
        return self.variable.sensitive

    @property
    def source(self) -> str:
        return self.variable.source


@dataclass(frozen=True)
class RenderedCompose:
    """Rendered compose text plus the env specs it is allowed to reference."""

    compose_file: str
    env_specs: tuple[DokployEnvSpec, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "env_specs", tuple(self.env_specs))


class DokployEnvComposeApi(Protocol):
    def create_compose(
        self,
        *,
        name: str,
        environment_id: str,
        compose_file: str,
        app_name: str,
        env: str | None = None,
    ) -> DokployComposeRecord: ...

    def update_compose(
        self,
        *,
        compose_id: str,
        compose_file: str | None = None,
        env: str | None = None,
    ) -> DokployComposeRecord: ...

    def deploy_compose(
        self, *, compose_id: str, title: str | None, description: str | None
    ) -> DokployDeployResult: ...


RecordT = TypeVar("RecordT", bound=DokployComposeRecord)


class DokployEnvReconciler:
    """Validate and apply Dokploy compose env payloads before compose deploys."""

    def __init__(self, *, client: DokployEnvComposeApi) -> None:
        self._client = client

    def validate_rendered_compose(self, rendered: RenderedCompose) -> None:
        specs_by_name = _index_specs_by_name(rendered.env_specs)
        service_ranges = _service_ranges(rendered.compose_file)
        placeholder_names: set[str] = set()

        for spec in rendered.env_specs:
            if spec.sensitive:
                self._reject_raw_sensitive_value(rendered.compose_file, spec, service_ranges)

        for occurrence in _required_placeholder_occurrences(rendered.compose_file, service_ranges):
            placeholder_names.add(occurrence.name)
            specs = specs_by_name.get(occurrence.name, ())
            if len(specs) != 1:
                raise DokployEnvValidationError(
                    "Required compose placeholder "
                    f"'{occurrence.name}' in service '{occurrence.service or '<unknown>'}' "
                    f"must have exactly one Dokploy env spec; found {len(specs)}."
                )
            spec = specs[0]
            if spec.target_services and occurrence.service not in spec.target_services:
                raise DokployEnvValidationError(
                    "Env placeholder exposure is not allowed: "
                    f"key '{spec.name}' for owner '{spec.owner}' appeared in service "
                    f"'{occurrence.service or '<unknown>'}', expected one of "
                    f"{', '.join(spec.target_services)}."
                )

        for spec in rendered.env_specs:
            if spec.required and spec.name not in placeholder_names:
                expected = spec.placeholder or spec.name
                raise DokployEnvValidationError(
                    f"Required Dokploy env spec '{spec.name}' for owner '{spec.owner}' "
                    f"is not referenced by required compose placeholder '{expected}'."
                )

    def build_env_payload(
        self, rendered: RenderedCompose, *, existing_env_text: str | None = None
    ) -> str:
        self.validate_rendered_compose(rendered)
        return self.merge_env_text(existing_env_text or "", rendered.env_specs)

    def serialize_env_payload(
        self,
        rendered_or_specs: RenderedCompose | Sequence[DokployEnvSpec],
        *,
        existing_env_text: str | None = None,
    ) -> str:
        if isinstance(rendered_or_specs, RenderedCompose):
            return self.build_env_payload(
                rendered_or_specs,
                existing_env_text=existing_env_text,
            )
        return self.merge_env_text(existing_env_text or "", rendered_or_specs)

    def merge_env_text(
        self, existing_env_text: str, env_specs: Sequence[DokployEnvSpec]
    ) -> str:
        preserved = _preserve_unrelated_env_lines(existing_env_text, env_specs)
        rendered_block = _render_wizard_env_block(env_specs)
        if not preserved:
            return rendered_block
        if not rendered_block:
            return "\n".join(preserved).rstrip() + "\n"
        return "\n".join(preserved).rstrip() + "\n" + rendered_block

    def reconcile_compose(
        self,
        *,
        name: str,
        environment_id: str,
        app_name: str,
        rendered: RenderedCompose,
        existing_compose_id: str | None,
        existing_env_text: str | None = None,
        title: str | None = None,
        description: str | None = None,
    ) -> DokployComposeRecord:
        env_payload = self.build_env_payload(rendered, existing_env_text=existing_env_text)
        if existing_compose_id is None:
            created = self._client.create_compose(
                name=name,
                environment_id=environment_id,
                compose_file=_EMPTY_COMPOSE_FILE,
                app_name=app_name,
            )
            compose_id = created.compose_id
        else:
            compose_id = existing_compose_id

        self._client.update_compose(compose_id=compose_id, env=env_payload)
        updated = self._client.update_compose(
            compose_id=compose_id,
            compose_file=rendered.compose_file,
        )
        deployment = self._client.deploy_compose(
            compose_id=updated.compose_id,
            title=title,
            description=description,
        )
        if deployment is not None and not deployment.success:
            msg = f"Dokploy deploy for compose service '{name}' did not report success."
            raise RuntimeError(msg)
        return updated

    def _reject_raw_sensitive_value(
        self,
        compose_file: str,
        spec: DokployEnvSpec,
        service_ranges: Sequence[ServiceRange],
    ) -> None:
        if spec.value == "" or spec.value not in compose_file:
            return
        line_index = compose_file[: compose_file.index(spec.value)].count("\n")
        service = _service_for_line(line_index, service_ranges)
        raise DokployEnvValidationError(
            "Sensitive env value must not be rendered as a raw compose scalar: "
            f"key '{spec.name}' for owner '{spec.owner}' in service "
            f"'{service or '<unknown>'}' leaked its raw value."
        )


@dataclass(frozen=True)
class ServiceRange:
    name: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class PlaceholderOccurrence:
    name: str
    service: str | None


def _index_specs_by_name(
    env_specs: Iterable[DokployEnvSpec],
) -> dict[str, tuple[DokployEnvSpec, ...]]:
    grouped: dict[str, list[DokployEnvSpec]] = {}
    for spec in env_specs:
        grouped.setdefault(spec.name, []).append(spec)
    return {name: tuple(specs) for name, specs in grouped.items()}


def _required_placeholder_occurrences(
    compose_file: str, service_ranges: Sequence[ServiceRange]
) -> tuple[PlaceholderOccurrence, ...]:
    occurrences: list[PlaceholderOccurrence] = []
    for line_index, line in enumerate(compose_file.splitlines()):
        for match in _REQUIRED_PLACEHOLDER_RE.finditer(line):
            occurrences.append(
                PlaceholderOccurrence(
                    name=match.group("name"),
                    service=_service_for_line(line_index, service_ranges),
                )
            )
    return tuple(occurrences)


def _service_ranges(compose_file: str) -> tuple[ServiceRange, ...]:
    lines = compose_file.splitlines()
    services_indent: int | None = None
    service_entries: list[tuple[str, int, int]] = []
    for line_index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "services:":
            services_indent = len(line) - len(line.lstrip())
            continue
        if services_indent is None or stripped == "" or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= services_indent:
            services_indent = None
            continue
        if indent == services_indent + 2:
            match = _SERVICE_LINE_RE.match(line)
            if match is not None:
                service_entries.append((match.group("name"), line_index, indent))

    ranges: list[ServiceRange] = []
    for entry_index, (name, start_line, _indent) in enumerate(service_entries):
        end_line = (
            service_entries[entry_index + 1][1]
            if entry_index + 1 < len(service_entries)
            else len(lines)
        )
        ranges.append(ServiceRange(name=name, start_line=start_line, end_line=end_line))
    return tuple(ranges)


def _service_for_line(line_index: int, service_ranges: Sequence[ServiceRange]) -> str | None:
    for service_range in service_ranges:
        if service_range.start_line <= line_index < service_range.end_line:
            return service_range.name
    return None


def _render_wizard_env_block(env_specs: Sequence[DokployEnvSpec]) -> str:
    lines: list[str] = []
    for spec in env_specs:
        lines.append(
            f"{_WIZARD_ENV_MARKER_PREFIX} marker={spec.ownership_marker} "
            f"owner={spec.owner} key={spec.name} fingerprint={spec.redacted_fingerprint}"
        )
        lines.append(f"{spec.name}={_dotenv_value(spec.value)}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _preserve_unrelated_env_lines(
    existing_env_text: str, env_specs: Sequence[DokployEnvSpec]
) -> list[str]:
    owned_pairs = {(spec.ownership_marker, spec.owner) for spec in env_specs}
    preserved: list[str] = []
    skip_next_assignment = False
    for line in existing_env_text.splitlines():
        if skip_next_assignment:
            skip_next_assignment = False
            if _looks_like_env_assignment(line):
                continue
        if line.startswith(_WIZARD_ENV_MARKER_PREFIX):
            marker = _marker_field(line, "marker")
            owner = _marker_field(line, "owner")
            if marker is not None and owner is not None and (marker, owner) in owned_pairs:
                skip_next_assignment = True
                continue
        preserved.append(line)
    return preserved


def _marker_field(line: str, field_name: str) -> str | None:
    prefix = f"{field_name}="
    for part in line.split():
        if part.startswith(prefix):
            return part.removeprefix(prefix)
    return None


def _looks_like_env_assignment(line: str) -> bool:
    if line == "" or line.lstrip().startswith("#") or "=" not in line:
        return False
    key = line.split("=", 1)[0]
    return re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key) is not None


def _dotenv_value(value: str) -> str:
    if value == "":
        return '""'
    if re.match(r"^[A-Za-z0-9_./:@%+=,-]+$", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'
