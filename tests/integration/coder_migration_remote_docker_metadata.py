from __future__ import annotations

import os
import stat

from tests.integration.coder_migration_remote_protocol import RemoteCoderProtocolError


def parse_loopback_port(output: bytes) -> int:
    try:
        value = output.decode("ascii")
    except UnicodeDecodeError as error:
        raise RemoteCoderProtocolError("remote Coder published port is not ASCII") from error
    prefix = "127.0.0.1:"
    if not value.startswith(prefix) or not value.endswith("\n"):
        raise RemoteCoderProtocolError("remote Coder published port is not loopback-only")
    port_text = value.removeprefix(prefix).removesuffix("\n")
    if not port_text.isdecimal():
        raise RemoteCoderProtocolError("remote Coder published port is invalid")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise RemoteCoderProtocolError("remote Coder published port is out of range")
    return port


def socket_group_id() -> int:
    try:
        metadata = os.stat("/var/run/docker.sock")
    except OSError as error:
        raise RemoteCoderProtocolError("remote docker-socket-metadata") from error
    if not stat.S_ISSOCK(metadata.st_mode):
        raise RemoteCoderProtocolError("remote docker-socket-type")
    group_id = metadata.st_gid
    if group_id < 0:
        raise RemoteCoderProtocolError("remote Docker socket group is invalid")
    return group_id


__all__ = ["parse_loopback_port", "socket_group_id"]
