"""Secure, run-scoped filesystem access for ingestion artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import re
import stat
from typing import Self


class WorkspaceError(RuntimeError):
    """Base error for run workspace failures."""


class WorkspaceBusy(WorkspaceError):
    """Raised when another process owns the run write lock."""


class WorkspacePathError(WorkspaceError):
    """Raised when a path would escape a run workspace."""


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


_HELD_LOCKS: set[str] = set()


def _close_lock_fd(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _require_safe_lock_inode(fd: int, path: Path) -> None:
    """Reject symlink targets, non-files, and extra hard links before writing."""

    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise WorkspacePathError(f"workspace lock must be a regular file: {path}")
    if info.st_nlink != 1:
        raise WorkspacePathError(f"workspace lock must not be hard-linked: {path}")


@dataclass
class _WriteLock:
    """Exclusive run lock via flock. The lock file is kept; release is flock close."""

    path: Path
    _fd: int | None = None
    _key: str | None = None

    def __enter__(self) -> Self:
        key = str(self.path)
        if key in _HELD_LOCKS:
            raise WorkspaceBusy(f"workspace is already locked: {self.path}")
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        try:
            self._fd = os.open(self.path, flags, 0o600)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise WorkspacePathError(
                    f"workspace lock must not be a symlink: {self.path}"
                ) from error
            if error.errno == errno.EISDIR:
                raise WorkspacePathError(
                    f"workspace lock must be a regular file: {self.path}"
                ) from error
            raise WorkspaceError(f"cannot create workspace lock: {self.path}") from error
        try:
            _require_safe_lock_inode(self._fd, self.path)
        except (OSError, WorkspacePathError) as error:
            _close_lock_fd(self._fd)
            self._fd = None
            if isinstance(error, WorkspacePathError):
                raise
            raise WorkspaceError(f"cannot inspect workspace lock: {self.path}") from error
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            _close_lock_fd(self._fd)
            self._fd = None
            raise WorkspaceBusy(f"workspace is already locked: {self.path}") from error
        except OSError as error:
            _close_lock_fd(self._fd)
            self._fd = None
            raise WorkspaceError(f"cannot lock workspace: {self.path}") from error
        try:
            _require_safe_lock_inode(self._fd, self.path)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.ftruncate(self._fd, 0)
            os.write(self._fd, f"pid={os.getpid()}\n".encode("ascii"))
            os.fsync(self._fd)
        except WorkspacePathError:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            _close_lock_fd(self._fd)
            self._fd = None
            raise
        except OSError:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            _close_lock_fd(self._fd)
            self._fd = None
            raise
        self._key = key
        _HELD_LOCKS.add(key)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._key is not None:
            _HELD_LOCKS.discard(self._key)
            self._key = None
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None


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
