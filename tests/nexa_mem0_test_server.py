from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator


@dataclass(frozen=True)
class RecordedMem0Request:
    method: str
    path: str
    headers: dict[str, str]
    body: dict[str, Any]


class RecordingMem0Server(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        search_results: list[dict[str, Any]] | None,
        failure_paths: dict[str, tuple[int, dict[str, Any]]] | None,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.requests: list[RecordedMem0Request] = []
        self.search_results = search_results or []
        self.failure_paths = failure_paths or {}
        self.memory_counter = 0


class _Mem0Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        server = self.server
        assert isinstance(server, RecordingMem0Server)
        server.requests.append(
            RecordedMem0Request(
                method="POST",
                path=self.path,
                headers={key: value for key, value in self.headers.items()},
                body=body,
            )
        )
        if self.path in server.failure_paths:
            status_code, payload = server.failure_paths[self.path]
            self._write_json(status_code, payload)
            return
        if self.path == "/configure":
            self._write_json(200, {"configured": True})
            return
        if self.path == "/search":
            self._write_json(200, {"results": server.search_results})
            return
        if self.path == "/memories":
            server.memory_counter += 1
            self._write_json(201, {"id": f"mem-{server.memory_counter}"})
            return
        self._write_json(404, {"error": "unknown_path"})

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
def run_recording_mem0_server(
    *,
    search_results: list[dict[str, Any]] | None = None,
    failure_paths: dict[str, tuple[int, dict[str, Any]]] | None = None,
) -> Iterator[RecordingMem0Server]:
    server = RecordingMem0Server(
        ("127.0.0.1", 0),
        _Mem0Handler,
        search_results=search_results,
        failure_paths=failure_paths,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def mem0_base_url(server: RecordingMem0Server) -> str:
    return f"http://127.0.0.1:{server.server_port}"
