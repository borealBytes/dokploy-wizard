from __future__ import annotations


def loopback_access_url(port: int) -> str:
    if port not in range(1, 65536):
        raise ValueError("remote Coder port must be a valid TCP port")
    return f"http://127.0.0.1:{port}"
