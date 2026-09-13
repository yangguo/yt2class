"""Secure, run-scoped filesystem access for ingestion artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Self


class WorkspaceError(RuntimeError):
    """Base error for run workspace failures."""


class WorkspaceBusy(WorkspaceError):
    """Raised when another process owns the run write lock."""


class WorkspacePathError(WorkspaceError):
    """Raised when a path would escape a run workspace."""


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass
class _WriteLock:
    """Exclusive lock represented by an atomically-created marker file."""

    path: Path
    _fd: int | None = None

    def __enter__(self) -> Self:
        try:
            self._fd = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as error:
            raise WorkspaceBusy(f"workspace is already locked: {self.path}") from error
        except OSError as error:
            raise WorkspaceError(f"cannot create workspace lock: {self.path}") from error
        try:
            os.write(self._fd, f"pid={os.getpid()}\n".encode("ascii"))
        except OSError:
            os.close(self._fd)
            self._fd = None
            self.path.unlink(missing_ok=True)
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._fd is None:
            return
        os.close(self._fd)
        self._fd = None
        self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class Workspace:
    """A resolved run root with confined artifact subdirectories."""

    root: Path
    media_dir: Path
    metadata_dir: Path
    tmp_dir: Path
    lock_path: Path

    @classmethod
    def create(cls, run_root: Path, *, run_id: str) -> Self:
        base = Path(run_root).expanduser()
        base.mkdir(parents=True, exist_ok=True)
        try:
            base = base.resolve(strict=True)
        except OSError as error:
            raise WorkspacePathError(f"cannot resolve run root: {run_root}") from error
        if not _RUN_ID_PATTERN.fullmatch(run_id):
            raise WorkspacePathError("run_id must be a path-safe identifier")
        root = (base / run_id).resolve(strict=False)
        if not root.is_relative_to(base):
            raise WorkspacePathError("run workspace resolves outside run root")
        root.mkdir(parents=True, exist_ok=True)
        # Resolve after creation so a pre-existing run symlink cannot redirect artifacts.
        root = root.resolve(strict=True)
        if not root.is_relative_to(base):
            raise WorkspacePathError("run workspace resolves outside run root")
        media_dir = root / "media"
        metadata_dir = root / "metadata"
        tmp_dir = root / "tmp"
        for directory in (media_dir, metadata_dir, tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return cls(
            root=root,
            media_dir=media_dir,
            metadata_dir=metadata_dir,
            tmp_dir=tmp_dir,
            lock_path=root / ".write.lock",
        )

    def safe_path(self, relative_path: str | Path, *, create_parent: bool = False) -> Path:
        """Return a path whose resolved location remains below the run root."""

        relative = Path(relative_path)
        if relative.is_absolute() or any(part in {"", "."} for part in relative.parts):
            if relative.is_absolute() or relative == Path("."):
                raise WorkspacePathError(f"path must be relative to workspace: {relative_path}")
        if ".." in relative.parts:
            raise WorkspacePathError(f"path traversal is not allowed: {relative_path}")
        candidate = self.root / relative
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise WorkspacePathError(f"path resolves outside workspace: {relative_path}")
        if create_parent:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            # Re-check after creating directories in case a concurrent actor replaced one.
            resolved = candidate.resolve(strict=False)
            if not resolved.is_relative_to(self.root):
                raise WorkspacePathError(f"path resolves outside workspace: {relative_path}")
        return candidate

    def write_lock(self) -> _WriteLock:
        """Return a context manager for the run's single-writer lock."""

        return _WriteLock(self.lock_path)


__all__ = ["Workspace", "WorkspaceBusy", "WorkspaceError", "WorkspacePathError"]
