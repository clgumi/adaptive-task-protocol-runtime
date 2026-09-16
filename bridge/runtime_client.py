"""Dependency-free authenticated client for the local Runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


class RuntimeUnavailable(RuntimeError):
    pass


class RuntimeRejected(RuntimeError):
    def __init__(self, status: int, payload: dict[str, Any]):
        self.status = status
        self.payload = payload
        message = payload.get("error", {}).get("message") if isinstance(payload.get("error"), dict) else None
        super().__init__(message or f"Runtime rejected request with HTTP {status}.")


class RuntimeClient:
    def __init__(self, base_url: str, *, token: str = "", token_file: str | Path | None = None, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/") + "/"
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Adaptive Runtime URL must be loopback HTTP.")
        self._token = token.strip()
        self.token_file = Path(token_file).expanduser() if token_file else None
        self.timeout = max(0.25, min(float(timeout), 30.0))

    def _auth_token(self) -> str:
        token = os.getenv("ADAPTIVE_RUNTIME_TOKEN", "").strip() or self._token
        if not token and self.token_file and self.token_file.is_file():
            token = self.token_file.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise RuntimeUnavailable("Runtime bearer token is unavailable or too short.")
        return token

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None, *, authenticated: bool = True) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers["Authorization"] = f"Bearer {self._auth_token()}"
        request = Request(urljoin(self.base_url, path.lstrip("/")), data=data, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                payload = {"error": {"code": "HTTP_ERROR", "message": str(exc)}}
            raise RuntimeRejected(exc.code, payload) from exc
        except (URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeUnavailable(f"Adaptive Runtime is unavailable: {exc}") from exc

    def health(self) -> dict[str, Any]:
        return self.request("GET", "/health", authenticated=False)

    def create_task(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/v1/tasks", payload)

    def snapshot(self, task_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/tasks/{task_id}")

    def action(self, task_id: str, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/v1/tasks/{task_id}/{action}", payload)

