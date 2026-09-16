"""Runtime configuration, initialization, and local authentication."""

from __future__ import annotations

import os
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path

from .errors import RuntimeProtocolError


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    host: str = "127.0.0.1"
    port: int = 8790
    data_dir: Path = project_root() / "runtime-data"
    log_level: str = "INFO"
    auth_token: str = ""
    max_body_bytes: int = 2 * 1024 * 1024

    @classmethod
    def from_env(cls, *, require_token: bool = True) -> "RuntimeConfig":
        host = os.getenv("ADAPTIVE_RUNTIME_HOST", "127.0.0.1").strip()
        if host not in {"127.0.0.1", "localhost"}:
            raise RuntimeProtocolError(
                "UNSAFE_BIND_ADDRESS",
                "Runtime may only bind to a loopback address.",
                500,
                {"host": host},
            )
        try:
            port = int(os.getenv("ADAPTIVE_RUNTIME_PORT", "8790"))
        except ValueError as exc:
            raise RuntimeProtocolError("INVALID_CONFIG", "ADAPTIVE_RUNTIME_PORT must be an integer.", 500) from exc
        if not 1 <= port <= 65535:
            raise RuntimeProtocolError("INVALID_CONFIG", "Runtime port must be between 1 and 65535.", 500)
        data_dir = Path(os.getenv("ADAPTIVE_RUNTIME_DATA_DIR", str(project_root() / "runtime-data"))).expanduser().resolve()
        token = os.getenv("ADAPTIVE_RUNTIME_TOKEN", "").strip()
        if not token:
            token_path = data_dir / "auth-token"
            if token_path.is_file():
                token = token_path.read_text(encoding="utf-8").strip()
        if require_token and len(token) < 32:
            raise RuntimeProtocolError(
                "AUTH_TOKEN_MISSING",
                "Initialize the Runtime or provide ADAPTIVE_RUNTIME_TOKEN (minimum 32 characters).",
                500,
            )
        return cls(
            host=host,
            port=port,
            data_dir=data_dir,
            log_level=os.getenv("ADAPTIVE_RUNTIME_LOG_LEVEL", "INFO").upper(),
            auth_token=token,
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "runtime.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "auth-token"


def initialize_data_dir(data_dir: str | Path) -> tuple[Path, bool]:
    directory = Path(data_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tasks").mkdir(exist_ok=True)
    token_path = directory / "auth-token"
    created = False
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        created = True
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
    elif len(token_path.read_text(encoding="utf-8").strip()) < 32:
        raise RuntimeProtocolError("INVALID_CONFIG", "Existing auth-token is too short.", 500)
    return token_path, created


def assert_port_available(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.5)
        sock.bind((host, port))
    except OSError as exc:
        raise RuntimeProtocolError(
            "PORT_UNAVAILABLE",
            f"Cannot bind Runtime to {host}:{port}.",
            500,
            {"reason": str(exc)},
        ) from exc
    finally:
        sock.close()
