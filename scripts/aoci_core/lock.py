"""
Cross-process exclusive index lock (`.aoci/.lock`): owner tokens plus hostname, same-thread re-entrancy,
a heartbeat that keeps long holders fresh, and atomic stale-lock reaping (dead PIDs at once, live same-host
PIDs only past `hard_stale`).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .constants import LOCK_FILE, LOCK_STALE_S, LOCK_TIMEOUT_S, utc_now_iso

_THREAD_LOCKS = threading.local()
_LOCAL_HOST: Optional[str] = None


def _local_host() -> str:
    """Hostname recorded in `.lock` so PID liveness is only trusted for holders on this machine."""
    global _LOCAL_HOST
    if _LOCAL_HOST is None:
        try:
            import socket

            raw_host = socket.gethostname() or "localhost"
            _LOCAL_HOST = raw_host.encode("ascii", "replace").decode("ascii").replace(" ", "_") or "localhost"
        except Exception:
            _LOCAL_HOST = "localhost"
    return _LOCAL_HOST


def _canonical_lock_key(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(str(path)))


def _thread_held_map() -> Dict[str, int]:
    held = getattr(_THREAD_LOCKS, "held", None)
    if held is None:
        held = {}
        _THREAD_LOCKS.held = held
    return held


def _pid_is_alive(pid: int) -> bool:
    """Return True if a local OS process with `pid` still exists (never sends signals on Windows)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":  # `sys.platform` (not `os.name`) lets type checkers narrow Windows-only ctypes APIs
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
    """
    Exclusive cross-process lock on `.aoci/.lock` (O_CREAT|O_EXCL, dead-PID & TOCTOU-safe stale recovery).

    The holder refreshes the lock's mtime every `stale / 4` seconds (heartbeat), so long operations are
    never mistaken for crashed ones. Recovery rules: a dead same-host PID is reaped at once; a live
    same-host PID is only reaped past `hard_stale` (default `stale * 12`: frozen holder or PID reuse);
    locks from another host (or unparsable ones) fall back to the plain `stale` age limit.
    """

    def __init__(
        self,
        aoci_dir: Path,
        timeout: float = LOCK_TIMEOUT_S,
        stale: float = LOCK_STALE_S,
        hard_stale: Optional[float] = None,
    ) -> None:
        self.path = aoci_dir / LOCK_FILE
        self._key = _canonical_lock_key(self.path)
        self.timeout = timeout
        self.stale = stale
        self.hard_stale = hard_stale if hard_stale is not None else stale * 12.0
        self._fd: Optional[int] = None
        self._token: Optional[str] = None
        self._reentrant: bool = False
        self._heartbeat: Optional[Tuple[threading.Event, threading.Thread]] = None

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
        pid: Optional[int] = None
        if tokens:
            try:
                pid = int(tokens[0])
            except ValueError:
                pid = None
        holder_host = tokens[3] if len(tokens) >= 4 else None  # pre-1.6 locks carry no host => assume local
        if pid is not None and (holder_host is None or holder_host == _local_host()):
            if not _pid_is_alive(pid):
                return (st.st_mtime_ns, raw)
            return (st.st_mtime_ns, raw) if age > self.hard_stale else None
        return (st.st_mtime_ns, raw) if age > self.stale else None

    def _start_heartbeat(self) -> None:
        interval = max(0.05, self.stale / 4.0)
        stop = threading.Event()
        path = self.path

        def _beat() -> None:
            while not stop.wait(interval):
                try:
                    os.utime(path, None)
                except OSError:
                    return

        thread = threading.Thread(target=_beat, name="aoci-lock-heartbeat", daemon=True)
        thread.start()
        self._heartbeat = (stop, thread)

    def _stop_heartbeat(self) -> None:
        hb = self._heartbeat
        self._heartbeat = None
        if hb is not None:
            hb[0].set()
            hb[1].join(timeout=1.0)

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
        held = _thread_held_map()
        if held.get(self._key, 0) > 0 and self.path.exists():
            held[self._key] += 1
            self._reentrant = True
            return self
        deadline = time.monotonic() + self.timeout
        delay = 0.02
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                token = os.urandom(6).hex()
                self._token = token
                self._reentrant = False
                os.write(self._fd, f"{os.getpid()} {utc_now_iso()} {token} {_local_host()}\n".encode("ascii", "replace"))
                held[self._key] = 1
                self._start_heartbeat()
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
                    ) from None
                time.sleep(delay)
                delay = min(delay * 1.5, 0.2)

    def __exit__(self, *exc: Any) -> None:
        held = _thread_held_map()
        if self._reentrant:
            cur = held.get(self._key, 1)
            if cur > 1:
                held[self._key] = cur - 1
            else:
                held.pop(self._key, None)
            self._reentrant = False
            return
        held.pop(self._key, None)
        self._stop_heartbeat()
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
