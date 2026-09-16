"""Authenticated loopback HTTP server for the Runtime protocol."""

from __future__ import annotations

import hmac
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import RuntimeConfig
from .errors import RuntimeProtocolError
from .protocol import RuntimeProtocol

logger = logging.getLogger(__name__)


class RuntimeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, config: RuntimeConfig, protocol: RuntimeProtocol):
        self.config = config
        self.protocol = protocol
        try:
            super().__init__((config.host, config.port), RuntimeRequestHandler)
        except OSError as exc:
            raise RuntimeProtocolError(
                "PORT_UNAVAILABLE",
                f"Cannot bind Runtime to {config.host}:{config.port}.",
                500,
                {"reason": str(exc)},
            ) from exc


class RuntimeRequestHandler(BaseHTTPRequestHandler):
    server: RuntimeHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def _handle(self) -> None:
        try:
            if self.path.rstrip("/") != "/health":
                self._authenticate()
            body = self._read_json_body() if self.command in {"POST", "PUT", "PATCH"} else None
            status, payload = self.server.protocol.dispatch(self.command, self.path, body)
            self._send_json(status, payload)
        except RuntimeProtocolError as exc:
            self._send_json(exc.status_code, exc.to_dict())
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.exception("Unhandled Runtime request failure")
            self._send_json(500, {"error": {"code": "INTERNAL_ERROR", "message": "Runtime request failed."}})

    def _authenticate(self) -> None:
        authorization = self.headers.get("Authorization", "")
        supplied = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, self.server.config.auth_token):
            raise RuntimeProtocolError("UNAUTHORIZED", "A valid bearer token is required.", 401)

    def _read_json_body(self) -> Any:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise RuntimeProtocolError("INVALID_REQUEST", "Content-Length must be an integer.", 400) from exc
        if length <= 0:
            return {}
        if length > self.server.config.max_body_bytes:
            raise RuntimeProtocolError("REQUEST_TOO_LARGE", "Request body exceeds the configured limit.", 413)
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeProtocolError("INVALID_JSON", "Request body must be valid UTF-8 JSON.", 400) from exc

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", 'Bearer realm="adaptive-runtime"')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - %s", self.client_address[0], format % args)


def serve(config: RuntimeConfig, protocol: RuntimeProtocol) -> None:
    server = RuntimeHTTPServer(config, protocol)
    logger.info("Adaptive Runtime ready on http://%s:%s", config.host, config.port)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
