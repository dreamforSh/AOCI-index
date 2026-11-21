"""Cross-process exclusive index lock (`.aoci/.lock`) with atomic stale-lock reaping and owner tokens."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional, Tuple

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
    """Exclusive cross-process lock on `.aoci/.lock` (O_CREAT|O_EXCL, dead-PID & TOCTOU-safe stale recovery)."""

    def __init__(self, aoci_dir: Path, timeout: float = LOCK_TIMEOUT_S, stale: float = LOCK_STALE_S) -> None:
        self.path = aoci_dir / LOCK_FILE
        self.timeout = timeout
        self.stale = stale
        self._fd: Optional[int] = None
        self._token: Optional[str] = None

    def _stale_snapshot(self) -> Optional[Tuple[int, str]]:
        """Return `(st_mtime_ns, raw_content)` when `.lock` is confirmed stale, else `None`."""
        try:
            st = self.path.stat()
            age = time.time() - st.st_mtime
        except OSError:
            return None
        if age < 0.25 and age <= self.stale:
            return None
        try:
            raw = self.path.read_text(encoding="ascii", errors="replace").strip()
        except OSError:
            raw = ""
        tokens = raw.split()
        if age > self.stale:
            return (st.st_mtime_ns, raw)
        if tokens:
            try:
                if not _pid_is_alive(int(tokens[0])):
                    return (st.st_mtime_ns, raw)
            except ValueError:
                pass
        return None

    def _is_stale(self) -> bool:
        return self._stale_snapshot() is not None

    def _try_reap_stale(self) -> bool:
        snap = self._stale_snapshot()
        if snap is None:
            return False
        stale_mtime_ns, stale_raw = snap
        try:
            cur_st = self.path.stat()
            if cur_st.st_mtime_ns != stale_mtime_ns:
                return False
            if stale_raw:
                cur_raw = self.path.read_text(encoding="ascii", errors="replace").strip()
                if cur_raw != stale_raw:
                    return False
            reap_tmp = self.path.with_name(f"{self.path.name}.stale.{os.getpid()}.{time.time_ns()}")
            os.replace(self.path, reap_tmp)
            try:
                reap_tmp.unlink()
            except OSError:
                pass
            return True
        except OSError:
            return False

    def __enter__(self) -> "IndexLock":
        deadline = time.monotonic() + self.timeout
        delay = 0.02
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                token = os.urandom(6).hex()
                self._token = token
                os.write(self._fd, f"{os.getpid()} {utc_now_iso()} {token}\n".encode("ascii"))
                return self
            except (FileExistsError, PermissionError) as exc:
                if isinstance(exc, PermissionError) and not self.path.exists():
                    raise
                if self._try_reap_stale():
                    continue
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
        expected_tok = self._token
        self._token = None
        for _ in range(10):
            try:
                if expected_tok is not None and self.path.exists():
                    raw = self.path.read_text(encoding="ascii", errors="replace")
                    if expected_tok not in raw:
                        return
                self.path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:
                time.sleep(0.02)
            except OSError:
                return
