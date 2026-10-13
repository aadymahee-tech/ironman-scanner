"""Rendering for the headless scan: markdown for the Actions summary, CSV for history."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from .datafeed import IST
from .engine import ACTIONABLE_THRESHOLD, SymbolScan

CSV_COLUMNS = [
    "scan_ts_utc",
    "interval",
    "symbol",
    "name",
    "sector",
    "status",
    "ltp",
    "score",
    "setup",
    "context",
    "in_band",
    "signal_score",
    "signal_setup",
    "signal_context",
    "signal_bars_ago",
    "signal_bar_utc",
    "box_breakout_long",
    "box_breakout_short",
    "sweep_long",
    "sweep_short",
    "strict_bull",
    "strict_bear",
    "chop",
    "high_vol",
    "error",
]


@dataclass
class ScanReport:
    results: Sequence[SymbolScan]
    interval: str
    lookback: int
    min_price: float
    max_price: float
    started_at: datetime
    finished_at: datetime
    failures: List[tuple] = field(default_factory=list)   # (symbol, error)
    source: str = "yahoo"

    # -- derived ------------------------------------------------------------ #
    @property
    def ok(self) -> List[SymbolScan]:
        return [r for r in self.results if r.status == "ok"]

    @property
    def signals(self) -> List[SymbolScan]:
        return sorted(
            (r for r in self.ok if r.has_signal),
            key=lambda r: (-r.hit.score, r.bars_ago, r.symbol),  # type: ignore[union-attr]
        )

    @property
    def out_of_band(self) -> List[SymbolScan]:
        return [r for r in self.ok if r.latest is not None and not r.latest.in_band]

    @property
    def in_band_quiet(self) -> List[SymbolScan]:
        return [r for r in self.ok if r.latest is not None and r.latest.in_band and not r.has_signal]

    def _band_text(self) -> str:
        return f"₹{self.min_price:,.0f} – ₹{self.max_price:,.0f}"


def _fmt_ist(moment: datetime) -> str:
    return moment.astimezone(IST).strftime("%d %b %Y, %H:%M IST")


def _fmt_price(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.2f}"


def _rating(scan: SymbolScan) -> str:
    if scan.latest is None:
        return "—"
    return f"{scan.latest.score}/5"


def render_markdown(report: ScanReport) -> str:
    """Build the scan summary that the workflow appends to the job summary."""
    signals = report.signals
    lines: List[str] = []

    lines.append(f"# IronMan F&O scan — {_fmt_ist(report.finished_at)}")
    lines.append("")
    lines.append(
        f"`{report.interval}` bars · lookback **{report.lookback}** closed bar(s) · "
        f"band **{report._band_text()}** · {len(report.ok)}/{len(report.results)} symbols scanned"
        f" · source `{report.source}`"
    )
    lines.append("")
    lines.append(
        f"**{len(signals)} signal(s)** at the ≥{ACTIONABLE_THRESHOLD}/5 threshold · "
        f"{len(report.in_band_quiet)} in-band quiet · {len(report.out_of_band)} out of band · "
        f"{len(report.failures)} fetch failure(s)"
    )
    lines.append("")

    # ---- signals ---------------------------------------------------------- #
    if signals:
        lines.append("## Signals")
        lines.append("")
        lines.append("| Symbol | LTP ₹ | Setup | Rating | Context | Triggered |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for s in signals:
            hit = s.hit
            when = (
                "latest closed bar"
                if s.bars_ago == 0
                else f"{s.bars_ago} bar(s) ago ({hit.close_ts.astimezone(IST).strftime('%d %b %H:%M')} IST)"
            )
            lines.append(
                f"| **{s.symbol}** | {_fmt_price(hit.close)} | {hit.setup} | {hit.score}/5 "
                f"| {hit.context} | {when} |"
            )
        lines.append("")
    else:
        lines.append("## Signals")
        lines.append("")
        lines.append(f"No watchlist symbol scored ≥{ACTIONABLE_THRESHOLD}/5 while in band in this window.")
        lines.append("")

    # ---- full watchlist --------------------------------------------------- #
    by_score = sorted(report.ok, key=lambda r: (-(r.latest.score if r.latest else -1), r.symbol))
    if by_score:
        lines.append("## Watchlist")
        lines.append("")
        lines.append("| Symbol | Sector | LTP ₹ | Setup | Rating | Context | Band |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for s in by_score:
            latest = s.latest
            assert latest is not None
            band = "in" if latest.in_band else "**out**"
            setup = latest.setup if latest.in_band else "OUT OF RANGE"
            lines.append(
                f"| {s.symbol} | {s.sector} | {_fmt_price(latest.close)} | {setup} "
                f"| {_rating(s)} | {latest.context} | {band} |"
            )
        lines.append("")

    if report.out_of_band:
        names = ", ".join(f"{s.symbol} ({_fmt_price(s.latest.close)})" for s in report.out_of_band if s.latest)  # type: ignore[union-attr]
        lines.append(f"**Out of band ({len(report.out_of_band)}):** {names}")
        lines.append("")

    # ---- non-ok ------------------------------------------------------------ #
    problems = [r for r in report.results if r.status != "ok"]
    if problems or report.failures:
        lines.append("## Skipped")
        lines.append("")
        for r in problems:
            lines.append(f"- `{r.symbol}` — {r.status}: {r.error}")
        for symbol, err in report.failures:
            if not any(r.symbol == symbol for r in problems):
                lines.append(f"- `{symbol}` — fetch failed: {err}")
        lines.append("")

    if report.started_at and report.finished_at:
        elapsed = (report.finished_at - report.started_at).total_seconds()
        lines.append(f"<sub>Scan took {elapsed:.1f}s · started {_fmt_ist(report.started_at)}</sub>")
        lines.append("")

    return "\n".join(lines)


def csv_rows(report: ScanReport) -> List[dict]:
    """One row per scanned symbol, flat and audit-friendly."""
    rows: List[dict] = []
    scan_ts = report.finished_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for s in report.results:
        latest = s.latest
        hit = s.hit
        row = {column: "" for column in CSV_COLUMNS}
        row.update(
            scan_ts_utc=scan_ts,
            interval=report.interval,
            symbol=s.symbol,
            name=s.name,
            sector=s.sector,
            status=s.status,
            error=s.error or "",
        )
        if latest is not None:
            row.update(
                ltp=f"{latest.close:.2f}",
                score=latest.score,
                setup=latest.setup,
                context=latest.context,
                in_band="1" if latest.in_band else "0",
                box_breakout_long=int(latest.box_breakout_long),
                box_breakout_short=int(latest.box_breakout_short),
                sweep_long=int(latest.sweep_long),
                sweep_short=int(latest.sweep_short),
                strict_bull=int(latest.strict_bull),
                strict_bear=int(latest.strict_bear),
                chop=int(latest.chop),
                high_vol=int(latest.high_vol),
            )
        if hit is not None:
            row.update(
                signal_score=hit.score,
                signal_setup=hit.setup,
                signal_context=hit.context,
                signal_bars_ago=s.bars_ago,
                signal_bar_utc=hit.close_ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        rows.append(row)
    return rows


def append_csv(path: Path, rows: Sequence[dict]) -> None:
    """Append to the day's history file, writing a header only if it's new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def console_summary(report: ScanReport) -> str:
    """Short stdout summary for the Actions log."""
    signals = report.signals
    lines = [
        f"scanned      : {len(report.ok)}/{len(report.results)} symbols "
        f"({len(report.failures)} failure(s))",
        f"interval     : {report.interval}  lookback {report.lookback}",
        f"band         : {report._band_text()}  ({len(report.out_of_band)} out of band)",
        f"signals      : {len(signals)}",
    ]
    for s in signals:
        hit = s.hit
        age = "latest" if s.bars_ago == 0 else f"+{s.bars_ago} bars"
        lines.append(
            f"  {s.symbol:<14} {hit.score}/5  {hit.setup:<14} {hit.context:<9} "
            f"{hit.close:>10.2f}  ({age})"
        )
    return "\n".join(lines)
