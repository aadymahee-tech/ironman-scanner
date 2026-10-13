"""Bar providers for the headless IronMan scan.

Two sources, one interface:

* ``yahoo``    — live OHLCV via the public Yahoo Finance chart endpoint.
* ``fixture``  — bars read from a local JSON file, for offline tests and dry runs.

Everything here is standard library only. That is deliberate: the workflow has no
``pip install`` step, so the scanner has to run on a bare Python.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from .engine import Bar

# NSE has no daylight saving, so a fixed offset is exact — and it avoids a
# tzdata dependency on minimal CI images.
IST = timezone(timedelta(hours=5, minutes=30))
NSE_CLOSE_IST = (15, 30)

DEFAULT_TIMEOUT = 20.0
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0 Safari/537.36 ironman-scanner/16.2"
)

CHART_URL = "https://{host}/v8/finance/chart/{ticker}"

# Yahoo mirrors the chart endpoint across query1/query2. Retries alternate
# between them: the usual failure is a per-host rate limit, which the other
# host does not share.
HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")

# interval -> (yahoo code, seconds per bar, default range)
INTERVALS: Dict[str, tuple] = {
    "1m": ("1m", 60, "7d"),
    "5m": ("5m", 300, "60d"),
    "15m": ("15m", 900, "60d"),
    "30m": ("30m", 1800, "60d"),
    "1h": ("60m", 3600, "60d"),
    "1d": ("1d", 86400, "1y"),
}

# Yahoo only serves fine intraday history for a limited lookback.
MAX_RANGE_DAYS = {"1m": 7, "5m": 60, "15m": 60, "30m": 60, "1h": 60, "1d": 3650}

EXCHANGE_SUFFIX = {"NSE": ".NS", "BSE": ".BO", "NFO": ".NS"}


def to_yahoo_ticker(symbol: str) -> str:
    """``NSE:HDFCBANK`` -> ``HDFCBANK.NS``. Bare tickers pass through."""
    if ":" not in symbol:
        return symbol
    exchange, _, ticker = symbol.partition(":")
    suffix = EXCHANGE_SUFFIX.get(exchange.upper())
    if suffix is None:
        raise ValueError(f"unsupported exchange {exchange!r} in {symbol!r}")
    return f"{ticker}{suffix}"


def interval_seconds(interval: str) -> int:
    try:
        return INTERVALS[interval][1]
    except KeyError:
        raise ValueError(f"unsupported interval {interval!r}; choose from {sorted(INTERVALS)}") from None


def is_forming(bar_ts: datetime, interval: str, now: datetime) -> bool:
    """True when the bar's period has not finished yet.

    Intrabar, the last candle from any provider is still moving; scoring it would
    mean reporting signals that can vanish. Daily candles close at 15:30 IST.
    """
    if interval == "1d":
        close_at = datetime.combine(
            bar_ts.date(), datetime.min.time(), tzinfo=IST
        ).replace(hour=NSE_CLOSE_IST[0], minute=NSE_CLOSE_IST[1])
        return now < close_at
    return now < bar_ts + timedelta(seconds=interval_seconds(interval))


def _parse_ts(raw) -> datetime:
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    text = str(raw).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Yahoo
# --------------------------------------------------------------------------- #
def parse_yahoo_payload(payload: dict, interval: str) -> List[Bar]:
    """Turn a chart response into bars, dropping the still-forming candle."""
    chart = payload.get("chart") or {}
    error = chart.get("error")
    if error:
        raise ValueError(f"yahoo error: {error.get('description') or error}")
    results = chart.get("result") or []
    if not results:
        raise ValueError("yahoo returned no result")

    result = results[0]
    stamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    opens, highs = quote.get("open") or [], quote.get("high") or []
    lows, closes = quote.get("low") or [], quote.get("close") or []
    volumes = quote.get("volume") or []

    now = datetime.now(timezone.utc)
    bars: List[Bar] = []
    for i, ts in enumerate(stamps):
        o, h, l, c = (
            _at(opens, i),
            _at(highs, i),
            _at(lows, i),
            _at(closes, i),
        )
        if None in (o, h, l, c):
            continue                      # provider gap / holiday stub
        bar_ts = _parse_ts(ts)
        if is_forming(bar_ts, interval, now):
            continue
        bars.append(Bar(ts=bar_ts, open=o, high=h, low=l, close=c, volume=_at(volumes, i) or 0.0))

    bars.sort(key=lambda b: b.ts)
    return bars


def _at(seq: Sequence, i: int):
    return seq[i] if i < len(seq) else None


def fetch_yahoo(
    symbol: str,
    interval: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    range_: Optional[str] = None,
    retries: int = 2,
) -> List[Bar]:
    """Fetch and parse one symbol's bars. Raises on network/parse failure."""
    if interval not in INTERVALS:
        raise ValueError(f"unsupported interval {interval!r}; choose from {sorted(INTERVALS)}")
    code, _, default_range = INTERVALS[interval]
    ticker = to_yahoo_ticker(symbol)
    query = urllib.parse.urlencode(
        {"interval": code, "range": range_ or default_range, "includePrePost": "false"}
    )

    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        url = CHART_URL.format(host=HOSTS[attempt % len(HOSTS)], ticker=urllib.parse.quote(ticker))
        req = urllib.request.Request(
            f"{url}?{query}", headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return parse_yahoo_payload(payload, interval)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"{symbol}: {last_error}")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def load_fixture(path: str, symbol: str) -> List[Bar]:
    """Read one symbol's bars from a fixture JSON file.

    Accepts either ``{"symbols": {"NSE:TCS": [...]}}`` or a flat
    ``{"NSE:TCS": [...]}``. Each bar is ``[ts, open, high, low, close, volume]``
    with ``ts`` as ISO-8601 or epoch seconds.
    """
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    table = payload.get("symbols", payload) if isinstance(payload, dict) else {}
    raw = table.get(symbol)
    if raw is None:
        raise KeyError(f"{symbol} not present in fixture {path}")
    rows = raw.get("bars", raw) if isinstance(raw, dict) else raw

    bars: List[Bar] = []
    for row in rows:
        if len(row) < 5:
            raise ValueError(f"bar needs at least [ts, o, h, l, c]: {row!r}")
        row = list(row) + [0.0] * (6 - len(row))
        bars.append(
            Bar(
                ts=_parse_ts(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        )
    bars.sort(key=lambda b: b.ts)
    return bars
