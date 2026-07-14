"""
single_instance.py — refuse to run two LIVE executors against the same account.

Two bot processes pointed at the SAME Hyperliquid account both mirror every
signal, so every real order is placed twice (a client hit exactly this: a
leftover test instance on a second UI port kept trading beside the production
bot, doubling every position). The feed sends each signal only once, but nothing
stops two independent consumers from both acting on it — the guard has to live in
the executor side.

This enforces the real invariant: AT MOST ONE live executor per Hyperliquid
account per machine. It uses an OS-level advisory lock (fcntl on POSIX,
msvcrt on Windows) held for the lifetime of the process. The kernel releases it
automatically if the process dies — so there is no stale-PID problem across
crashes or reboots.

Dry-run / mock loops never lock (they place no real orders, so running several is
harmless). The lock key is derived from the account address, so two DIFFERENT
accounts on one machine (e.g. two family members) can each run their own bot.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile

log = logging.getLogger("observer.lock")

# Where the msvcrt byte-range lock sits. Kept well past the small PID header we
# write at offset 0 so another process can still READ the holder PID for a
# diagnostic message while we hold the lock (Windows mandatory locks would block
# a read of the locked byte).
_WIN_LOCK_OFFSET = 1_000_000


class AlreadyRunning(RuntimeError):
    """Raised when another live process already holds the account lock."""


if os.name == "nt":  # pragma: no cover - platform dependent
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        # LK_NBLCK: non-blocking exclusive lock; raises OSError if already held.
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> None:
        # Non-blocking exclusive lock; raises OSError (EACCES/EAGAIN) if held.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class SingleInstanceLock:
    """Advisory single-instance lock keyed to a Hyperliquid account address."""

    def __init__(self, account_address: "str | None"):
        key = (account_address or "default").strip().lower()
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        self.path = os.path.join(tempfile.gettempdir(), f"observer-bot-{digest}.lock")
        self._fd: "int | None" = None

    def acquire(self) -> None:
        """Take the lock or raise AlreadyRunning if another process holds it."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _try_lock(fd)
        except OSError as exc:
            holder = self._read_holder()
            os.close(fd)
            raise AlreadyRunning(
                "another Observer bot is already running LIVE for this account "
                f"(lock file {self.path}{holder}). Refusing to start a second "
                "live instance — two bots on one Hyperliquid account would place "
                "every order twice. Stop the other instance first."
            ) from exc
        # We hold the lock. Record our PID at offset 0 for a friendly diagnostic
        # (best-effort; failure here never gives up the lock).
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
        except OSError:
            pass
        self._fd = fd
        log.info("single-instance lock acquired (%s)", self.path)

    def release(self) -> None:
        """Release the lock. Safe to call more than once / if never acquired."""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            _unlock(fd)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def _read_holder(self) -> str:
        """", held by PID N" if we can read the holder's recorded PID, else ""."""
        try:
            with open(self.path, "r", encoding="ascii") as fh:
                pid = fh.read().strip()
            return f", held by PID {pid}" if pid else ""
        except OSError:
            return ""

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
