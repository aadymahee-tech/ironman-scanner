"""Tradetron Developer API client, plus a fixture client for offline runs.

What is known about the API (see ``docs/API.md`` for provenance):

* base URL ``https://tradetron.tech/api/v1``
* ``Authorization: Bearer <read-only key>`` created under *Tradetron -> Developer API*
* every response is wrapped as ``{"success": true, "data": {...}}``
* ``GET /me/deployments[?page=N]``                 -> ``data.deployments[]``, ``data.last_page``
* ``GET /me/deployments/{id}/positions``          -> ``data.positions[]``, ``data.positions_hidden``, ``data.as_of``
* ``GET /me/notifications``                        -> platform notifications
* a per-key rate limit applies

The exact field names inside a deployment or a position record can differ between
accounts and releases, so normalisation goes through an alias table and the raw
record is always kept. Nothing here ever logs or raises the API key.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import (DEFAULT_BASE_URL, DEFAULT_DEPLOYMENTS_PATH, DEFAULT_NOTIFICATIONS_PATH,
                     DEFAULT_POSITIONS_PATH)

USER_AGENT = "tradetron-pnl/0.1 (+https://github.com/aadymahee-tech)"
DEFAULT_TIMEOUT = 20.0
MAX_PAGES = 20

Transport = Callable[[str, Dict[str, str], float], Tuple[int, Mapping[str, str], bytes]]


class ApiError(RuntimeError):
    """A failed Developer API call. The message is safe to log (no credentials)."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: str = "API_ERROR", retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


def redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, secret[:3] + "…" + secret[-2:])
    return text


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def urllib_transport(url: str, headers: Dict[str, str], timeout: float):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        return exc.code, dict((exc.headers or {}).items()), body


class TradetronClient:
    """Thin, retrying GET client over the ``/me/*`` read-only endpoints."""

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = DEFAULT_TIMEOUT, retries: int = 2,
                 transport: Optional[Transport] = None, user_agent: str = USER_AGENT,
                 sleep: Callable[[float], None] = time.sleep,
                 deployments_path: str = DEFAULT_DEPLOYMENTS_PATH,
                 positions_path: str = DEFAULT_POSITIONS_PATH,
                 notifications_path: str = DEFAULT_NOTIFICATIONS_PATH):
        if not api_key or not api_key.strip():
            raise ApiError("TRADETRON_API_KEY is not set", code="NOT_CONFIGURED")
        self._api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.deployments_path = deployments_path
        self.positions_path = positions_path
        self.notifications_path = notifications_path
        self.timeout = timeout
        self.retries = max(0, retries)
        self.transport = transport or urllib_transport
        self.user_agent = user_agent
        self._sleep = sleep
        self.calls = 0

    @property
    def source(self) -> str:
        return "api"

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": self.user_agent,
        }

    def get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        """GET ``path`` and return the unwrapped ``data`` member."""
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(dict(params))

        last_error: Optional[ApiError] = None
        for attempt in range(self.retries + 1):
            self.calls += 1
            try:
                status, headers, body = self.transport(url, self._headers(), self.timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = ApiError(
                    f"network error calling {path}: {redact(str(exc), self._api_key)}",
                    code="NETWORK_ERROR", retryable=True,
                )
                self._backoff(attempt, None)
                continue

            if status == 429 or status >= 500:
                reason = "rate limit exceeded" if status == 429 else "server error"
                last_error = ApiError(
                    f"Tradetron API {reason} on {path} (HTTP {status})",
                    status=status, code=f"HTTP_{status}", retryable=True,
                )
                self._backoff(attempt, _retry_after(headers))
                continue

            if status in (401, 403):
                raise ApiError(
                    f"authentication or read permission denied on {path} (HTTP {status}); "
                    "check TRADETRON_API_KEY and the key's read permissions",
                    status=status, code=f"HTTP_{status}",
                )
            if status == 404:
                raise ApiError(
                    f"{path} not found (HTTP 404); check TRADETRON_API_BASE_URL and the endpoint path",
                    status=status, code="HTTP_404",
                )
            if status >= 400:
                raise ApiError(f"request to {path} failed (HTTP {status})", status=status, code=f"HTTP_{status}")

            return self._unwrap(path, status, body)

        assert last_error is not None
        raise last_error

    def _backoff(self, attempt: int, retry_after: Optional[float]) -> None:
        if attempt >= self.retries:
            return
        delay = retry_after if retry_after is not None else 0.8 * (attempt + 1)
        self._sleep(min(delay, 30.0))

    def _unwrap(self, path: str, status: int, body: bytes) -> Any:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(
                f"{path} returned non-JSON (HTTP {status}): {_excerpt(body)}",
                status=status, code="INVALID_JSON",
            ) from None
        if not isinstance(payload, dict):
            raise ApiError(f"{path} returned {type(payload).__name__}, expected an object",
                           status=status, code="INVALID_RESPONSE")
        if payload.get("success") is False:
            message = payload.get("message") or payload.get("error") or "success=false"
            raise ApiError(f"{path}: {message}", status=status, code="API_FAILURE")
        if "data" in payload:
            return payload["data"]
        # Some deployments of the API may answer without the envelope; accept a
        # bare object if it carries the fields we are after.
        if any(k in payload for k in ("deployments", "positions", "notifications")):
            return payload
        raise ApiError(f"{path}: response has no 'data' member", status=status, code="INVALID_RESPONSE")

    # -- endpoints ---------------------------------------------------------- #
    def list_deployments(self, max_pages: int = MAX_PAGES) -> List[dict]:
        deployments: List[dict] = []
        page = 1
        while page <= max_pages:
            data = self.get(self.deployments_path, {"page": page} if page > 1 else None)
            rows, last_page = _deployments_page(data)
            deployments.extend(rows)
            if page >= last_page:
                break
            page += 1
        else:
            raise ApiError(f"deployment list exceeded {max_pages} pages; refusing to loop",
                           code="TOO_MANY_PAGES")
        return deployments

    def deployment_positions(self, deployment_id: int) -> dict:
        dep_id = int(deployment_id)
        data = self.get(self.positions_path.replace("{id}", str(dep_id)))
        if not isinstance(data, dict):
            raise ApiError(f"positions for {dep_id}: expected an object", code="INVALID_POSITIONS")
        return data

    def notifications(self) -> Any:
        return self.get(self.notifications_path)


def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _excerpt(body: bytes, limit: int = 120) -> str:
    text = body[:limit].decode("utf-8", errors="replace").replace("\n", " ")
    return text + ("…" if len(body) > limit else "")


def _deployments_page(data: Any) -> Tuple[List[dict], int]:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)], 1
    if not isinstance(data, dict):
        raise ApiError("deployment response is neither a list nor an object", code="INVALID_DEPLOYMENTS")
    rows = data.get("deployments")
    if rows is None:
        rows = data.get("data") if isinstance(data.get("data"), list) else None
    if not isinstance(rows, list):
        raise ApiError("deployment response is missing data.deployments[]", code="INVALID_DEPLOYMENTS")
    try:
        last_page = max(1, int(data.get("last_page") or 1))
    except (TypeError, ValueError):
        last_page = 1
    return [d for d in rows if isinstance(d, dict)], last_page


# --------------------------------------------------------------------------- #
# Fixture client — same interface, no network
# --------------------------------------------------------------------------- #
class FixtureClient:
    """Serve deployments/positions from a JSON file.

    Layout::

        {
          "deployments": [ {...}, {...} ],         # or {"pages": [[...], [...]]}
          "positions": { "1001": {"deployment_id": 1001, "positions_hidden": false,
                                  "as_of": "...", "positions": [ ... ]} },
          "notifications": [ ... ]
        }
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        with self.path.open("r", encoding="utf-8") as fh:
            self.payload = json.load(fh)
        self.calls = 0

    @property
    def source(self) -> str:
        return "fixture"

    def list_deployments(self, max_pages: int = MAX_PAGES) -> List[dict]:
        self.calls += 1
        block = self.payload.get("deployments", [])
        if isinstance(block, dict) and "pages" in block:
            rows: List[dict] = []
            for page in block["pages"]:
                rows.extend(page)
            return rows
        if isinstance(block, dict):
            rows, _ = _deployments_page(block)
            return rows
        return list(block)

    def deployment_positions(self, deployment_id: int) -> dict:
        self.calls += 1
        table = self.payload.get("positions", {}) or {}
        entry = table.get(str(int(deployment_id)))
        if entry is None:
            return {"deployment_id": int(deployment_id), "positions_hidden": False,
                    "as_of": None, "positions": []}
        return entry

    def notifications(self) -> Any:
        self.calls += 1
        return self.payload.get("notifications", [])


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
FIELD_ALIASES: Dict[str, Sequence[str]] = {
    "deployment_id": ("deployment_id", "id", "deployed_strategy_id", "deployment"),
    "name": ("name", "strategy_name", "deployment_name", "strategy"),
    "deployment_type": ("deployment_type", "type", "mode", "deployment_mode"),
    "status": ("status", "state"),
    "broker": ("broker", "broker_name"),
    "multiplier": ("multiplier", "lot_multiplier"),
    "capital": ("capital", "deployed_capital"),
    "pnl": ("pnl", "total_pnl", "net_pnl", "mtm", "profit_loss", "pl"),
    "open_positions": ("open_positions", "open_legs"),
    "total_positions": ("total_positions", "positions_count"),
    "currency": ("currency_code", "currency"),
    "deployed_on": ("deployed_on", "deployed_at", "created_at"),
}

POSITION_ALIASES: Dict[str, Sequence[str]] = {
    "symbol": ("instrument_symbol", "symbol", "tradingsymbol", "trading_symbol", "instrument", "scrip"),
    "quantity": ("net_quantity", "quantity", "qty", "net_qty", "position"),
    "pnl": ("pnl", "mtm", "unrealised_pnl", "unrealized_pnl", "profit_loss"),
    "side": ("side", "transaction_type", "direction"),
    "ltp": ("ltp", "last_price", "last_traded_price"),
    "average_price": ("average_price", "avg_price", "entry_price", "price"),
}

PAPER_MARKERS = ("OFFLINE", "PAPER", "SIMULAT")


def pick(raw: Mapping[str, Any], key: str, aliases: Mapping[str, Sequence[str]] = FIELD_ALIASES) -> Any:
    for name in aliases.get(key, (key,)):
        if name in raw and raw[name] is not None:
            return raw[name]
    return None


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "—", "NA", "N/A", "null"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_int(value: Any) -> Optional[int]:
    number = to_float(value)
    return None if number is None else int(round(number))


@dataclass
class Deployment:
    deployment_id: int
    name: str = ""
    deployment_type: str = ""
    status: str = ""
    broker: str = ""
    multiplier: Optional[float] = None
    capital: Optional[float] = None
    pnl: float = 0.0
    pnl_known: bool = True
    open_positions: Optional[int] = None
    total_positions: Optional[int] = None
    currency: str = "INR"
    deployed_on: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def paper(self) -> bool:
        kind = (self.deployment_type or "").upper()
        return any(marker in kind for marker in PAPER_MARKERS)

    @property
    def in_error(self) -> bool:
        return (self.status or "").strip().lower().startswith("error")

    @property
    def finished(self) -> bool:
        text = (self.status or "").strip().lower()
        return text.startswith(("exited", "paused", "blocked", "stopped", "completed"))

    @property
    def label(self) -> str:
        return self.name or f"Deployment {self.deployment_id}"


def normalise_deployment(raw: Mapping[str, Any], aliases: Mapping[str, Sequence[str]] = FIELD_ALIASES) -> Deployment:
    dep_id = to_int(pick(raw, "deployment_id", aliases))
    if dep_id is None:
        raise ValueError(f"deployment record has no id: {sorted(raw.keys())}")
    pnl_raw = to_float(pick(raw, "pnl", aliases))
    currency = pick(raw, "currency", aliases)
    return Deployment(
        deployment_id=dep_id,
        name=str(pick(raw, "name", aliases) or "").strip(),
        deployment_type=str(pick(raw, "deployment_type", aliases) or "").strip(),
        status=str(pick(raw, "status", aliases) or "").strip(),
        broker=str(pick(raw, "broker", aliases) or "").strip(),
        multiplier=to_float(pick(raw, "multiplier", aliases)),
        capital=to_float(pick(raw, "capital", aliases)),
        pnl=pnl_raw if pnl_raw is not None else 0.0,
        pnl_known=pnl_raw is not None,
        open_positions=to_int(pick(raw, "open_positions", aliases)),
        total_positions=to_int(pick(raw, "total_positions", aliases)),
        currency=str(currency or "INR").strip().upper() or "INR",
        deployed_on=str(pick(raw, "deployed_on", aliases) or "").strip()[:32],
        raw=dict(raw),
    )


def normalise_deployments(rows: Sequence[Mapping[str, Any]]) -> Tuple[List[Deployment], List[str]]:
    """Normalise every row; malformed rows are reported, not fatal."""
    out: List[Deployment] = []
    problems: List[str] = []
    for row in rows:
        try:
            out.append(normalise_deployment(row))
        except ValueError as exc:
            problems.append(str(exc))
    out.sort(key=lambda d: d.deployment_id)
    return out, problems


@dataclass
class PositionSet:
    deployment_id: int
    positions_hidden: Optional[bool]
    as_of: Optional[str]
    positions: List[dict]

    @property
    def count(self) -> int:
        return len(self.positions)


def normalise_positions(payload: Mapping[str, Any], deployment_id: int) -> PositionSet:
    rows = payload.get("positions")
    if rows is None and isinstance(payload.get("data"), list):
        rows = payload["data"]
    if not isinstance(rows, list):
        rows = []
    hidden = payload.get("positions_hidden")
    if not isinstance(hidden, bool):
        hidden = None
    as_of = payload.get("as_of")
    return PositionSet(
        deployment_id=int(payload.get("deployment_id") or deployment_id),
        positions_hidden=hidden,
        as_of=str(as_of) if as_of is not None else None,
        positions=[r for r in rows if isinstance(r, dict)],
    )


def position_summary(positions: Sequence[Mapping[str, Any]]) -> dict:
    """Best-effort roll-up of raw position records; unknown shapes degrade to counts."""
    symbols: List[str] = []
    pnl_total = 0.0
    pnl_seen = False
    net_qty = 0.0
    for row in positions:
        symbol = pick(row, "symbol", POSITION_ALIASES)
        if symbol:
            symbols.append(str(symbol))
        pnl = to_float(pick(row, "pnl", POSITION_ALIASES))
        if pnl is not None:
            pnl_total += pnl
            pnl_seen = True
        qty = to_float(pick(row, "quantity", POSITION_ALIASES))
        if qty is not None:
            net_qty += qty
    return {
        "count": len(positions),
        "symbols": symbols,
        "pnl": round(pnl_total, 2) if pnl_seen else None,
        "net_quantity": net_qty,
    }
