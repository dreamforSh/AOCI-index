#!/usr/bin/env python3
"""CLI entrypoint wrapper for `aoci_core.installer` (`python scripts/install.py ...`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from aoci_core import installer as _installer  # noqa: E402
from aoci_core.installer import *  # noqa: E402, F401, F403

main = _installer.main

if __name__ == "__main__":
    raise SystemExit(main())
