"""Self-contained HTML dashboard for a scan.

The output is a single file with inline CSS and a little vanilla JS — no CDN, no
fonts, no frameworks — so it can be committed to ``results/``, attached to a
workflow artifact, or opened straight off disk. The styling deliberately echoes
the TradingView table (dark background, deep-blue title bar, green/red/grey
cells) so the dashboard and the chart read the same way.

The scan payload is also embedded as JSON in a ``<script>`` tag, which keeps the
page inspectable and lets the tests assert on the data rather than on markup.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from .datafeed import IST
from .engine import ACTIONABLE_THRESHOLD, SymbolScan
from .report import ScanReport

# Palette lifted from the indicator's table cells.
BG = "#0d1117"
PANEL = "#161b22"
TITLE_BG = "#0a2a5e"
HEAD_BG = "#1f3a93"
GREEN = "#16a34a"
RED = "#dc2626"
GREY = "#4b5563"
ORANGE = "#d97706"

STYLE = f"""
:root {{
  --bg: {BG}; --panel: {PANEL}; --title: {TITLE_BG}; --head: {HEAD_BG};
  --green: {GREEN}; --red: {RED}; --grey: {GREY}; --orange: {ORANGE};
  --line: #263041; --text: #e6edf3; --muted: #9aa7b4;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}}
a {{ color: #7cc4ff; }}
.wrap {{ max-width: 1400px; margin: 0 auto; padding: 0 16px 48px; }}

header.top {{
  background: var(--title); border-bottom: 1px solid var(--line);
  padding: 14px 16px; margin-bottom: 18px;
}}
header.top .inner {{ max-width: 1400px; margin: 0 auto; display: flex; flex-wrap: wrap; gap: 10px 18px; align-items: center; }}
.brand {{ font-size: 20px; font-weight: 700; letter-spacing: .5px; }}
.brand .ver {{ font-size: 12px; font-weight: 500; color: #b9d4ff; }}
.stamp {{ margin-left: auto; color: #cfe0ff; font-size: 12px; }}
.chip {{
  background: rgba(255,255,255,.10); border: 1px solid rgba(255,255,255,.18);
  border-radius: 999px; padding: 2px 10px; font-size: 12px; white-space: nowrap;
}}
.chip.sig {{ background: {GREEN}; border-color: {GREEN}; font-weight: 700; }}
.chip.sig.zero {{ background: rgba(255,255,255,.10); border-color: rgba(255,255,255,.18); font-weight: 500; }}

.stats {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 22px; }}
.stat {{
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 10px 14px; min-width: 128px;
}}
.stat b {{ display: block; font-size: 22px; line-height: 1.1; }}
.stat span {{ color: var(--muted); font-size: 12px; }}
.stat.warn b {{ color: var(--orange); }}

h2 {{ font-size: 16px; margin: 26px 0 10px; }}
h2 small {{ color: var(--muted); font-weight: 400; font-size: 12px; }}

.controls {{ display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: center; margin-bottom: 10px; }}
.controls input[type=text], .controls select {{
  background: var(--panel); color: var(--text); border: 1px solid var(--line);
  border-radius: 7px; padding: 6px 10px; font-size: 13px; min-width: 210px;
}}
.controls label {{ color: var(--muted); font-size: 13px; display: inline-flex; gap: 6px; align-items: center; }}
.controls .count {{ margin-left: auto; color: var(--muted); font-size: 12px; }}

table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }}
th, td {{ padding: 7px 10px; text-align: left; border-bottom: 1px solid var(--line); white-space: nowrap; }}
th {{ background: var(--head); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; cursor: pointer; user-select: none; }}
th.nosort {{ cursor: default; }}
th .arrow {{ opacity: .5; font-size: 10px; }}
td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
tr:last-child td {{ border-bottom: 0; }}
tbody tr:hover {{ background: #1c2431; }}

.sym {{ font-weight: 600; }}
.nm {{ color: var(--muted); font-size: 12px; }}
.score {{ font-variant-numeric: tabular-nums; font-weight: 600; }}
.dotbar {{ display: inline-block; width: 46px; height: 6px; background: #2b3546; border-radius: 3px; overflow: hidden; vertical-align: middle; margin-left: 7px; }}
.dotbar i {{ display: block; height: 100%; background: var(--green); }}
tr.short .dotbar i {{ background: var(--red); }}

.tag {{ border-radius: 5px; padding: 2px 7px; font-size: 12px; display: inline-block; }}
.tag.long {{ background: rgba(22,163,74,.20); color: #86efac; }}
.tag.short {{ background: rgba(220,38,38,.20); color: #fca5a5; }}
.tag.none {{ background: rgba(75,85,99,.28); color: #cbd5e1; }}
.tag.oor {{ background: rgba(217,119,6,.20); color: #fdba74; }}
.ctx {{ border-radius: 5px; padding: 2px 7px; font-size: 12px; background: rgba(75,85,99,.28); }}
.ctx.bull {{ background: rgba(22,163,74,.18); color: #86efac; }}
.ctx.bear {{ background: rgba(220,38,38,.18); color: #fca5a5; }}
.ctx.chop {{ background: rgba(217,119,6,.18); color: #fdba74; }}
.ctx.oor  {{ background: rgba(217,119,6,.24); color: #fdba74; font-weight: 600; }}
.price.{{}} .oor-text {{ color: var(--orange); }}
tr.oor {{ opacity: .72; }}
tr.oor .sym {{ color: #cbd5e1; }}

.empty {{ padding: 18px; color: var(--muted); background: var(--panel); border: 1px dashed var(--line); border-radius: 10px; }}
footer {{ margin-top: 34px; color: var(--muted); font-size: 12px; border-top: 1px solid var(--line); padding-top: 14px; }}
footer p {{ margin: 6px 0; }}
"""

SCRIPT = """
(function () {
  var q = document.getElementById('q');
  var onlySig = document.getElementById('only-sig');
  var onlyIn = document.getElementById('only-in');
  var sector = document.getElementById('sector');
  var table = document.getElementById('universe');
  var rows = table ? Array.prototype.slice.call(table.querySelectorAll('tbody tr')) : [];
  var count = document.getElementById('shown');

  function apply() {
    var term = (q && q.value || '').trim().toLowerCase();
    var wantSig = onlySig && onlySig.checked;
    var wantIn = onlyIn && onlyIn.checked;
    var wantSector = sector && sector.value;
    var shown = 0;
    rows.forEach(function (tr) {
      var ok = true;
      if (term && (tr.dataset.search || '').indexOf(term) === -1) ok = false;
      if (wantSig && tr.dataset.sig !== '1') ok = false;
      if (wantIn && tr.dataset.in !== '1') ok = false;
      if (wantSector && tr.dataset.sector !== wantSector) ok = false;
      tr.hidden = !ok;
      if (ok) shown++;
    });
    if (count) count.textContent = shown + ' of ' + rows.length + ' shown';
  }

  [q, onlySig, onlyIn, sector].forEach(function (el) {
    if (el) el.addEventListener('input', apply);
    if (el && el.tagName === 'SELECT') el.addEventListener('change', apply);
  });

  // Click a header to sort; numeric cells sort by their data-value.
  Array.prototype.forEach.call(document.querySelectorAll('th[data-key]'), function (th) {
    th.addEventListener('click', function () {
      var key = th.dataset.key;
      var numeric = th.dataset.type === 'num';
      var dir = th.dataset.dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
      var body = table.querySelector('tbody');
      rows.sort(function (a, b) {
        var av = a.dataset[key] || '', bv = b.dataset[key] || '';
        var r;
        if (numeric) {
          r = (parseFloat(av) || 0) - (parseFloat(bv) || 0);
        } else {
          r = av.localeCompare(bv);
        }
        if (r === 0) r = (a.dataset.symbol || '').localeCompare(b.dataset.symbol || '');
        return dir === 'asc' ? r : -r;
      });
      rows.forEach(function (tr) { body.appendChild(tr); });
      Array.prototype.forEach.call(document.querySelectorAll('th[data-key] .arrow'), function (a) {
        a.textContent = '';
      });
      var arrow = th.querySelector('.arrow');
      if (arrow) arrow.textContent = dir === 'asc' ? '▲' : '▼';
    });
  });

  apply();
})();
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _fmt_ist(moment: datetime) -> str:
    return moment.astimezone(IST).strftime("%d %b %Y, %H:%M IST")


def _fmt_price(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _ctx_class(context: str, in_band: bool) -> str:
    if not in_band:
        return "ctx oor"
    if context == "CHOP":
        return "ctx chop"
    if "BULL" in context:
        return "ctx bull"
    if "BEAR" in context:
        return "ctx bear"
    return "ctx"


def _setup_tag(setup: str, score: int, in_band: bool) -> str:
    if not in_band:
        return '<span class="tag oor">OUT OF RANGE</span>'
    if score < ACTIONABLE_THRESHOLD or setup == "None":
        return '<span class="tag none">—</span>'
    short = "SHORT" in setup or "BREAKDOWN" in setup
    return f'<span class="tag {"short" if short else "long"}">{_e(setup)}</span>'


def _score_cell(score: int, in_band: bool) -> str:
    if not in_band:
        return '<span class="score">—</span>'
    width = max(0, min(100, score * 20))
    return (
        f'<span class="score">{score}/5</span>'
        f'<span class="dotbar"><i style="width:{width}%"></i></span>'
    )


def _age_text(scan: SymbolScan) -> str:
    if not scan.has_signal:
        return "—"
    if scan.bars_ago == 0:
        return "latest bar"
    return f"+{scan.bars_ago} bars"


# --------------------------------------------------------------------------- #
# Payload + page
# --------------------------------------------------------------------------- #
def dashboard_payload(report: ScanReport) -> dict:
    """The scan as plain data — embedded in the page and asserted on by tests."""
    symbols = []
    for scan in report.results:
        latest = scan.latest
        symbols.append(
            {
                "symbol": scan.symbol,
                "name": scan.name,
                "sector": scan.sector,
                "status": scan.status,
                "error": scan.error or "",
                "ltp": None if latest is None else round(latest.close, 2),
                "score": None if latest is None else latest.score,
                "setup": None if latest is None else latest.setup,
                "context": None if latest is None else latest.context,
                "in_band": None if latest is None else latest.in_band,
                "signal_score": None if scan.hit is None else scan.hit.score,
                "signal_setup": None if scan.hit is None else scan.hit.setup,
                "bars_ago": scan.bars_ago,
            }
        )
    return {
        "generated_at": report.finished_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval": report.interval,
        "lookback": report.lookback,
        "band": {"low": report.min_price, "high": report.max_price},
        "source": report.source,
        "threshold": ACTIONABLE_THRESHOLD,
        "counts": {
            "universe": len(report.results),
            "scanned": len(report.ok),
            "signals": len(report.signals),
            "in_band": len(report.results) - len(report.out_of_band),
            "out_of_band": len(report.out_of_band),
            "failures": len(report.failures),
        },
        "symbols": symbols,
    }


def _rows(report: ScanReport) -> str:
    ordered = sorted(
        report.results,
        key=lambda r: (
            0 if r.has_signal else 1,
            -(r.hit.score if r.hit else (r.latest.score if r.latest else -1)),
            r.symbol,
        ),
    )
    out: List[str] = []
    for scan in ordered:
        latest = scan.latest
        in_band = bool(latest.in_band) if latest else False
        score = latest.score if latest else -1
        ok = scan.status == "ok"

        if not ok:
            setup = f'<span class="tag oor">{_e(scan.status)}</span>'
            context = f'<span class="ctx oor">{_e(scan.error or "no data")[:60]}</span>'
        else:
            setup = _setup_tag(latest.setup, score, in_band)
            context = f'<span class="{_ctx_class(latest.context, in_band)}">{_e(latest.context if in_band else "OUT OF BAND")}</span>'

        short = bool(latest and ("SHORT" in latest.setup or "BREAKDOWN" in latest.setup))
        cls = []
        if latest and not in_band:
            cls.append("oor")
        if short:
            cls.append("short")

        search = " ".join(
            [scan.symbol, scan.name, scan.sector, "" if latest is None else latest.setup]
        ).lower()

        out.append(
            "<tr class=\"{cls}\" data-symbol=\"{sym}\" data-search=\"{search}\" "
            "data-score=\"{score}\" data-in=\"{inb}\" data-sig=\"{sig}\" data-sector=\"{sector}\" "
            "data-scan=\"{sym}\" data-name=\"{name}\" data-ctx=\"{ctx}\" data-setup=\"{setup_s}\" "
            "data-price=\"{price}\" data-age=\"{age}\">"
            "<td><span class=\"sym\">{sym_short}</span><br><span class=\"nm\">{name}</span></td>"
            "<td>{sector_html}</td>"
            "<td class=\"num\">{price}</td>"
            "<td>{setup}</td>"
            "<td class=\"num\">{score_cell}</td>"
            "<td>{context}</td>"
            "<td class=\"num\">{age}</td>"
            "</tr>".format(
                cls=" ".join(cls),
                sym=_e(scan.symbol),
                sym_short=_e(scan.symbol.split(":")[-1]),
                name=_e(scan.name),
                sector=_e(scan.sector),
                sector_html=_e(scan.sector),
                search=_e(search),
                score=score,
                inb=1 if in_band else 0,
                sig=1 if scan.has_signal else 0,
                price=_fmt_price(latest.close if latest else None),
                setup=setup,
                setup_s=_e(latest.setup if latest else scan.status),
                score_cell=_score_cell(score, in_band),
                context=context,
                ctx=_e(latest.context if latest else scan.status),
                age=_age_text(scan),
            )
        )
    return "\n".join(out)


def render_dashboard(report: ScanReport, *, title: str = "IronMan F&O scan") -> str:
    """Render the whole dashboard as one self-contained HTML document."""
    payload = dashboard_payload(report)
    counts = payload["counts"]
    signals = report.signals
    sectors = sorted({r.sector for r in report.ok if r.sector})

    # ---- signals table ---------------------------------------------------- #
    if signals:
        body = []
        for scan in signals:
            hit = scan.hit
            short = "SHORT" in hit.setup or "BREAKDOWN" in hit.setup
            body.append(
                f"<tr class=\"{'short' if short else ''}\">"
                f"<td><span class=\"sym\">{_e(scan.symbol.split(':')[-1])}</span><br>"
                f"<span class=\"nm\">{_e(scan.name)}</span></td>"
                f"<td class=\"num\">{_fmt_price(hit.close)}</td>"
                f"<td>{_setup_tag(hit.setup, hit.score, True)}</td>"
                f"<td class=\"num\">{_score_cell(hit.score, True)}</td>"
                f"<td><span class=\"{_ctx_class(hit.context, True)}\">{_e(hit.context)}</span></td>"
                f"<td class=\"num\">{_age_text(scan)}</td>"
                f"</tr>"
            )
        signals_table = (
            "<table id=\"signals\"><thead><tr>"
            "<th class=\"nosort\">Symbol</th><th class=\"nosort\">LTP ₹</th>"
            "<th class=\"nosort\">Setup</th><th class=\"nosort\">Rating</th>"
            "<th class=\"nosort\">Context</th><th class=\"nosort\">Triggered</th>"
            "</tr></thead><tbody>" + "\n".join(body) + "</tbody></table>"
        )
    else:
        signals_table = (
            f'<p class="empty">No watchlist symbol scored ≥{ACTIONABLE_THRESHOLD}/5 '
            f"while in band in this window.</p>"
        )

    # ---- failures --------------------------------------------------------- #
    failures = [r for r in report.results if r.status != "ok"]
    failure_block = ""
    if failures:
        items = "\n".join(
            f"<li><code>{_e(r.symbol)}</code> — {_e(r.status)}: {_e(r.error or '')}</li>"
            for r in failures[:40]
        )
        if len(failures) > 40:
            items += f"\n<li>… and {len(failures) - 40} more</li>"
        failure_block = f"<h2>Skipped <small>{len(failures)}</small></h2><ul>{items}</ul>"

    sector_options = "\n".join(
        f'<option value="{_e(s)}">{_e(s)}</option>' for s in sectors
    )

    band = payload["band"]
    sig_chip = (
        f'<span class="chip sig">SIG {counts["signals"]}</span>'
        if counts["signals"]
        else f'<span class="chip sig zero">SIG 0</span>'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} — {_e(_fmt_ist(report.finished_at))}</title>
<style>{STYLE}</style>
</head>
<body>
<header class="top">
  <div class="inner">
    <div class="brand">IRONMAN <span class="ver">v16.2 · F&amp;O scanner</span></div>
    <span class="chip">{_e(report.interval)} bars</span>
    <span class="chip">lookback {report.lookback}</span>
    <span class="chip">₹{band['low']:,.0f} – ₹{band['high']:,.0f}</span>
    {sig_chip}
    <span class="chip">{counts['scanned']}/{counts['universe']} scanned</span>
    <span class="stamp">{_e(_fmt_ist(report.finished_at))}</span>
  </div>
</header>

<div class="wrap">
  <section class="stats">
    <div class="stat"><b>{counts['signals']}</b><span>signals ≥{payload['threshold']}/5</span></div>
    <div class="stat"><b>{counts['in_band']}</b><span>in band</span></div>
    <div class="stat"><b>{counts['out_of_band']}</b><span>out of band</span></div>
    <div class="stat{' warn' if counts['failures'] else ''}"><b>{counts['failures']}</b><span>fetch failure(s)</span></div>
    <div class="stat"><b>{(report.finished_at - report.started_at).total_seconds():.1f}s</b><span>scan time</span></div>
  </section>

  <h2>Signals <small>score ≥ {payload['threshold']}/5 while in band — freshest first</small></h2>
  {signals_table}

  <h2>Full F&amp;O universe <small>{counts['universe']} symbols · click a header to sort</small></h2>
  <div class="controls">
    <input type="text" id="q" placeholder="Filter symbol, name, sector or setup…" autocomplete="off">
    <label><input type="checkbox" id="only-sig"> signals only</label>
    <label><input type="checkbox" id="only-in"> in band only</label>
    <select id="sector"><option value="">All sectors</option>{sector_options}</select>
    <span class="count" id="shown"></span>
  </div>
  <table id="universe">
    <thead><tr>
      <th data-key="sym" data-type="text">Symbol <span class="arrow"></span></th>
      <th data-key="sector" data-type="text">Sector <span class="arrow"></span></th>
      <th data-key="price" data-type="num">LTP ₹ <span class="arrow"></span></th>
      <th data-key="setup" data-type="text">Setup <span class="arrow"></span></th>
      <th data-key="score" data-type="num">Rating <span class="arrow"></span></th>
      <th data-key="ctx" data-type="text">Context <span class="arrow"></span></th>
      <th data-key="age" data-type="text">Triggered <span class="arrow"></span></th>
    </tr></thead>
    <tbody>
{_rows(report)}
    </tbody>
  </table>

  {failure_block}

  <footer>
    <p><strong>Source:</strong> {_e(report.source)} bars · interval {_e(report.interval)} ·
       lookback {report.lookback} closed bars · band ₹{band['low']:,.0f}–₹{band['high']:,.0f} ·
       generated {_e(_fmt_ist(report.finished_at))}</p>
    <p>Scanning and visualisation tool, not investment advice. Signals come from price-action
       statistics on historical bars and will produce false positives, especially in CHOP regimes.
       The engine is a port of the Pine v16.2 indicator and has not been diffed against TradingView —
       confirm on a chart before acting. See <code>docs/SCANNER.md</code>.</p>
  </footer>
</div>

<script type="application/json" id="scan-data">{html.escape(json.dumps(payload, separators=(',', ':')))}</script>
<script>{SCRIPT}</script>
</body>
</html>
"""
