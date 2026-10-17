"""Command-line entry point: ``python ttpnl.py <command>`` (or ``python -m tradetron_pnl``).

Commands
--------
run       fetch -> snapshot -> ledger -> Telegram -> reports (what the workflow calls)
probe     call the API once and print the raw (key-redacted) payload; optionally save a fixture
report    regenerate reports/ from stored data, no network
tax       write the tax workings for a financial year
send      send a test message to the Telegram chat
import    load an external day-wise P&L CSV into the ledger
rebuild   recompute the whole ledger from snapshots (e.g. after changing the P&L semantics)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .analytics import by_deployment, by_month, daily_totals, period_stats, select_rows
from .api import (
    ApiError, Deployment, FixtureClient, TradetronClient, normalise_deployments,
    normalise_positions, position_summary,
)
from .config import Settings, load_deployment_tags, settings_from_env, tag_for
from .ledger import import_rows, rebuild_ledger, update_ledger_for_day
from .messages import (
    DeploymentView, RunView, fmt_money, render_eod, render_error_alert, render_fetch_failure,
    render_hourly, render_monthly, render_preopen, render_quarter, render_weekly,
)
from .reports import render_dashboard, render_markdown
from .store import DataStore, Snapshot
from .tax import build_tax_report, render_tax_markdown, tax_csv
from .telegram import TelegramBot, TelegramError
from .trading_calendar import (
    CLOSED, IST, OPEN, POST_CLOSE, PRE_OPEN, UTC, Holidays, fy_bounds, fy_label, fy_quarter,
    holiday_coverage, is_last_trading_day_of_month, is_last_trading_day_of_quarter,
    is_last_trading_day_of_week, load_holidays, month_bounds, next_advance_tax_due,
    now_ist, parse_when, quarter_bounds, session_phase, week_bounds,
)

ROOT = Path(__file__).resolve().parents[1]

KINDS = ("auto", "preopen", "hourly", "eod", "silent")

EXIT_OK = 0
EXIT_FETCH_FAILED = 1
EXIT_CONFIG = 2
EXIT_TELEGRAM_FAILED = 3


@dataclass
class Paths:
    root: Path
    data: Path
    reports: Path
    config: Path
    env_file: Path

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Paths":
        root = Path(args.root).resolve() if getattr(args, "root", None) else ROOT
        return cls(
            root=root,
            data=Path(args.data_dir) if args.data_dir else root / "data",
            reports=Path(args.reports_dir) if args.reports_dir else root / "reports",
            config=Path(args.config_dir) if args.config_dir else root / "config",
            env_file=Path(args.env_file) if args.env_file else root / ".env",
        )

    @property
    def holidays(self) -> Path:
        return self.config / "holidays_nse.csv"

    @property
    def deployments(self) -> Path:
        return self.config / "deployments.csv"


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ttpnl",
        description="Fetch, store, analyse and broadcast a personal Tradetron P&L.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version", version=f"tradetron-pnl {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=None, help="project root (default: the repo this file lives in)")
    common.add_argument("--data-dir", default=None, help="where snapshots/ledger live (default: <root>/data)")
    common.add_argument("--reports-dir", default=None, help="where reports are written (default: <root>/reports)")
    common.add_argument("--config-dir", default=None, help="holidays + deployment tags (default: <root>/config)")
    common.add_argument("--env-file", default=None, help=".env to read secrets from (default: <root>/.env)")
    common.add_argument("--quiet", action="store_true", help="only print the summary line (dry-run messages are still shown)")
    common.add_argument("--verbose", action="store_true", help="print per-deployment detail")

    source = argparse.ArgumentParser(add_help=False)
    source.add_argument("--source", choices=("api", "fixture"), default="api",
                        help="where deployments come from (default: api)")
    source.add_argument("--fixture", default=None, help="fixture JSON for --source fixture")
    source.add_argument("--now", default=None,
                        help="override the clock, ISO-8601 (naive = IST); for tests and backfills")

    sub = ap.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common, source], help="fetch, store, notify, report")
    run.add_argument("--kind", choices=KINDS, default="auto",
                     help="which update to send; auto picks from the IST clock (default: auto)")
    run.add_argument("--dry-run", action="store_true", help="print Telegram messages instead of sending")
    run.add_argument("--no-telegram", action="store_true", help="store and report only, send nothing")
    run.add_argument("--no-positions", action="store_true", help="skip the per-deployment positions calls")
    run.add_argument("--no-reports", action="store_true", help="skip writing reports/")
    run.add_argument("--force", action="store_true", help="run even on a weekend or exchange holiday")

    probe = sub.add_parser("probe", parents=[common, source], help="print the raw API payload (key redacted)")
    probe.add_argument("--positions", type=int, default=1, metavar="N",
                       help="also fetch positions for the first N deployments (default: 1)")
    probe.add_argument("--notifications", action="store_true", help="also fetch /me/notifications")
    probe.add_argument("--save-fixture", default=None, metavar="PATH",
                       help="write what was fetched as a fixture JSON usable with --source fixture")

    report = sub.add_parser("report", parents=[common], help="rebuild reports/ from stored data")
    report.add_argument("--now", default=None, help="report as of this time (default: now)")

    tax = sub.add_parser("tax", parents=[common], help="write the tax workings for a financial year")
    tax.add_argument("--fy", default=None, help="e.g. FY2026-27 (default: current)")
    tax.add_argument("--as-of", default=None, help="date for the advance-tax status (default: today)")
    tax.add_argument("--rate", type=float, default=None,
                     help="flat effective tax rate for an indicative figure, e.g. 0.30")
    tax.add_argument("--stdout", action="store_true", help="print the markdown instead of writing files")

    send = sub.add_parser("send", parents=[common], help="send a test message to Telegram")
    send.add_argument("--text", default=None, help="message text (HTML allowed); default: a test line")

    imp = sub.add_parser("import", parents=[common], help="import a day-wise P&L CSV into the ledger")
    imp.add_argument("csv", help="CSV with at least date and pnl columns")
    imp.add_argument("--date-col", default="date")
    imp.add_argument("--pnl-col", default="pnl")
    imp.add_argument("--name-col", default="name")
    imp.add_argument("--id-col", default="deployment_id")
    imp.add_argument("--currency", default=None, help="currency for rows that do not carry one")

    rebuild = sub.add_parser("rebuild", parents=[common], help="recompute the ledger from snapshots")
    rebuild.add_argument("--drop-imported", action="store_true", help="also discard imported rows")
    return ap


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
class Console:
    def __init__(self, quiet: bool = False, verbose: bool = False, out=None, err=None):
        self.quiet = quiet
        self.verbose = verbose
        self.out = out or sys.stdout
        self.err = err or sys.stderr

    def info(self, text: str) -> None:
        if not self.quiet:
            print(text, file=self.out)

    def detail(self, text: str) -> None:
        if self.verbose and not self.quiet:
            print(text, file=self.out)

    def warn(self, text: str) -> None:
        print(f"warning: {text}", file=self.err)

    def always(self, text: str) -> None:
        print(text, file=self.out)


def _settings(paths: Paths) -> Settings:
    return settings_from_env(dotenv=paths.env_file)


def _clock(args: argparse.Namespace) -> datetime:
    if getattr(args, "now", None):
        return parse_when(args.now)
    return now_ist()


def _client(args: argparse.Namespace, settings: Settings):
    if args.source == "fixture":
        if not args.fixture:
            raise SystemExit("ERROR: --source fixture requires --fixture <file.json>")
        return FixtureClient(Path(args.fixture))
    return TradetronClient(settings.api_key, base_url=settings.base_url, timeout=settings.timeout,
                           deployments_path=settings.deployments_path,
                           positions_path=settings.positions_path,
                           notifications_path=settings.notifications_path)


def resolve_kind(requested: str, phase: str, state: dict, day: date) -> str:
    if requested != "auto":
        return requested
    if phase == PRE_OPEN:
        return "preopen"
    if phase == OPEN:
        return "hourly"
    if phase == POST_CLOSE:
        return "silent" if state.get("last_eod_day") == day.isoformat() else "eod"
    return "silent"


def _deployments_from_snapshots(snapshots: Sequence[Snapshot]) -> List[Deployment]:
    latest: Dict[int, Snapshot] = {}
    for snap in snapshots:
        latest[snap.deployment_id] = snap
    out: List[Deployment] = []
    for snap in latest.values():
        out.append(Deployment(
            deployment_id=snap.deployment_id, name=snap.name, deployment_type=snap.deployment_type,
            status=snap.status, broker=snap.broker, multiplier=snap.multiplier, capital=snap.capital,
            pnl=snap.pnl, pnl_known=snap.pnl_known, open_positions=snap.open_positions,
            total_positions=snap.total_positions, currency=snap.currency, deployed_on=snap.deployed_on,
        ))
    out.sort(key=lambda d: d.deployment_id)
    return out


def build_view(*, now: datetime, phase: str, kind: str, source: str, settings: Settings,
               tags: dict, deployments: Sequence[Deployment], ledger: Sequence,
               snapshots_today: Sequence[Snapshot], state: dict,
               notes: Optional[List[str]] = None) -> Tuple[RunView, list]:
    """Assemble the view model shared by messages, markdown and the dashboard."""
    day = now.date()
    fy = fy_label(day)
    cur = settings.reporting_currency
    today_rows = {r.deployment_id: r for r in ledger if r.trading_day == day}

    # previous-run delta per deployment, from today's snapshots
    series: Dict[int, List[Snapshot]] = {}
    for snap in sorted(snapshots_today, key=lambda s: s.ts_utc):
        series.setdefault(snap.deployment_id, []).append(snap)

    views: List[DeploymentView] = []
    for dep in deployments:
        tag = tag_for(dep.deployment_id, tags, settings.default_segment)
        row = today_rows.get(dep.deployment_id)
        snaps = series.get(dep.deployment_id, [])
        delta = round(snaps[-1].pnl - snaps[-2].pnl, 2) if len(snaps) >= 2 else None
        views.append(DeploymentView(
            deployment=dep, tag=tag,
            day_pnl=row.day_pnl if row else 0.0,
            baseline_source=row.baseline_source if row else "",
            counted=tag.include and not dep.paper and dep.currency == cur,
            hour_delta=delta,
        ))

    counted = [v for v in views if v.counted]
    today_total = round(sum(v.day_pnl for v in counted), 2)
    deltas = [v.hour_delta for v in counted if v.hour_delta is not None]
    hour_delta = round(sum(deltas), 2) if deltas else None
    paper_views = [v for v in views if v.deployment.paper and v.currency == cur]
    paper_total = round(sum(v.day_pnl for v in paper_views), 2) if paper_views else None
    other: Dict[str, float] = {}
    for v in views:
        if v.tag.include and not v.deployment.paper and v.currency != cur:
            other[v.currency] = round(other.get(v.currency, 0.0) + v.day_pnl, 2)
    open_positions = sum(v.deployment.open_positions or 0 for v in views
                         if v.tag.include and not v.deployment.paper)

    selected = select_rows(ledger, tags, currency=cur, default_segment=settings.default_segment)
    daily = daily_totals(selected)
    fy_start, fy_end = fy_bounds(fy)
    q_start, q_end = quarter_bounds(fy, fy_quarter(day))
    m_start, _ = month_bounds(day)
    w_start, _ = week_bounds(day)

    alerted = set(state.get("error_alerted", {}).get(day.isoformat(), []))
    new_errors = [v for v in views if v.deployment.in_error and v.deployment.deployment_id not in alerted]

    view = RunView(
        now=now, day=day, phase=phase, kind=kind, fy=fy, currency=cur, source=source,
        deployments=views, today_total=today_total, hour_delta=hour_delta,
        paper_total=paper_total, other_currency_totals=other, open_positions=open_positions,
        week=period_stats(daily, w_start, day),
        mtd=period_stats(daily, m_start, day),
        quarter=period_stats(daily, q_start, min(q_end, day)),
        fytd=period_stats(daily, fy_start, min(fy_end, day)),
        month_deployments=by_deployment(
            [r for r in selected if m_start <= r.trading_day <= day], tags, settings.default_segment),
        months=by_month([(d, v) for d, v in daily if fy_start <= d <= day]),
        next_due=next_advance_tax_due(day),
        new_errors=new_errors,
        notes=list(notes or []),
    )
    fy_deployments = by_deployment(
        [r for r in selected if fy_start <= r.trading_day <= day], tags, settings.default_segment)
    return view, fy_deployments


def compose_messages(view: RunView, kind: str, holidays: Holidays, settings: Settings) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    if view.new_errors:
        out.append(("error", render_error_alert(view)))
    if kind == "preopen":
        if settings.preopen_message:
            out.append(("preopen", render_preopen(view)))
    elif kind == "hourly":
        out.append(("hourly", render_hourly(view)))
    elif kind == "eod":
        out.append(("eod", render_eod(view)))
        if is_last_trading_day_of_week(view.day, holidays):
            out.append(("weekly", render_weekly(view)))
        if is_last_trading_day_of_month(view.day, holidays):
            out.append(("monthly", render_monthly(view)))
        if is_last_trading_day_of_quarter(view.day, holidays):
            out.append(("quarter", render_quarter(view)))
    return out


def write_reports(paths: Paths, view: RunView, fy_deployments: list, ledger: Sequence,
                  tags: dict, settings: Settings, console: Console) -> List[Path]:
    paths.reports.mkdir(parents=True, exist_ok=True)
    selected = select_rows(ledger, tags, currency=settings.reporting_currency,
                           default_segment=settings.default_segment)
    recent = daily_totals(selected)
    written: List[Path] = []

    md = paths.reports / "latest.md"
    md.write_text(render_markdown(view, recent, fy_deployments), encoding="utf-8")
    written.append(md)

    dash = paths.reports / "dashboard.html"
    dash.write_text(render_dashboard(view, fy_deployments, recent), encoding="utf-8")
    written.append(dash)

    summary = paths.reports / "latest.json"
    summary.write_text(json.dumps({
        "generated_ist": view.now.isoformat(timespec="seconds"),
        "trading_day": view.day.isoformat(),
        "kind": view.kind,
        "source": view.source,
        "currency": view.currency,
        "today": view.today_total,
        "week": view.week.net,
        "mtd": view.mtd.net,
        "quarter": view.quarter.net,
        "fytd": view.fytd.net,
        "fy": view.fy,
        "active": len(view.active),
        "errors": [d.deployment.deployment_id for d in view.errors],
        "deployments": [
            {"id": d.deployment.deployment_id, "label": d.label, "status": d.deployment.status,
             "day_pnl": d.day_pnl, "api_pnl": d.deployment.pnl, "currency": d.currency,
             "counted": d.counted, "paper": d.deployment.paper}
            for d in view.deployments
        ],
    }, indent=2), encoding="utf-8")
    written.append(summary)

    written.extend(write_tax_reports(paths, ledger, tags, settings, view.fy, view.day))
    for path in written:
        console.detail(f"wrote {path}")
    return written


def write_tax_reports(paths: Paths, ledger: Sequence, tags: dict, settings: Settings, fy: str,
                      as_of: date, rate: Optional[float] = None) -> List[Path]:
    tax_dir = paths.reports / "tax"
    tax_dir.mkdir(parents=True, exist_ok=True)
    report = build_tax_report(ledger, fy, tags, as_of=as_of, currency=settings.reporting_currency,
                              default_segment=settings.default_segment, rate=rate)
    md = tax_dir / f"{fy}.md"
    md.write_text(render_tax_markdown(report), encoding="utf-8")
    csv_path = tax_dir / f"{fy}.csv"
    csv_path.write_text(tax_csv(ledger, fy, tags, currency=settings.reporting_currency,
                                default_segment=settings.default_segment), encoding="utf-8")
    return [md, csv_path]


def _fetch_positions(client, deployments: Sequence[Deployment], settings: Settings, now: datetime,
                     console: Console, sleep: Callable[[float], None] = time.sleep) -> Tuple[List[dict], List[str]]:
    """Positions for deployments that may have any. Failures become notes, never exceptions."""
    records: List[dict] = []
    notes: List[str] = []
    candidates = [d for d in deployments if not d.finished and d.open_positions != 0]
    skipped = len(candidates) - min(len(candidates), settings.max_positions)
    if skipped > 0:
        notes.append(f"positions fetched for {settings.max_positions} of {len(candidates)} deployments (TTPNL_MAX_POSITIONS)")
    for index, dep in enumerate(candidates[: settings.max_positions]):
        if index and settings.pace > 0:
            sleep(settings.pace)
        try:
            payload = client.deployment_positions(dep.deployment_id)
        except ApiError as exc:
            notes.append(f"positions {dep.deployment_id}: {exc}")
            continue
        pset = normalise_positions(payload, dep.deployment_id)
        summary = position_summary(pset.positions)
        records.append({
            "ts_utc": now.astimezone(UTC).isoformat(timespec="seconds"),
            "ts_ist": now.astimezone(IST).isoformat(timespec="seconds"),
            "deployment_id": pset.deployment_id,
            "name": dep.name,
            "positions_hidden": pset.positions_hidden,
            "as_of": pset.as_of,
            "count": pset.count,
            "summary": summary,
            "positions": pset.positions,
        })
        console.detail(f"positions {dep.deployment_id} {dep.label}: {pset.count} record(s)"
                       + (" (hidden)" if pset.positions_hidden else ""))
    return records, notes


def _prune_state(state: dict, day: date, keep_days: int = 10) -> None:
    alerted = state.get("error_alerted", {})
    cutoff = date.fromordinal(day.toordinal() - keep_days).isoformat()
    state["error_alerted"] = {k: v for k, v in alerted.items() if k >= cutoff}


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_run(args: argparse.Namespace) -> int:
    console = Console(args.quiet, args.verbose)
    paths = Paths.from_args(args)
    try:
        settings = _settings(paths)
    except ValueError as exc:
        console.warn(str(exc))
        return EXIT_CONFIG
    holidays = load_holidays(paths.holidays)
    tags = load_deployment_tags(paths.deployments)
    store = DataStore(paths.data)
    state = store.load_state()

    now = _clock(args)
    day = now.date()
    phase = session_phase(now, holidays)
    notes: List[str] = []

    if phase == CLOSED and not args.force:
        reason = holidays.get(day, "weekend")
        console.always(f"{day:%a %d %b %Y}: market closed ({reason}); nothing to do. Use --force to run anyway.")
        return EXIT_OK
    if not holiday_coverage(holidays, day.year):
        notes.append(f"config/holidays_nse.csv has no entries for {day.year}; add the NSE list or the bot will run on holidays")

    kind = resolve_kind(args.kind, phase, state, day)
    console.info(f"{now:%d %b %Y %H:%M IST} · phase {phase} · update kind {kind}")

    # -- fetch --------------------------------------------------------------- #
    try:
        client = _client(args, settings)
        raw_deployments = client.list_deployments()
    except ApiError as exc:
        return _fetch_failed(exc, now, store, state, settings, args, console)
    except SystemExit:
        raise
    except Exception as exc:                                              # noqa: BLE001
        return _fetch_failed(ApiError(f"{type(exc).__name__}: {exc}"), now, store, state, settings, args, console)

    deployments, problems = normalise_deployments(raw_deployments)
    notes.extend(problems)
    if not deployments:
        notes.append("the API returned no deployments")

    # -- store ---------------------------------------------------------------- #
    source = client.source
    if deployments:
        snapshots = [Snapshot.from_deployment(d, now=now, phase=phase, run_kind=kind, source=source)
                     for d in deployments]
        store.append_snapshots(snapshots)
        new_ids = store.update_registry(deployments, now)
        if new_ids:
            notes.append(f"new deployment id(s) seen: {', '.join(map(str, new_ids))} — tag them in config/deployments.csv")
    if settings.fetch_positions and not args.no_positions and deployments:
        records, pos_notes = _fetch_positions(
            client, deployments, settings, now, console,
            sleep=time.sleep if client.source == "api" else (lambda _s: None),
        )
        notes.extend(pos_notes)
        store.append_positions(day, records)

    update_ledger_for_day(store, day, holidays, settings.pnl_semantics)
    ledger = store.read_ledger()

    # -- view / messages -------------------------------------------------------- #
    view, fy_deployments = build_view(
        now=now, phase=phase, kind=kind, source=source, settings=settings, tags=tags,
        deployments=deployments, ledger=ledger, snapshots_today=store.snapshots_for_day(day),
        state=state, notes=notes,
    )
    messages = compose_messages(view, kind, holidays, settings)
    telegram_rc = _deliver(messages, view, kind, state, settings, args, console)

    # -- reports / state --------------------------------------------------------- #
    if not args.no_reports:
        write_reports(paths, view, fy_deployments, ledger, tags, settings, console)
    _prune_state(state, day)
    state["last_run_ist"] = now.isoformat(timespec="seconds")
    state["last_kind"] = kind
    state["last_source"] = source
    store.save_state(state)

    _console_summary(view, messages, console)
    return telegram_rc


def _fetch_failed(exc: ApiError, now: datetime, store: DataStore, state: dict, settings: Settings,
                  args: argparse.Namespace, console: Console) -> int:
    console.warn(f"fetch failed: {exc}")
    day = now.date().isoformat()
    text = render_fetch_failure(str(exc), now)
    if state.get("fetch_failure_alert_day") != day:
        if _send_texts([("fetch_failure", text)], settings, args, console) == EXIT_OK:
            state["fetch_failure_alert_day"] = day
    else:
        console.info("fetch-failure alert already sent today; not repeating")
    state["last_run_ist"] = now.isoformat(timespec="seconds")
    state["last_error"] = str(exc)
    store.save_state(state)
    console.always(f"FAILED · {exc}")
    return EXIT_FETCH_FAILED


def _send_texts(messages: Sequence[Tuple[str, str]], settings: Settings, args: argparse.Namespace,
                console: Console) -> int:
    if not messages:
        return EXIT_OK
    dry = getattr(args, "dry_run", False) or getattr(args, "no_telegram", False)
    if dry or not settings.telegram_configured:
        if not dry and not settings.telegram_configured:
            console.warn("Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID); printing instead")
        for label, text in messages:
            console.always(f"--- telegram [{label}] ---\n{text}\n")
        return EXIT_OK
    bot = TelegramBot(settings.telegram_token, settings.telegram_chat_id, timeout=settings.timeout)
    rc = EXIT_OK
    for label, text in messages:
        try:
            ids = bot.send(text, silent=(label in ("hourly", "preopen") and settings.silent_hourly))
            console.info(f"telegram [{label}] sent ({len(ids)} message(s))")
        except TelegramError as exc:
            console.warn(f"telegram [{label}] failed: {exc}")
            print(f"::warning title=Telegram::{label}: {exc}", file=console.err)
            rc = EXIT_TELEGRAM_FAILED
    return rc


def _deliver(messages: List[Tuple[str, str]], view: RunView, kind: str, state: dict, settings: Settings,
             args: argparse.Namespace, console: Console) -> int:
    rc = _send_texts(messages, settings, args, console)
    really_sent = rc == EXIT_OK and messages and not (args.dry_run or args.no_telegram) and settings.telegram_configured
    if not really_sent:
        return rc
    day = view.day.isoformat()
    labels = {label for label, _ in messages}
    if "eod" in labels:
        state["last_eod_day"] = day
    if "weekly" in labels:
        state["last_weekly_day"] = day
    if "monthly" in labels:
        state["last_monthly_day"] = day
    if "quarter" in labels:
        state["last_quarter_day"] = day
    if "error" in labels:
        alerted = state.setdefault("error_alerted", {}).setdefault(day, [])
        for v in view.new_errors:
            if v.deployment.deployment_id not in alerted:
                alerted.append(v.deployment.deployment_id)
    state["last_message_kind"] = kind
    return rc


def _console_summary(view: RunView, messages: Sequence[Tuple[str, str]], console: Console) -> None:
    cur = view.currency
    console.always(
        f"{view.day} {view.kind:<7} today {fmt_money(view.today_total, cur, sign=True):>12} · "
        f"MTD {fmt_money(view.mtd.net, cur, sign=True)} · FYTD {fmt_money(view.fytd.net, cur, sign=True)} · "
        f"{len(view.deployments)} deployment(s), {len(view.active)} active, {len(view.errors)} in error · "
        f"messages: {', '.join(l for l, _ in messages) or 'none'}"
    )
    for v in sorted(view.deployments, key=lambda d: (-d.day_pnl, d.label)):
        console.detail(f"  {v.deployment.deployment_id:>8}  {v.label:<28} {fmt_money(v.day_pnl, v.currency, sign=True):>12}"
                       f"  {v.deployment.status:<18} {'' if v.counted else '(not counted)'}")
    for note in view.notes:
        console.info(f"note: {note}")


def cmd_probe(args: argparse.Namespace) -> int:
    console = Console(args.quiet, args.verbose)
    paths = Paths.from_args(args)
    settings = _settings(paths)
    try:
        client = _client(args, settings)
        raw = client.list_deployments()
    except ApiError as exc:
        console.always(f"FAILED · {exc}")
        return EXIT_FETCH_FAILED
    deployments, problems = normalise_deployments(raw)
    console.always(f"{len(raw)} deployment record(s) from {client.source}")
    for problem in problems:
        console.warn(problem)
    if raw:
        console.always("first deployment record (raw):")
        console.always(json.dumps(raw[0], indent=2, ensure_ascii=False, default=str))
    for dep in deployments:
        console.always(f"  {dep.deployment_id:>8}  {dep.label:<30} type={dep.deployment_type or '-':<14} "
                       f"status={dep.status or '-':<18} pnl={dep.pnl:>12.2f} {dep.currency} "
                       f"open={dep.open_positions if dep.open_positions is not None else '-'}"
                       f"{'  [paper]' if dep.paper else ''}")
    positions: Dict[str, dict] = {}
    for dep in deployments[: max(0, args.positions)]:
        try:
            payload = client.deployment_positions(dep.deployment_id)
        except ApiError as exc:
            console.warn(f"positions {dep.deployment_id}: {exc}")
            continue
        positions[str(dep.deployment_id)] = payload
        console.always(f"positions for {dep.deployment_id} ({dep.label}):")
        console.always(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    notifications = None
    if args.notifications:
        try:
            notifications = client.notifications()
            console.always("notifications:")
            console.always(json.dumps(notifications, indent=2, ensure_ascii=False, default=str)[:4000])
        except ApiError as exc:
            console.warn(f"notifications: {exc}")
    if args.save_fixture:
        target = Path(args.save_fixture)
        target.parent.mkdir(parents=True, exist_ok=True)
        fixture = {"deployments": raw, "positions": positions}
        if notifications is not None:
            fixture["notifications"] = notifications
        target.write_text(json.dumps(fixture, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        console.always(f"fixture written to {target}")
    console.always(
        "\nNext: compare 'pnl' above with the Deployed page. If it equals the *Today* figure, set "
        "TTPNL_PNL_SEMANTICS=today; if it equals the running P&L since deployment, keep 'cumulative'."
    )
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    console = Console(args.quiet, args.verbose)
    paths = Paths.from_args(args)
    settings = _settings(paths)
    holidays = load_holidays(paths.holidays)
    tags = load_deployment_tags(paths.deployments)
    store = DataStore(paths.data)
    now = _clock(args)
    days = [d for d in store.snapshot_days() if d <= now.date()]
    if not days:
        console.always("no snapshots stored yet; run 'ttpnl run' first")
        return EXIT_CONFIG
    day = days[-1]
    snapshots = store.snapshots_for_day(day)
    as_of = now if day == now.date() else datetime.combine(day, datetime.min.time(), tzinfo=IST).replace(hour=15, minute=30)
    phase = session_phase(as_of, holidays)
    deployments = _deployments_from_snapshots(snapshots)
    ledger = store.read_ledger()
    view, fy_deployments = build_view(
        now=as_of, phase=phase, kind="report", source="stored", settings=settings, tags=tags,
        deployments=deployments, ledger=ledger, snapshots_today=snapshots, state=store.load_state(),
    )
    written = write_reports(paths, view, fy_deployments, ledger, tags, settings, console)
    console.always(f"reports rebuilt as of {day} from {len(snapshots)} snapshot row(s): "
                   + ", ".join(str(p.relative_to(paths.root)) if p.is_relative_to(paths.root) else str(p) for p in written))
    return EXIT_OK


def cmd_tax(args: argparse.Namespace) -> int:
    console = Console(args.quiet, args.verbose)
    paths = Paths.from_args(args)
    settings = _settings(paths)
    tags = load_deployment_tags(paths.deployments)
    store = DataStore(paths.data)
    today = now_ist().date()
    fy = (args.fy or fy_label(today)).upper()
    as_of = date.fromisoformat(args.as_of) if args.as_of else min(today, fy_bounds(fy)[1])
    ledger = store.read_ledger()
    if args.stdout:
        report = build_tax_report(ledger, fy, tags, as_of=as_of, currency=settings.reporting_currency,
                                  default_segment=settings.default_segment, rate=args.rate)
        console.always(render_tax_markdown(report))
        return EXIT_OK
    written = write_tax_reports(paths, ledger, tags, settings, fy, as_of, rate=args.rate)
    report = build_tax_report(ledger, fy, tags, as_of=as_of, currency=settings.reporting_currency,
                              default_segment=settings.default_segment, rate=args.rate)
    console.always(f"{fy} net {fmt_money(report.overall.net, settings.reporting_currency, sign=True)} over "
                   f"{report.overall.trading_days} trading day(s) · " + ", ".join(str(p) for p in written))
    return EXIT_OK


def cmd_send(args: argparse.Namespace) -> int:
    console = Console(args.quiet, args.verbose)
    paths = Paths.from_args(args)
    settings = _settings(paths)
    if not settings.telegram_configured:
        console.always("Telegram is not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return EXIT_CONFIG
    text = args.text or (f"✅ <b>tradetron-pnl</b> test · {now_ist():%d %b %Y %H:%M IST}\n"
                         "If you can read this, the bot token and chat id are correct.")
    try:
        ids = TelegramBot(settings.telegram_token, settings.telegram_chat_id, timeout=settings.timeout).send(text)
    except TelegramError as exc:
        console.always(f"FAILED · {exc}")
        return EXIT_TELEGRAM_FAILED
    console.always(f"sent message id(s) {ids}")
    return EXIT_OK


def cmd_import(args: argparse.Namespace) -> int:
    import csv as _csv
    console = Console(args.quiet, args.verbose)
    paths = Paths.from_args(args)
    settings = _settings(paths)
    store = DataStore(paths.data)
    path = Path(args.csv)
    if not path.exists():
        console.always(f"no such file: {path}")
        return EXIT_CONFIG
    records = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in _csv.DictReader(fh):
            if not (row.get(args.date_col) or "").strip():
                continue
            records.append({
                "date": row.get(args.date_col),
                "pnl": row.get(args.pnl_col),
                "name": row.get(args.name_col),
                "deployment_id": row.get(args.id_col),
                "currency": row.get("currency"),
                "broker": row.get("broker"),
                "notes": f"imported from {path.name}",
            })
    try:
        rows = import_rows(records, default_currency=args.currency or settings.reporting_currency)
    except (ValueError, KeyError) as exc:
        console.always(f"import failed: {exc} — check --date-col/--pnl-col")
        return EXIT_CONFIG
    merged = store.upsert_ledger(rows)
    console.always(f"imported {len(rows)} row(s) from {path.name}; ledger now has {len(merged)} row(s)")
    return EXIT_OK


def cmd_rebuild(args: argparse.Namespace) -> int:
    console = Console(args.quiet, args.verbose)
    paths = Paths.from_args(args)
    settings = _settings(paths)
    holidays = load_holidays(paths.holidays)
    store = DataStore(paths.data)
    rows = rebuild_ledger(store, holidays, settings.pnl_semantics, keep_imported=not args.drop_imported)
    days = sorted({r.trading_day for r in rows})
    console.always(f"ledger rebuilt with semantics '{settings.pnl_semantics}': {len(rows)} row(s) over "
                   f"{len(days)} day(s)" + (f" ({days[0]} → {days[-1]})" if days else ""))
    return EXIT_OK


COMMANDS = {
    "run": cmd_run,
    "probe": cmd_probe,
    "report": cmd_report,
    "tax": cmd_tax,
    "send": cmd_send,
    "import": cmd_import,
    "rebuild": cmd_rebuild,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except ValueError as exc:                     # configuration problems surface as ValueError
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        return 130
    except Exception:                              # noqa: BLE001 — never swallow a traceback silently
        traceback.print_exc()
        return 1


__all__ = ["build_parser", "build_view", "compose_messages", "main", "resolve_kind"]
