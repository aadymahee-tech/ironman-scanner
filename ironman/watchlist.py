"""Watchlist loading for the headless scan.

The enabled/disabled flags, the 40-slot Pine ceiling and the ``EXCHANGE:TICKER``
validation all live in ``tools/build_indicator.py``, because that is what
generates the indicator. Rather than re-implement that parsing here — and risk
the scan and the on-chart table disagreeing about which symbols are live — the
scanner loads that exact function.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "tools" / "build_indicator.py"

_generator = None


def _load_generator():
    global _generator
    if _generator is None:
        if not GENERATOR.exists():
            raise FileNotFoundError(f"watchlist generator not found: {GENERATOR}")
        spec = importlib.util.spec_from_file_location("ironman_build_indicator", GENERATOR)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {GENERATOR}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _generator = module
    return _generator


def load_watchlist(path: Optional[str] = None) -> List[dict]:
    """Return the enabled watchlist rows, in generator (sector) order.

    Raises SystemExit via the generator's own validation on a malformed CSV or
    more than 40 enabled rows.
    """
    generator = _load_generator()
    return generator.load_watchlist(Path(path) if path else generator.DEFAULT_DATA)
