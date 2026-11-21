"""Cross-process exclusive index lock (`.aoci/.lock`) with dead-PID and stale recovery."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

from .constants import LOCK_FILE, LOCK_STALE_S, LOCK_TIMEOUT_S, utc_now_iso


def _pid_is_alive(pid: int) -> bool:
    """Return True if a local OS process with `pid` still exists (never sends signals on Windows)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int

            handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED => alive under another user
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


class IndexLock:
    """Exclusive cross-process lock on `.aoci/.lock` (O_CREAT|O_EXCL, dead-PID & stale-lock recovery)."""

    def __init__(self, aoci_dir: Path, timeout: float = LOCK_TIMEOUT_S, stale: float = LOCK_STALE_S) -> None:
        self.path = aoci_dir / LOCK_FILE
        self.timeout = timeout
        self.stale = stale
        self._fd: Optional[int] = None

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        if age > self.stale:
            return True
        # Allow a newly created lock a brief window to finish writing its PID payload
        if age < 0.25:
            return False
        try:
            tokens = self.path.read_text(encoding="ascii", errors="replace").strip().split()
            if tokens and not _pid_is_alive(int(tokens[0])):
                return True
        except (OSError, ValueError):
            pass
        return False

    def __enter__(self) -> "IndexLock":
        deadline = time.monotonic() + self.timeout
        delay = 0.02
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, f"{os.getpid()} {utc_now_iso()}\n".encode("ascii"))
                return self
            except (FileExistsError, PermissionError) as exc:
                if isinstance(exc, PermissionError) and not self.path.exists():
                    # Read-only filesystem / sandbox: fail fast instead of spinning until timeout
                    raise
                if self._is_stale():
                    try:
                        self.path.unlink()
                        continue
                    except OSError:
                        pass
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out after {self.timeout:.0f}s waiting for `{self.path}` "
                        "(another aoci_index.py writer is running; delete the file if it is stale)."
                    )
                time.sleep(delay)
                delay = min(delay * 1.5, 0.2)

    def __exit__(self, *exc: Any) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        for _ in range(10):
            try:
                self.path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:
                time.sleep(0.02)
