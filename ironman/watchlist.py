"""Watchlist loading for the headless scan.

Two universes, one loader:

* ``data/fno_watchlist_price_band.csv`` — the 40 curated names the Pine
  indicator can hold, limited by Pine's 40 ``request.security()`` ceiling.
* ``data/fno_universe.csv`` — the full NSE single-stock F&O universe (210+
  names), used by the scan and the dashboard. No ceiling applies: the scanner is
  a Python program, not a Pine script.

The enabled/disabled flags, symbol format and duplicate checks all live in
``tools/build_indicator.py``, because that is what generates the indicator.
Rather than re-implement that parsing here — and risk the scan and the on-chart
table disagreeing about which symbols are live — the scanner loads that exact
function. The only difference is the symbol ceiling, which the caller passes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "tools" / "build_indicator.py"
UNIVERSE = ROOT / "data" / "fno_universe.csv"

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
    """The 40-name price-band watchlist, in generator (sector) order.

    Raises SystemExit via the generator's own validation on a malformed CSV or
    more than 40 enabled rows.
    """
    generator = _load_generator()
    return generator.load_watchlist(Path(path) if path else generator.DEFAULT_DATA)


def load_universe(path: Optional[str] = None) -> List[dict]:
    """The full F&O universe — no Pine 40-symbol ceiling.

    Same validation as :func:`load_watchlist`, with the ceiling removed.
    """
    generator = _load_generator()
    return generator.load_watchlist(Path(path) if path else UNIVERSE, max_symbols=None)

