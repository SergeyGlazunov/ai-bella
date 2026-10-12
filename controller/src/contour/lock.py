from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class LockBackend(Protocol):
    def try_acquire(self, path: str) -> bool: ...

    def release(self, path: str) -> None: ...


class MemoryBackend:
    def __init__(self) -> None:
        self._held: set[str] = set()

    def try_acquire(self, path: str) -> bool:
        if path in self._held:
            return False
        self._held.add(path)
        return True

    def release(self, path: str) -> None:
        self._held.discard(path)


class FcntlBackend:
    def __init__(self) -> None:
        self._handles: dict[str, int] = {}

    def try_acquire(self, path: str) -> bool:
        import fcntl

        fd = os.open(path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._handles[path] = fd
        return True

    def release(self, path: str) -> None:
        import fcntl

        fd = self._handles.pop(path, None)
        if fd is None:
            return
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class Lock:
    def __init__(self, path: str, backend: LockBackend) -> None:
        self._path = path
        self._backend = backend

    def __enter__(self) -> Lock:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self._backend.release(self._path)
        return False


_default_backend: LockBackend | None = None


def _get_default_backend() -> LockBackend:
    global _default_backend
    if _default_backend is None:
        _default_backend = FcntlBackend()
    return _default_backend


def acquire(path: str, backend: LockBackend | None = None) -> Lock | None:
    lock_backend = backend if backend is not None else _get_default_backend()
    if lock_backend.try_acquire(path):
        return Lock(path, lock_backend)
    return None
