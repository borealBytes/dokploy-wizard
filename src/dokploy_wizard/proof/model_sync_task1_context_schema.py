"""Value-free Task 1 proof-context schema and canonical encodings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

TASK1_PROOF_OVERLAY_KEYS: Final = frozenset(
    {
        "STACK_NAME",
        "CLOUDFLARE_TUNNEL_NAME",
        "DOKPLOY_SUBDOMAIN",
        "CODER_SUBDOMAIN",
        "SEAWEEDFS_SUBDOMAIN",
        "LITELLM_ADMIN_SUBDOMAIN",
        "DOKPLOY_WIZARD_TASK1_PROOF_CONTEXT_ID",
        "DOKPLOY_WIZARD_TASK1_DISABLE_CODER_WILDCARD",
    }
)
PROOF_CONTROL_KEYS: Final = frozenset(
    {
        "DOKPLOY_WIZARD_TASK1_PROOF_CONTEXT_ID",
        "DOKPLOY_WIZARD_TASK1_DISABLE_CODER_WILDCARD",
    }
)
_CONTEXT_FIELDS: Final = (
    "context_id",
    "source_env_sha256",
    "normalized_env_sha256",
    "overlay_env_sha256",
    "uploaded_env_sha256",
    "namespace_sha256",
    "expected_restored_source_sha256",
    "source_env_mode",
    "root_domain",
    "stack_name",
    "tunnel_name",
    "dokploy_subdomain",
    "coder_subdomain",
    "seaweedfs_subdomain",
    "litellm_admin_subdomain",
)


class Task1ProofContextError(ValueError):
    """Raised when a Task 1 proof context is absent, malformed, or mismatched."""


@dataclass(frozen=True, slots=True)
class Task1ProofContextV1:
    """Value-free binding for one isolated local KVM proof namespace."""

    context_id: str
    source_env_sha256: str
    normalized_env_sha256: str
    overlay_env_sha256: str
    uploaded_env_sha256: str
    namespace_sha256: str
    expected_restored_source_sha256: str
    source_env_mode: int
    root_domain: str
    stack_name: str
    tunnel_name: str
    dokploy_subdomain: str
    coder_subdomain: str
    seaweedfs_subdomain: str
    litellm_admin_subdomain: str

    @property
    def litellm_admin_hostname(self) -> str:
        return f"{self.litellm_admin_subdomain}.{self.root_domain}"

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    def to_payload(self) -> dict[str, str | int]:
        return {"schema_version": 1, **{field: getattr(self, field) for field in _CONTEXT_FIELDS}}

    @classmethod
    def from_bytes(cls, content: bytes) -> "Task1ProofContextV1":
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Task1ProofContextError("Task 1 proof context is not valid JSON") from error
        expected = {"schema_version", *_CONTEXT_FIELDS}
        if not isinstance(value, dict) or set(value) != expected or value["schema_version"] != 1:
            raise Task1ProofContextError("Task 1 proof context schema is invalid")
        string_fields = set(_CONTEXT_FIELDS) - {"source_env_mode"}
        if any(not isinstance(value[field], str) for field in string_fields):
            raise Task1ProofContextError("Task 1 proof context fields are invalid")
        source_mode = value["source_env_mode"]
        if isinstance(source_mode, bool) or not isinstance(source_mode, int):
            raise Task1ProofContextError("Task 1 proof context fields are invalid")
        context = cls(
            **{field: value[field] for field in string_fields}, source_env_mode=source_mode
        )
        if content != context.to_bytes():
            raise Task1ProofContextError("Task 1 proof context bytes are not canonical")
        validate_context(context)
        return context


def canonical_json_bytes(
    value: Mapping[str, str | int] | Mapping[str, str | dict[str, str]],
) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def validate_context(context: Task1ProofContextV1) -> None:
    validate_token(context.context_id)
    digests = (
        context.source_env_sha256,
        context.normalized_env_sha256,
        context.overlay_env_sha256,
        context.uploaded_env_sha256,
        context.namespace_sha256,
        context.expected_restored_source_sha256,
    )
    if any(len(value) != 64 or set(value) - set("0123456789abcdef") for value in digests):
        raise Task1ProofContextError("Task 1 proof context hash is invalid")
    if context.source_env_sha256 != context.expected_restored_source_sha256:
        raise Task1ProofContextError("Task 1 proof source and restoration hashes differ")
    if isinstance(context.source_env_mode, bool) or not 1 <= context.source_env_mode <= 0o777:
        raise Task1ProofContextError("Task 1 proof context source mode is invalid")
    if not valid_root_domain(context.root_domain):
        raise Task1ProofContextError("Task 1 proof root domain is not canonical")
    if not valid_dns_label(context.stack_name) or not valid_dns_label(context.tunnel_name):
        raise Task1ProofContextError("Task 1 proof namespace name is invalid")
    labels = (
        context.dokploy_subdomain,
        context.coder_subdomain,
        context.seaweedfs_subdomain,
        context.litellm_admin_subdomain,
    )
    if not all(valid_dns_label(value) for value in labels):
        raise Task1ProofContextError("Task 1 proof hostname label is invalid")


def task1_env_bytes(values: Mapping[str, str]) -> bytes:
    return "".join(f"{key}={values[key]}\n" for key in sorted(values)).encode()


def task1_namespace(values: Mapping[str, str]) -> dict[str, str | dict[str, str]]:
    root_domain = values["ROOT_DOMAIN"]
    return {
        "stack_name": values["STACK_NAME"],
        "tunnel_name": values["CLOUDFLARE_TUNNEL_NAME"],
        "hostnames": {
            "coder": f"{values['CODER_SUBDOMAIN']}.{root_domain}",
            "dokploy": f"{values['DOKPLOY_SUBDOMAIN']}.{root_domain}",
            "litellm-admin": f"{values['LITELLM_ADMIN_SUBDOMAIN']}.{root_domain}",
            "seaweedfs": f"{values['SEAWEEDFS_SUBDOMAIN']}.{root_domain}",
        },
    }


def task1_receipt_payload(
    context: Task1ProofContextV1, source_path_sha256: str
) -> dict[str, str | int]:
    return {
        "context_sha256": sha256(context.to_bytes()),
        "expected_restored_source_sha256": context.expected_restored_source_sha256,
        "namespace_sha256": context.namespace_sha256,
        "source_env_mode": context.source_env_mode,
        "source_env_sha256": context.source_env_sha256,
        "source_path_sha256": source_path_sha256,
        "uploaded_env_sha256": context.uploaded_env_sha256,
    }


def validate_task1_uploaded_values(context: Task1ProofContextV1, values: Mapping[str, str]) -> None:
    expected_overlay = {
        "ROOT_DOMAIN": context.root_domain,
        "STACK_NAME": context.stack_name,
        "CLOUDFLARE_TUNNEL_NAME": context.tunnel_name,
        "DOKPLOY_SUBDOMAIN": context.dokploy_subdomain,
        "CODER_SUBDOMAIN": context.coder_subdomain,
        "SEAWEEDFS_SUBDOMAIN": context.seaweedfs_subdomain,
        "LITELLM_ADMIN_SUBDOMAIN": context.litellm_admin_subdomain,
        "DOKPLOY_WIZARD_TASK1_PROOF_CONTEXT_ID": context.context_id,
        "DOKPLOY_WIZARD_TASK1_DISABLE_CODER_WILDCARD": "true",
    }
    if any(values.get(key) != value for key, value in expected_overlay.items()):
        raise Task1ProofContextError("Task 1 proof context does not match upload values")
    overlay = {key: values[key] for key in TASK1_PROOF_OVERLAY_KEYS}
    normalized = {
        key: value for key, value in values.items() if key not in TASK1_PROOF_OVERLAY_KEYS
    }
    projections = (
        (task1_env_bytes(normalized), context.normalized_env_sha256),
        (task1_env_bytes(overlay), context.overlay_env_sha256),
        (task1_env_bytes(values), context.uploaded_env_sha256),
        (canonical_json_bytes(task1_namespace(values)), context.namespace_sha256),
    )
    if any(sha256(content) != expected for content, expected in projections):
        raise Task1ProofContextError("Task 1 proof upload projection hash drifted")


def validate_token(token: str) -> None:
    if len(token) != 32 or set(token) - set("0123456789abcdef"):
        raise Task1ProofContextError("Task 1 proof attempt token must be 128-bit lowercase hex")


def valid_dns_label(value: str) -> bool:
    return (
        1 <= len(value) <= 63
        and value[0].isalnum()
        and value[-1].isalnum()
        and all(
            character.isdigit() or "a" <= character <= "z" or character == "-"
            for character in value
        )
    )


def valid_root_domain(value: str) -> bool:
    labels = value.split(".")
    return (
        value == value.lower()
        and 1 < len(value) <= 253
        and len(labels) >= 2
        and all(valid_dns_label(label) for label in labels)
    )
