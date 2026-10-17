#!/usr/bin/env python3
"""tradetron-pnl command line. See ``python3 ttpnl.py --help``."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tradetron_pnl.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
