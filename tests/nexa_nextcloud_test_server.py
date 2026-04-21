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
        talk_auth_user: str | None = None,
        talk_auth_password: str | None = None,
        talk_conversations: tuple[dict[str, Any], ...] = (),
        talk_messages_by_token: dict[str, tuple[dict[str, Any], ...]] | None = None,
        public_share_base_url: str = "https://nextcloud.example.com/s/",
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
        self.talk_auth_user = talk_auth_user or webdav_user
        self.talk_auth_password = talk_auth_password or webdav_password
        self.talk_conversations = talk_conversations
        self.talk_messages_by_token = talk_messages_by_token or {}
        self.public_share_base_url = public_share_base_url
        self.webdav_files: dict[str, bytes] = {}


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
        if self.path.startswith("/ocs/v2.php/apps/spreed/api/v1/chat/"):
            if not self._check_talk_auth():
                return
            self._write_json(
                201,
                {
                    "ocs": {
                        "meta": {"status": "ok", "requestid": "request-42"},
                        "data": {"id": 902},
                    }
                },
            )
            return
        if self.path == "/ocs/v2.php/apps/files_sharing/api/v1/shares":
            if not self._check_talk_auth():
                return
            share_request = json.loads(body.decode("utf-8"))
            relative_path = str(share_request.get("path", "")).strip()
            token = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:12]
            self._write_json(
                200,
                {
                    "ocs": {
                        "meta": {"status": "ok", "requestid": "share-request-1"},
                        "data": {"id": 701, "token": token, "url": server.public_share_base_url + token},
                    }
                },
            )
            return
        self._write_json(404, {"error": "unknown_path"})

    def do_PROPFIND(self) -> None:  # noqa: N802
        self._handle_webdav()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_webdav()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/ocs/v2.php/apps/spreed/api/v4/room"):
            if not self._check_talk_auth():
                return
            self._write_json(200, {"ocs": {"meta": {"status": "ok"}, "data": list(self.server.talk_conversations)}})
            return
        if self.path.startswith("/ocs/v2.php/apps/spreed/api/v1/chat/"):
            if not self._check_talk_auth():
                return
            token = self.path.split("/chat/", 1)[1].split("?", 1)[0]
            messages = list(self.server.talk_messages_by_token.get(token, ()))
            self._write_json(200, {"ocs": {"meta": {"status": "ok"}, "data": messages}})
            return
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
            stored_content = server.webdav_files.get(self.path)
            if stored_content is not None:
                self.send_response(200)
                self.send_header("Content-Type", server.webdav_content_type)
                self.send_header("Content-Length", str(len(stored_content)))
                self.end_headers()
                self.wfile.write(stored_content)
                return
            self.send_response(200)
            self.send_header("Content-Type", server.webdav_content_type)
            self.send_header("Content-Length", str(len(server.webdav_content)))
            self.end_headers()
            self.wfile.write(server.webdav_content)
            return
        if self.command == "PUT":
            server.webdav_files[self.path] = body
            self.send_response(201)
            self.end_headers()
            return
        self.send_response(405)
        self.end_headers()

    def _check_talk_auth(self) -> bool:
        server = self.server
        assert isinstance(server, RecordingNextcloudServer)
        expected_auth = "Basic " + base64.b64encode(
            f"{server.talk_auth_user}:{server.talk_auth_password}".encode("utf-8")
        ).decode("ascii")
        if self.headers.get("Authorization") != expected_auth:
            self.send_response(401)
            self.end_headers()
            return False
        return True

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
    talk_auth_user: str | None = None,
    talk_auth_password: str | None = None,
    talk_conversations: tuple[dict[str, Any], ...] = (),
    talk_messages_by_token: dict[str, tuple[dict[str, Any], ...]] | None = None,
    public_share_base_url: str = "https://nextcloud.example.com/s/",
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
        talk_auth_user=talk_auth_user,
        talk_auth_password=talk_auth_password,
        talk_conversations=talk_conversations,
        talk_messages_by_token=talk_messages_by_token,
        public_share_base_url=public_share_base_url,
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
