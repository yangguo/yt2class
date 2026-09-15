"""Secure, run-scoped filesystem access for ingestion artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import re
import stat
from typing import Self

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows and other non-POSIX hosts
    fcntl = None  # type: ignore[assignment,misc]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX hosts
    msvcrt = None  # type: ignore[assignment,misc]


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


def _lock_open_flags(*, exclusive_create: bool) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW
    if exclusive_create:
        flags |= os.O_CREAT | os.O_EXCL
    else:
        flags |= os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _read_lock_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="ascii")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("pid="):
            try:
                return int(line.split("=", 1)[1].strip())
            except ValueError:
                return None
    return None


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _open_lock_file(path: Path, *, exclusive_create: bool) -> int:
    flags = _lock_open_flags(exclusive_create=exclusive_create)
    try:
        return os.open(path, flags, 0o600)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise WorkspacePathError(f"workspace lock must not be a symlink: {path}") from error
        if error.errno == errno.EISDIR:
            raise WorkspacePathError(f"workspace lock must be a regular file: {path}") from error
        if exclusive_create and error.errno in {errno.EEXIST, errno.EACCES}:
            raise WorkspaceBusy(f"workspace is already locked: {path}") from error
        raise WorkspaceError(f"cannot create workspace lock: {path}") from error


def _try_reclaim_stale_lock(path: Path) -> bool:
    if not path.is_file():
        return False
    pid = _read_lock_pid(path)
    if pid is not None and _process_alive(pid):
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _acquire_exclusive_lock(fd: int, path: Path) -> None:
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WorkspaceBusy(f"workspace is already locked: {path}") from error
        return
    if msvcrt is not None:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise WorkspaceBusy(f"workspace is already locked: {path}") from error
        return
    # O_EXCL create path: holding the fd is sufficient.
    return


def _release_exclusive_lock(fd: int) -> None:
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return
    if msvcrt is not None:
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


def _write_lock_payload(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
    os.fsync(fd)


@dataclass
class _WriteLock:
    """Exclusive run lock. The lock file is kept; release is unlock + close."""

    path: Path
    _fd: int | None = None
    _key: str | None = None
    _exclusive_file: bool = False

    def __enter__(self) -> Self:
        key = str(self.path)
        if key in _HELD_LOCKS:
            raise WorkspaceBusy(f"workspace is already locked: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        use_exclusive_create = fcntl is None and msvcrt is None
        self._exclusive_file = use_exclusive_create
        for attempt in range(3):
            try:
                self._fd = _open_lock_file(self.path, exclusive_create=use_exclusive_create)
                break
            except WorkspaceBusy:
                if not use_exclusive_create or attempt >= 2:
                    raise
                if not _try_reclaim_stale_lock(self.path):
                    raise
        assert self._fd is not None
        try:
            _require_safe_lock_inode(self._fd, self.path)
        except (OSError, WorkspacePathError) as error:
            _close_lock_fd(self._fd)
            self._fd = None
            if isinstance(error, WorkspacePathError):
                raise
            raise WorkspaceError(f"cannot inspect workspace lock: {self.path}") from error
        try:
            _acquire_exclusive_lock(self._fd, self.path)
        except WorkspaceBusy:
            _close_lock_fd(self._fd)
            self._fd = None
            raise
        except OSError as error:
            _close_lock_fd(self._fd)
            self._fd = None
            raise WorkspaceError(f"cannot lock workspace: {self.path}") from error
        try:
            _require_safe_lock_inode(self._fd, self.path)
            _write_lock_payload(self._fd)
        except WorkspacePathError:
            _release_exclusive_lock(self._fd)
            _close_lock_fd(self._fd)
            self._fd = None
            raise
        except OSError:
            _release_exclusive_lock(self._fd)
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
        _release_exclusive_lock(self._fd)
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        if self._exclusive_file:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


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
