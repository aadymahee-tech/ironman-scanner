"""tradetron-pnl — fetch, store, analyse and broadcast a personal Tradetron P&L.

Standard library only. The scheduled workflow has no ``pip install`` step, so
every module here must run on a bare CI Python (3.10+).
"""

__version__ = "0.1.0"

__all__ = [
    "api",
    "analytics",
    "config",
    "ledger",
    "messages",
    "reports",
    "store",
    "tax",
    "telegram",
    "trading_calendar",
]
