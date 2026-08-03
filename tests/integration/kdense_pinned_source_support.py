from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.request import urlopen

from dokploy_wizard.dokploy.coder_runtime_asset_kdense_types import KdenseRuntimeContract

_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


def download_pinned_kdense_archive(destination: Path, contract: KdenseRuntimeContract) -> Path:
    """Download the immutable public codeload archive with bounded streaming output."""

    written = 0
    digest = hashlib.sha256()
    with urlopen(contract.codeload_url, timeout=30) as response, destination.open("xb") as output:  # noqa: S310
        while chunk := response.read(1024 * 1024):
            written += len(chunk)
            if written > _MAX_ARCHIVE_BYTES:
                raise ValueError("K-Dense archive exceeds the test bound")
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != contract.archive_sha256:
        raise ValueError("K-Dense archive digest is invalid")
    return destination
