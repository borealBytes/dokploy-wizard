from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final

from dokploy_wizard.proof.model_sync_identity import RemoteProbe, RemoteProofError

_ATTEMPTS: Final = 6
_DELAY_SECONDS: Final = 10.0


def capture_post_install_probe(
    capture: Callable[[], RemoteProbe],
    sleep: Callable[[float], None] = time.sleep,
) -> RemoteProbe:
    for attempt in range(_ATTEMPTS):
        try:
            return capture()
        except RemoteProofError:
            if attempt == _ATTEMPTS - 1:
                raise
            sleep(_DELAY_SECONDS)
    raise AssertionError("post-install probe attempts were exhausted")
