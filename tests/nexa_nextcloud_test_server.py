from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator


@dataclass(frozen=True)
class RecordedNextcloudRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class RecordingNextcloudServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        talk_shared_secret: str,
        talk_signing_secret: str,
        webdav_user: str,
        webdav_password: str,
        webdav_file_id: str,
        webdav_etag: str,
        webdav_content: bytes,
        webdav_content_type: str,
        webdav_acl_principals: tuple[str, ...] = (),
    ) -> None:
        super().__init__(server_address, handler_class)
        self.requests: list[RecordedNextcloudRequest] = []
        self.talk_shared_secret = talk_shared_secret
        self.talk_signing_secret = talk_signing_secret
        self.webdav_user = webdav_user
        self.webdav_password = webdav_password
        self.webdav_file_id = webdav_file_id
        self.webdav_etag = webdav_etag
        self.webdav_content = webdav_content
        self.webdav_content_type = webdav_content_type
        self.webdav_acl_principals = webdav_acl_principals


class _NextcloudHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        server = self.server
        assert isinstance(server, RecordingNextcloudServer)
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        server.requests.append(
            RecordedNextcloudRequest(
                method="POST",
                path=self.path,
                headers={key: value for key, value in self.headers.items()},
                body=body,
            )
        )
        if self.path.startswith("/ocs/v2.php/apps/spreed/api/v1/bot/"):
            payload = json.loads(body.decode("utf-8"))
            random_header = self.headers.get("X-Nextcloud-Talk-Bot-Random", "")
            signature = self.headers.get("X-Nextcloud-Talk-Bot-Signature", "")
            expected = hmac.new(
                server.talk_signing_secret.encode("utf-8"),
                f"{random_header}{payload['message']}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if self.headers.get("X-Nextcloud-Talk-Secret") != server.talk_shared_secret or signature != expected:
                self._write_json(401, {"ocs": {"meta": {"status": "failure"}}})
                return
            self._write_json(
                201,
                {
                    "ocs": {
                        "meta": {"status": "ok", "requestid": "request-42"},
                        "data": {"id": 901},
                    }
                },
            )
            return
        self._write_json(404, {"error": "unknown_path"})

    def do_PROPFIND(self) -> None:  # noqa: N802
        self._handle_webdav()

    def do_GET(self) -> None:  # noqa: N802
        self._handle_webdav()

    def _handle_webdav(self) -> None:
        server = self.server
        assert isinstance(server, RecordingNextcloudServer)
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        server.requests.append(
            RecordedNextcloudRequest(
                method=self.command,
                path=self.path,
                headers={key: value for key, value in self.headers.items()},
                body=body,
            )
        )
        expected_auth = "Basic " + base64.b64encode(
            f"{server.webdav_user}:{server.webdav_password}".encode("utf-8")
        ).decode("ascii")
        if self.headers.get("Authorization") != expected_auth:
            self.send_response(401)
            self.end_headers()
            return
        if self.command == "PROPFIND":
            acl_xml = "".join(
                f"<nc:acl><nc:acl-mapping-id>{principal}</nc:acl-mapping-id></nc:acl>"
                for principal in server.webdav_acl_principals
            )
            payload = (
                "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
                "<d:multistatus xmlns:d=\"DAV:\" xmlns:oc=\"http://owncloud.org/ns\" xmlns:nc=\"http://nextcloud.org/ns\">"
                "<d:response><d:propstat><d:prop>"
                f"<d:getetag>{server.webdav_etag}</d:getetag>"
                f"<oc:fileid>{server.webdav_file_id}</oc:fileid>"
                f"{acl_xml}"
                "</d:prop></d:propstat></d:response>"
                "</d:multistatus>"
            ).encode("utf-8")
            self.send_response(207)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.command == "GET":
            self.send_response(200)
            self.send_header("Content-Type", server.webdav_content_type)
            self.send_header("Content-Length", str(len(server.webdav_content)))
            self.end_headers()
            self.wfile.write(server.webdav_content)
            return
        self.send_response(405)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def _write_json(self, status_code: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@contextmanager
def run_recording_nextcloud_server(
    *,
    talk_shared_secret: str,
    talk_signing_secret: str,
    webdav_user: str,
    webdav_password: str,
    webdav_file_id: str,
    webdav_etag: str,
    webdav_content: bytes,
    webdav_content_type: str,
    webdav_acl_principals: tuple[str, ...] = (),
) -> Iterator[RecordingNextcloudServer]:
    server = RecordingNextcloudServer(
        ("127.0.0.1", 0),
        _NextcloudHandler,
        talk_shared_secret=talk_shared_secret,
        talk_signing_secret=talk_signing_secret,
        webdav_user=webdav_user,
        webdav_password=webdav_password,
        webdav_file_id=webdav_file_id,
        webdav_etag=webdav_etag,
        webdav_content=webdav_content,
        webdav_content_type=webdav_content_type,
        webdav_acl_principals=webdav_acl_principals,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def nextcloud_base_url(server: RecordingNextcloudServer) -> str:
    return f"http://127.0.0.1:{server.server_port}"
