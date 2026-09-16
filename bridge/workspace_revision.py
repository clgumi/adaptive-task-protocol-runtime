"""Cheap content-change token computed by the executing Hermes Bridge."""

from __future__ import annotations

import hashlib
from pathlib import Path


EXCLUDED_DIRECTORIES = {
    ".git", ".hg", ".svn", ".venv", "__pycache__", "node_modules",
    "dist", "build", "coverage", ".pytest_cache", "runtime-data",
    ".doctor-home", ".pip-cache-test", ".runtime-test", ".tmp-build",
    ".wheel-test", "backups", "logs",
}


def compute_workspace_revision(workspace: str | Path) -> str:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace does not exist: {root}")
    digest = hashlib.sha256()
    files = sorted(
        (path for path in root.rglob("*") if path.is_file() and not any(part in EXCLUDED_DIRECTORIES for part in path.relative_to(root).parts)),
        key=lambda path: path.as_posix().lower(),
    )
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()
