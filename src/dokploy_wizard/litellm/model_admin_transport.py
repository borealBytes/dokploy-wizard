from __future__ import annotations

import json
from email.message import Message
from typing import IO
from urllib import request

from dokploy_wizard.litellm.catalog_json import JsonValue


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        new_url: str,
    ) -> request.Request | None:
        del req, fp, code, msg, headers, new_url
        return None


def default_model_admin_request(raw_request: request.Request) -> JsonValue:
    opener = request.build_opener(_NoRedirect())
    with opener.open(raw_request, timeout=30) as response:
        value: JsonValue = json.loads(response.read().decode("utf-8"))
        return value
