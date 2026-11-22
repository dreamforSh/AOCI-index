#!/usr/bin/env python3
"""`.aoci/.lock`: heartbeat while held, host-aware reaping rules, and heartbeat thread lifecycle."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aoci_testkit  # noqa: E402,F401  (puts scripts/ on sys.path)
from aoci_core import lock  # noqa: E402


class LockHeartbeatAndReapingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="aoci-lock-"))
        self.path = self.dir / ".lock"

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _age(self, seconds: float) -> None:
        t = time.time() - seconds
        os.utime(self.path, (t, t))

    def test_heartbeat_keeps_long_held_lock_fresh_and_records_host(self) -> None:
        with lock.IndexLock(self.dir, timeout=1, stale=0.4):
            tokens = self.path.read_text(encoding="ascii").split()
            self.assertEqual(int(tokens[0]), os.getpid())
            self.assertEqual(tokens[3], lock._local_host())
            time.sleep(0.6)  # longer than `stale`: only the heartbeat keeps the mtime fresh
            self.assertLess(time.time() - self.path.stat().st_mtime, 0.3)
        self.assertFalse(self.path.exists())
        self.assertFalse([t for t in threading.enumerate() if t.name == "aoci-lock-heartbeat"])

    def test_reaping_rules_by_holder_liveness_and_host(self) -> None:
        probe = lock.IndexLock(self.dir, stale=5, hard_stale=60)
        # Live same-host holder past `stale`: never reaped before `hard_stale`
        self.path.write_text(f"{os.getpid()} t tok {lock._local_host()}\n", encoding="ascii")
        self._age(10)
        self.assertIsNone(probe._stale_snapshot())
        self._age(120)
        self.assertIsNotNone(probe._stale_snapshot(), "past hard_stale a frozen/reused PID is reaped")
        # Dead same-host holder (legacy line without host): reaped once older than the 0.25s grace period
        self.path.write_text("99999999 t tok\n", encoding="ascii")
        self._age(1)
        self.assertIsNotNone(probe._stale_snapshot())
        # Another host: PID liveness is meaningless, fall back to the plain `stale` age
        self.path.write_text(f"{os.getpid()} t tok some-other-host\n", encoding="ascii")
        self._age(2)
        self.assertIsNone(probe._stale_snapshot())
        self._age(10)
        self.assertIsNotNone(probe._stale_snapshot())

    def test_contender_cannot_steal_a_live_lock(self) -> None:
        outcome = []
        with lock.IndexLock(self.dir, timeout=1, stale=0.3):

            def contender() -> None:
                try:
                    with lock.IndexLock(self.dir, timeout=0.6, stale=0.3):
                        outcome.append("acquired")
                except TimeoutError:
                    outcome.append("timeout")

            t = threading.Thread(target=contender)
            t.start()
            t.join(timeout=3)
        self.assertEqual(outcome, ["timeout"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
