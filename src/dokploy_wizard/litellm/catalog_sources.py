from __future__ import annotations

from http.client import HTTPMessage
from typing import IO
from urllib import error, request

from dokploy_wizard.litellm.catalog_json import sha256_bytes
from dokploy_wizard.litellm.catalog_source_parsers import (
    parse_models_dev as parse_models_dev,
)
from dokploy_wizard.litellm.catalog_source_parsers import (
    parse_official as parse_official,
)
from dokploy_wizard.litellm.catalog_source_parsers import (
    parse_zen as parse_zen,
)
from dokploy_wizard.litellm.catalog_types import (
    FetchDocument as FetchDocument,
)
from dokploy_wizard.litellm.catalog_types import (
    FetchRequest as FetchRequest,
)
from dokploy_wizard.litellm.catalog_types import (
    FetchResponse as FetchResponse,
)
from dokploy_wizard.litellm.catalog_types import (
    SourceContractError as SourceContractError,
)
from dokploy_wizard.litellm.catalog_types import (
    SourceSpec,
    SourceTransport,
)

ZEN_SOURCE = SourceSpec(
    "zen",
    "https://opencode.ai/zen/go/v1/models",
    2 * 1024 * 1024,
    ("application/json",),
)
MODELS_DEV_SOURCE = SourceSpec(
    "models_dev",
    "https://models.dev/api.json",
    16 * 1024 * 1024,
    ("application/json",),
)
OFFICIAL_SOURCE = SourceSpec(
    "official",
    "https://raw.githubusercontent.com/anomalyco/opencode/"
    "0df2f6245a9cd966c0912e12db2c9d809e0c589f/packages/web/src/content/docs/go.mdx",
    512 * 1024,
    ("application/json", "text/plain"),
    "20a8a45e3fb78c93a7f143a9a4c0ec8b011209065e215d24a68d298f313f96d3",
    "0df2f6245a9cd966c0912e12db2c9d809e0c589f",
    "9a810171fe5c6a1dee905df43bbf326c82648cd2",
)
_HEADERS = (("Accept", "application/json"), ("User-Agent", "dokploy-wizard-model-sync/1"))


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> request.Request | None:
        return None


class UrllibSourceTransport:
    def execute(self, request_spec: FetchRequest) -> FetchResponse:
        opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        outgoing = request.Request(
            request_spec.url,
            headers=dict(request_spec.headers),
            method=request_spec.method,
        )
        try:
            with opener.open(outgoing, timeout=request_spec.timeout_seconds) as response:
                return FetchResponse(
                    response.status,
                    response.headers.get_content_type(),
                    _read_chunks(response, request_spec.max_bytes),
                )
        except error.HTTPError as response:
            return FetchResponse(
                response.code,
                response.headers.get_content_type(),
                _read_chunks(response, request_spec.max_bytes),
            )
        except (OSError, TimeoutError) as exc:
            raise SourceContractError("source transport failed") from exc


def fetch_source(spec: SourceSpec, transport: SourceTransport) -> FetchDocument:
    response = transport.execute(
        FetchRequest("GET", spec.url, _HEADERS, 30.0, False, spec.max_bytes)
    )
    if response.status != 200:
        raise SourceContractError(f"source status is {response.status}")
    content_type = response.content_type.partition(";")[0].strip().lower()
    if content_type not in spec.accepted_content_types:
        raise SourceContractError("source content_type is invalid")
    body = bytearray()
    for chunk in response.chunks:
        body.extend(chunk)
        if len(body) > spec.max_bytes:
            raise SourceContractError("source size exceeds limit")
    raw = bytes(body)
    if spec.expected_sha256 is not None and sha256_bytes(raw) != spec.expected_sha256:
        raise SourceContractError("source hash does not match pin")
    return FetchDocument(spec, raw, content_type)


def _read_chunks(response: IO[bytes], maximum: int) -> tuple[bytes, ...]:
    chunks: list[bytes] = []
    total = 0
    while total <= maximum:
        chunk = response.read(min(64 * 1024, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return tuple(chunks)
