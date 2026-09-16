"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import RuntimeConfig, initialize_data_dir, project_root
from .errors import RuntimeProtocolError
from .protocol import RuntimeProtocol
from .server import serve
from .storage import Storage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="adaptive-runtime", description="Adaptive Task Protocol Runtime")
    parser.add_argument("--data-dir", type=Path, help="Override the Runtime data directory")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("init", help="Initialize local storage and create an authentication token")
    subparsers.add_parser("serve", help="Start the authenticated loopback HTTP service")
    subparsers.add_parser("check", help="Validate configuration and initialize the database without serving")
    return parser


def _config(args: argparse.Namespace, *, require_token: bool) -> RuntimeConfig:
    config = RuntimeConfig.from_env(require_token=False)
    if args.data_dir:
        config = RuntimeConfig(
            host=config.host,
            port=config.port,
            data_dir=args.data_dir.expanduser().resolve(),
            log_level=config.log_level,
            auth_token="",
            max_body_bytes=config.max_body_bytes,
        )
    if require_token:
        token = config.auth_token
        if not token and config.token_path.is_file():
            token = config.token_path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise RuntimeProtocolError("AUTH_TOKEN_MISSING", "Run `adaptive-runtime init` first.", 500)
        config = RuntimeConfig(config.host, config.port, config.data_dir, config.log_level, token, config.max_body_bytes)
    return config


def _services(config: RuntimeConfig) -> tuple[Storage, RuntimeProtocol]:
    storage = Storage(config.database_path, config.data_dir)
    storage.initialize()
    return storage, RuntimeProtocol(storage)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        config = _config(args, require_token=args.command == "serve")
        logging.basicConfig(
            level=getattr(logging, config.log_level, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        if args.command == "init":
            token_path, created = initialize_data_dir(config.data_dir)
            initialized = _config(args, require_token=True)
            _services(initialized)
            print(f"Runtime data initialized at {initialized.data_dir}")
            print(f"Authentication token {'created' if created else 'already exists'} at {token_path}")
            return 0
        if args.command == "check":
            initialize_data_dir(config.data_dir)
            checked = _config(args, require_token=True)
            _services(checked)
            print(f"Runtime configuration is valid: {checked.host}:{checked.port}")
            return 0
        _, protocol = _services(config)
        serve(config, protocol)
        return 0
    except RuntimeProtocolError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
