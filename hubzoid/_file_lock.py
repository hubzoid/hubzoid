"""Exclusive locks on dedicated lock files, released by closing the handle.

Use flock on POSIX and a one-byte msvcrt lock on Windows. Lock files must be
opened for writing without truncation. They contain no application data.
"""
from __future__ import annotations

import errno
import os
import time

_WINDOWS = os.name == "nt"


def lock(file, *, blocking=True):
    fd = file if isinstance(file, int) else file.fileno()
    if not _WINDOWS:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    import msvcrt
    # msvcrt locks byte ranges, including a shared first byte in an empty file.
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if not blocking:
                raise BlockingIOError(exc.errno, "Lock is held by another process") from None
            time.sleep(0.05)


def unlock(file):
    fd = file if isinstance(file, int) else file.fileno()
    if not _WINDOWS:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    import msvcrt
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
