#!/usr/bin/env python3
"""Keep ``data/fno_universe.csv`` in step with NSE's F&O list.

Why this exists
---------------
The dashboard and the scheduled scan work on the *full* NSE F&O universe, not
the 40 names the Pine indicator can hold (Pine Script v5 caps a script at 40
``request.security()`` calls — a platform limit, nothing to do with this repo).
That universe changes every quarter: NSE adds, drops and renames single-stock
derivatives by circular.

NSE publishes the authoritative list as ``fo_mktlots.csv`` (underlying, symbol,
then per-expiry market lots). This tool reads it, diffs it against the committed
universe, and reports or applies the difference.

How curation works
------------------
``data/fno_universe.csv`` is the source of truth once written. This tool:

* **adds** symbols NSE has added (name from ``KNOWN_METADATA`` when we have one,
  otherwise NSE's own underlying text, sector ``Unknown``),
* **removes** symbols NSE has dropped,
* **never rewrites** the name, sector or ``enabled`` flag of a symbol that is
  already in the file — so hand-curated values survive every re-sync.

Usage
-----
    python3 tools/sync_universe.py                      # diff against live NSE, report only
    python3 tools/sync_universe.py --from FILE          # diff against a local copy
    python3 tools/sync_universe.py --from FILE --write  # apply (adds/removes rows)
    python3 tools/sync_universe.py --from FILE --check  # exit 1 if out of date (for CI)

NSE blocks non-browser clients from some networks, so ``--from`` exists to let a
mirrored copy be used instead. The scan itself does not import this module.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "fno_universe.csv"
WATCHLIST = ROOT / "data" / "fno_watchlist_price_band.csv"
COLUMNS = ["symbol", "name", "sector", "enabled"]

NSE_URLS = (
    "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv",
    "https://archives.nseindia.com/content/fo/fo_mktlots.csv",
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0 Safari/537.36 ironman-scanner/16.2"
)

SYMBOL_RE = re.compile(r"^[A-Z0-9_.\-&]+$")

# Curated display metadata, keyed by NSE symbol. Used when a symbol first enters
# the universe file; after that the CSV holds the value and this map is ignored
# for that row. Sectors are a convenience grouping for the dashboard, not an
# official NSE classification.
KNOWN_METADATA: dict[str, tuple[str, str]] = {
    # --- Banks ------------------------------------------------------------- #
    "AXISBANK": ("Axis Bank", "Banks"),
    "BANKBARODA": ("Bank of Baroda", "Banks"),
    "BANKINDIA": ("Bank of India", "Banks"),
    "CANBK": ("Canara Bank", "Banks"),
    "FEDERALBNK": ("Federal Bank", "Banks"),
    "HDFCBANK": ("HDFC Bank", "Banks"),
    "ICICIBANK": ("ICICI Bank", "Banks"),
    "IDFCFIRSTB": ("IDFC First Bank", "Banks"),
    "INDUSINDBK": ("IndusInd Bank", "Banks"),
    "INDIANB": ("Indian Bank", "Banks"),
    "KOTAKBANK": ("Kotak Mahindra Bank", "Banks"),
    "MAHABANK": ("Bank of Maharashtra", "Banks"),
    "PNB": ("Punjab National Bank", "Banks"),
    "SBIN": ("State Bank of India", "Banks"),
    "UNIONBANK": ("Union Bank of India", "Banks"),
    "YESBANK": ("Yes Bank", "Banks"),
    "AUBANK": ("AU Small Finance Bank", "Banks"),
    "BANDHANBNK": ("Bandhan Bank", "Banks"),
    "RBLBANK": ("RBL Bank", "Banks"),
    # --- Financial Services ------------------------------------------------ #
    "360ONE": ("360 One WAM", "Financial Services"),
    "ABCAPITAL": ("Aditya Birla Capital", "Financial Services"),
    "ANGELONE": ("Angel One", "Financial Services"),
    "BAJAJFINSV": ("Bajaj Finserv", "Financial Services"),
    "BAJAJHLDNG": ("Bajaj Holdings & Investment", "Financial Services"),
    "BAJFINANCE": ("Bajaj Finance", "Financial Services"),
    "BSE": ("BSE", "Financial Services"),
    "CAMS": ("Computer Age Management Services", "Financial Services"),
    "CDSL": ("Central Depository Services", "Financial Services"),
    "CHOLAFIN": ("Cholamandalam Investment & Finance", "Financial Services"),
    "HDFCAMC": ("HDFC Asset Management", "Financial Services"),
    "HDFCLIFE": ("HDFC Life Insurance", "Financial Services"),
    "ICICIGI": ("ICICI Lombard General Insurance", "Financial Services"),
    "ICICIPRULI": ("ICICI Prudential Life Insurance", "Financial Services"),
    "IEX": ("Indian Energy Exchange", "Financial Services"),
    "JIOFIN": ("Jio Financial Services", "Financial Services"),
    "KFINTECH": ("KFin Technologies", "Financial Services"),
    "LICHSGFIN": ("LIC Housing Finance", "Financial Services"),
    "LICI": ("Life Insurance Corporation of India", "Financial Services"),
    "LTF": ("L&T Finance", "Financial Services"),
    "MANAPPURAM": ("Manappuram Finance", "Financial Services"),
    "MCX": ("Multi Commodity Exchange", "Financial Services"),
    "MFSL": ("Max Financial Services", "Financial Services"),
    "MOTILALOFS": ("Motilal Oswal Financial Services", "Financial Services"),
    "MUTHOOTFIN": ("Muthoot Finance", "Financial Services"),
    "NAM-INDIA": ("Nippon Life India Asset Management", "Financial Services"),
    "PFC": ("Power Finance Corporation", "Financial Services"),
    "PNBHOUSING": ("PNB Housing Finance", "Financial Services"),
    "POLICYBZR": ("PB Fintech", "Financial Services"),
    "RECLTD": ("REC", "Financial Services"),
    "SBICARD": ("SBI Cards & Payment Services", "Financial Services"),
    "SBILIFE": ("SBI Life Insurance", "Financial Services"),
    "SHRIRAMFIN": ("Shriram Finance", "Financial Services"),
    # --- IT ---------------------------------------------------------------- #
    "COFORGE": ("Coforge", "IT"),
    "HCLTECH": ("HCL Technologies", "IT"),
    "INFY": ("Infosys", "IT"),
    "KPITTECH": ("KPIT Technologies", "IT"),
    "LTM": ("LTIMindtree", "IT"),
    "MPHASIS": ("Mphasis", "IT"),
    "NAUKRI": ("Info Edge (Naukri)", "IT"),
    "OFSS": ("Oracle Financial Services Software", "IT"),
    "PERSISTENT": ("Persistent Systems", "IT"),
    "TATAELXSI": ("Tata Elxsi", "IT"),
    "TCS": ("Tata Consultancy Services", "IT"),
    "TECHM": ("Tech Mahindra", "IT"),
    "WIPRO": ("Wipro", "IT"),
    "SAGILITY": ("Sagility", "IT"),
    "TATATECH": ("Tata Technologies", "IT"),
    # --- Pharma & Healthcare ----------------------------------------------- #
    "ALKEM": ("Alkem Laboratories", "Pharma & Healthcare"),
    "APOLLOHOSP": ("Apollo Hospitals Enterprise", "Pharma & Healthcare"),
    "AUROPHARMA": ("Aurobindo Pharma", "Pharma & Healthcare"),
    "BIOCON": ("Biocon", "Pharma & Healthcare"),
    "CIPLA": ("Cipla", "Pharma & Healthcare"),
    "DIVISLAB": ("Divi's Laboratories", "Pharma & Healthcare"),
    "DRREDDY": ("Dr. Reddy's Laboratories", "Pharma & Healthcare"),
    "FORTIS": ("Fortis Healthcare", "Pharma & Healthcare"),
    "GLENMARK": ("Glenmark Pharmaceuticals", "Pharma & Healthcare"),
    "LAURUSLABS": ("Laurus Labs", "Pharma & Healthcare"),
    "LUPIN": ("Lupin", "Pharma & Healthcare"),
    "MANKIND": ("Mankind Pharma", "Pharma & Healthcare"),
    "MAXHEALTH": ("Max Healthcare Institute", "Pharma & Healthcare"),
    "SUNPHARMA": ("Sun Pharmaceutical Industries", "Pharma & Healthcare"),
    "TORNTPHARM": ("Torrent Pharmaceuticals", "Pharma & Healthcare"),
    "ZYDUSLIFE": ("Zydus Lifesciences", "Pharma & Healthcare"),
    # --- Auto & Ancillaries ------------------------------------------------ #
    "ASHOKLEY": ("Ashok Leyland", "Auto & Ancillaries"),
    "BAJAJ-AUTO": ("Bajaj Auto", "Auto & Ancillaries"),
    "BHARATFORG": ("Bharat Forge", "Auto & Ancillaries"),
    "BOSCHLTD": ("Bosch", "Auto & Ancillaries"),
    "EICHERMOT": ("Eicher Motors", "Auto & Ancillaries"),
    "EXIDEIND": ("Exide Industries", "Auto & Ancillaries"),
    "HEROMOTOCO": ("Hero MotoCorp", "Auto & Ancillaries"),
    "HYUNDAI": ("Hyundai Motor India", "Auto & Ancillaries"),
    "M&M": ("Mahindra & Mahindra", "Auto & Ancillaries"),
    "MARUTI": ("Maruti Suzuki India", "Auto & Ancillaries"),
    "MOTHERSON": ("Samvardhana Motherson International", "Auto & Ancillaries"),
    "SONACOMS": ("Sona BLW Precision Forgings", "Auto & Ancillaries"),
    "TIINDIA": ("Tube Investments of India", "Auto & Ancillaries"),
    "TMPV": ("Tata Motors Passenger Vehicles", "Auto & Ancillaries"),
    "TVSMOTOR": ("TVS Motor Company", "Auto & Ancillaries"),
    "UNOMINDA": ("Uno Minda", "Auto & Ancillaries"),
    "FORCEMOT": ("Force Motors", "Auto & Ancillaries"),
    "ATHERENERG": ("Ather Energy", "Auto & Ancillaries"),
    # --- FMCG & Consumer --------------------------------------------------- #
    "BRITANNIA": ("Britannia Industries", "FMCG & Consumer"),
    "COLPAL": ("Colgate-Palmolive India", "FMCG & Consumer"),
    "DABUR": ("Dabur India", "FMCG & Consumer"),
    "GODREJCP": ("Godrej Consumer Products", "FMCG & Consumer"),
    "HINDUNILVR": ("Hindustan Unilever", "FMCG & Consumer"),
    "ITC": ("ITC", "FMCG & Consumer"),
    "JUBLFOOD": ("Jubilant FoodWorks", "FMCG & Consumer"),
    "MARICO": ("Marico", "FMCG & Consumer"),
    "NESTLEIND": ("Nestle India", "FMCG & Consumer"),
    "PATANJALI": ("Patanjali Foods", "FMCG & Consumer"),
    "RADICO": ("Radico Khaitan", "FMCG & Consumer"),
    "TATACONSUM": ("Tata Consumer Products", "FMCG & Consumer"),
    "UNITDSPR": ("United Spirits", "FMCG & Consumer"),
    "VBL": ("Varun Beverages", "FMCG & Consumer"),
    "GODFRYPHLP": ("Godfrey Phillips India", "FMCG & Consumer"),
    # --- Consumer Durables, Retail & Services ------------------------------ #
    "AMBER": ("Amber Enterprises India", "Consumer Durables & Retail"),
    "ASIANPAINT": ("Asian Paints", "Consumer Durables & Retail"),
    "BLUESTARCO": ("Blue Star", "Consumer Durables & Retail"),
    "CROMPTON": ("Crompton Greaves Consumer Electricals", "Consumer Durables & Retail"),
    "DIXON": ("Dixon Technologies India", "Consumer Durables & Retail"),
    "DMART": ("Avenue Supermarts (DMart)", "Consumer Durables & Retail"),
    "ETERNAL": ("Eternal (Zomato)", "Consumer Durables & Retail"),
    "HAVELLS": ("Havells India", "Consumer Durables & Retail"),
    "KAJARIACER": ("Kajaria Ceramics", "Consumer Durables & Retail"),
    "KALYANKJIL": ("Kalyan Jewellers India", "Consumer Durables & Retail"),
    "NYKAA": ("FSN E-Commerce Ventures (Nykaa)", "Consumer Durables & Retail"),
    "PAGEIND": ("Page Industries", "Consumer Durables & Retail"),
    "PGEL": ("PG Electroplast", "Consumer Durables & Retail"),
    "PIDILITIND": ("Pidilite Industries", "Consumer Durables & Retail"),
    "SWIGGY": ("Swiggy", "Consumer Durables & Retail"),
    "TITAN": ("Titan Company", "Consumer Durables & Retail"),
    "TRENT": ("Trent", "Consumer Durables & Retail"),
    "VOLTAS": ("Voltas", "Consumer Durables & Retail"),
    "VMM": ("Vishal Mega Mart", "Consumer Durables & Retail"),
    "INDHOTEL": ("Indian Hotels Company", "Consumer Durables & Retail"),
    # --- Energy, Power & Oil ------------------------------------------------- #
    "ADANIENSOL": ("Adani Energy Solutions", "Energy & Power"),
    "ADANIGREEN": ("Adani Green Energy", "Energy & Power"),
    "ADANIPOWER": ("Adani Power", "Energy & Power"),
    "BPCL": ("Bharat Petroleum Corporation", "Energy & Power"),
    "COALINDIA": ("Coal India", "Energy & Power"),
    "GAIL": ("GAIL (India)", "Energy & Power"),
    "HINDPETRO": ("Hindustan Petroleum Corporation", "Energy & Power"),
    "IOC": ("Indian Oil Corporation", "Energy & Power"),
    "IREDA": ("Indian Renewable Energy Development Agency", "Energy & Power"),
    "JSWENERGY": ("JSW Energy", "Energy & Power"),
    "NHPC": ("NHPC", "Energy & Power"),
    "NTPC": ("NTPC", "Energy & Power"),
    "OIL": ("Oil India", "Energy & Power"),
    "ONGC": ("Oil and Natural Gas Corporation", "Energy & Power"),
    "PETRONET": ("Petronet LNG", "Energy & Power"),
    "POWERGRID": ("Power Grid Corporation of India", "Energy & Power"),
    "PREMIERENE": ("Premier Energies", "Energy & Power"),
    "SUZLON": ("Suzlon Energy", "Energy & Power"),
    "TATAPOWER": ("Tata Power", "Energy & Power"),
    "TORNTPOWER": ("Torrent Power", "Energy & Power"),
    "INOXWIND": ("Inox Wind", "Energy & Power"),
    "WAAREEENER": ("Waaree Energies", "Energy & Power"),
    "SOLARINDS": ("Solar Industries India", "Energy & Power"),
    # --- Metals & Mining ---------------------------------------------------- #
    "APLAPOLLO": ("APL Apollo Tubes", "Metals & Mining"),
    "HINDALCO": ("Hindalco Industries", "Metals & Mining"),
    "HINDZINC": ("Hindustan Zinc", "Metals & Mining"),
    "JINDALSTEL": ("Jindal Steel", "Metals & Mining"),
    "JSWSTEEL": ("JSW Steel", "Metals & Mining"),
    "NATIONALUM": ("National Aluminium Company", "Metals & Mining"),
    "NMDC": ("NMDC", "Metals & Mining"),
    "SAIL": ("Steel Authority of India", "Metals & Mining"),
    "TATASTEEL": ("Tata Steel", "Metals & Mining"),
    "VEDL": ("Vedanta", "Metals & Mining"),
    "JSL": ("Jindal Stainless", "Metals & Mining"),
    "HINDCOPPER": ("Hindustan Copper", "Metals & Mining"),
    # --- Cement & Building Materials ---------------------------------------- #
    "AMBUJACEM": ("Ambuja Cements", "Cement & Building Materials"),
    "GRASIM": ("Grasim Industries", "Cement & Building Materials"),
    "SHREECEM": ("Shree Cement", "Cement & Building Materials"),
    "ULTRACEMCO": ("UltraTech Cement", "Cement & Building Materials"),
    "ASTRAL": ("Astral", "Cement & Building Materials"),
    "SUPREMEIND": ("Supreme Industries", "Cement & Building Materials"),
    "POLYCAB": ("Polycab India", "Cement & Building Materials"),
    "KEI": ("KEI Industries", "Cement & Building Materials"),
    "DALBHARAT": ("Dalmia Bharat", "Cement & Building Materials"),
    # --- Capital Goods, Defence & Industrials -------------------------------- #
    "ABB": ("ABB India", "Capital Goods & Defence"),
    "BDL": ("Bharat Dynamics", "Capital Goods & Defence"),
    "BEL": ("Bharat Electronics", "Capital Goods & Defence"),
    "BHEL": ("Bharat Heavy Electricals", "Capital Goods & Defence"),
    "CGPOWER": ("CG Power and Industrial Solutions", "Capital Goods & Defence"),
    "CUMMINSIND": ("Cummins India", "Capital Goods & Defence"),
    "GVT&D": ("GE Vernova T&D India", "Capital Goods & Defence"),
    "HAL": ("Hindustan Aeronautics", "Capital Goods & Defence"),
    "LT": ("Larsen & Toubro", "Capital Goods & Defence"),
    "MAZDOCK": ("Mazagon Dock Shipbuilders", "Capital Goods & Defence"),
    "POWERINDIA": ("Hitachi Energy India", "Capital Goods & Defence"),
    "SIEMENS": ("Siemens", "Capital Goods & Defence"),
    "COCHINSHIP": ("Cochin Shipyard", "Capital Goods & Defence"),
    "KAYNES": ("Kaynes Technology India", "Capital Goods & Defence"),
    "SYRMA": ("Syrma SGS Technology", "Capital Goods & Defence"),
    # --- Chemicals ---------------------------------------------------------- #
    "PIIND": ("PI Industries", "Chemicals"),
    "SRF": ("SRF", "Chemicals"),
    "UPL": ("UPL", "Chemicals"),
    "DEEPAKNTR": ("Deepak Nitrite", "Chemicals"),
    "NAVINFLUOR": ("Navin Fluorine International", "Chemicals"),
    "ATUL": ("Atul", "Chemicals"),
    "TATACHEM": ("Tata Chemicals", "Chemicals"),
    # --- Telecom & Media ---------------------------------------------------- #
    "BHARTIARTL": ("Bharti Airtel", "Telecom & Media"),
    "IDEA": ("Vodafone Idea", "Telecom & Media"),
    "INDUSTOWER": ("Indus Towers", "Telecom & Media"),
    "SUNTV": ("Sun TV Network", "Telecom & Media"),
    "PVRINOX": ("PVR INOX", "Telecom & Media"),
    "ZEEL": ("Zee Entertainment Enterprises", "Telecom & Media"),
    # --- Infrastructure, Logistics & Aviation -------------------------------- #
    "ADANIPORTS": ("Adani Ports & Special Economic Zone", "Infrastructure & Logistics"),
    "CONCOR": ("Container Corporation of India", "Infrastructure & Logistics"),
    "DELHIVERY": ("Delhivery", "Infrastructure & Logistics"),
    "GMRAIRPORT": ("GMR Airports", "Infrastructure & Logistics"),
    "INDIGO": ("InterGlobe Aviation (IndiGo)", "Infrastructure & Logistics"),
    "IRB": ("IRB Infrastructure Developers", "Infrastructure & Logistics"),
    "IRFC": ("Indian Railway Finance Corporation", "Infrastructure & Logistics"),
    "NBCC": ("NBCC (India)", "Infrastructure & Logistics"),
    "RVNL": ("Rail Vikas Nigam", "Infrastructure & Logistics"),
    "IRCON": ("Ircon International", "Infrastructure & Logistics"),
    "HUDCO": ("Housing & Urban Development Corporation", "Infrastructure & Logistics"),
    # --- Realty -------------------------------------------------------------- #
    "DLF": ("DLF", "Realty"),
    "GODREJPROP": ("Godrej Properties", "Realty"),
    "LODHA": ("Macrotech Developers (Lodha)", "Realty"),
    "OBEROIRLTY": ("Oberoi Realty", "Realty"),
    "PHOENIXLTD": ("Phoenix Mills", "Realty"),
    "PRESTIGE": ("Prestige Estates Projects", "Realty"),
    # --- Diversified & Others ------------------------------------------------- #
    "ADANIENT": ("Adani Enterprises", "Diversified"),
    "RELIANCE": ("Reliance Industries", "Diversified"),
    "PAYTM": ("One 97 Communications (Paytm)", "Diversified"),
}


# --------------------------------------------------------------------------- #
# Parsing NSE's file
# --------------------------------------------------------------------------- #
def parse_mktlots(text: str) -> list[tuple[str, str]]:
    """Return ``[(symbol, underlying)]`` for single-stock derivatives only.

    ``fo_mktlots.csv`` is laid out as an index section, then a section header row
    (``UNDERLYING`` = "Derivatives on Individual Securities"), then the stocks.
    Only the stock section is returned — NIFTY, BANKNIFTY, FINNIFTY and friends
    are indices, not scrips, and cannot be scanned as a stock.
    """
    rows = list(csv.reader(io.StringIO(text)))
    # The file has two header rows: the top one (UNDERLYING/SYMBOL/...) covering
    # the index section, then a second one whose UNDERLYING cell reads
    # "Derivatives on Individual Securities" and which starts the stock section.
    start = None
    for i, row in enumerate(rows):
        if len(row) < 2:
            continue
        first, second = row[0].strip().lower(), row[1].strip().lower()
        if second == "symbol" and "individual securities" in first:
            start = i + 1
            break
    if start is None:
        raise ValueError(
            "could not find the 'Derivatives on Individual Securities' section header - "
            "is this really fo_mktlots.csv?"
        )

    out: list[tuple[str, str]] = []
    for row in rows[start:]:
        if len(row) < 2:
            continue
        underlying, symbol = row[0].strip(), row[1].strip()
        if not symbol or not underlying:
            continue
        if not SYMBOL_RE.match(symbol):
            raise ValueError(f"unexpected symbol {symbol!r} in NSE list")
        out.append((symbol, underlying))

    if not out:
        raise ValueError("no single-stock rows found after the section header")
    return out


def display_name(symbol: str, underlying: str) -> str:
    """Prefer curated metadata; otherwise tidy NSE's own (often truncated) text."""
    if symbol in KNOWN_METADATA:
        return KNOWN_METADATA[symbol][0]
    name = re.sub(r"\s+", " ", underlying).strip(" .")
    name = re.sub(r"\b(LTD|LIMITED|CO|CORP|CORPORATION)\.?$", "", name, flags=re.I).strip(" .,")
    return name.title() or symbol


def sector_for(symbol: str) -> str:
    return KNOWN_METADATA.get(symbol, ("", "Unknown"))[1]


# --------------------------------------------------------------------------- #
# Source
# --------------------------------------------------------------------------- #
def fetch_nse(timeout: float = 25.0) -> str:
    """Fetch the live list, trying each NSE host in turn."""
    last: Exception | None = None
    for url in NSE_URLS:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
    raise RuntimeError(
        f"could not fetch the NSE list ({last}). NSE blocks some networks; "
        f"download fo_mktlots.csv yourself and pass --from FILE."
    )


def read_universe(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_universe(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------- #
def merge(existing: list[dict], nse: list[tuple[str, str]]) -> tuple[list[dict], list, list]:
    """Merge the NSE list into the committed rows, preserving curation.

    NSE's file lists bare tickers (``RELIANCE``); the repo's convention — used by
    the watchlist, the report and the Pine inputs — is ``EXCHANGE:TICKER``, so
    rows are written as ``NSE:RELIANCE``.
    """
    prefix = "NSE:"
    by_symbol = {r["symbol"].strip(): dict(r) for r in existing if r.get("symbol", "").strip()}
    nse_keys = [prefix + symbol for symbol, _ in nse]
    nse_key_set = set(nse_keys)

    added = [s for s, _ in nse if prefix + s not in by_symbol]
    removed = sorted(sym for sym in by_symbol if sym not in nse_key_set)

    rows: list[dict] = []
    for symbol, underlying in nse:                      # NSE order, so diffs are stable
        key = prefix + symbol
        if key in by_symbol:
            row = by_symbol[key]
            row["name"] = row.get("name") or display_name(symbol, underlying)
            row["sector"] = row.get("sector") or sector_for(symbol)
            row["enabled"] = (row.get("enabled") or "1").strip() or "1"
        else:
            row = {
                "symbol": key,
                "name": display_name(symbol, underlying),
                "sector": sector_for(symbol),
                "enabled": "1",
            }
        rows.append(row)
    return rows, added, removed


def audit_watchlist(nse_symbols: set[str], watchlist: Path) -> list[str]:
    """Names in the 40-symbol price-band watchlist that are not current F&O.

    The watchlist is hand-curated and the F&O segment changes every quarter, so
    a name can quietly stop being tradeable in F&O while still sitting in the
    file. Returns the offending symbols (bare, NSE-style).
    """
    if not watchlist.exists():
        return []
    with watchlist.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    stale: list[str] = []
    for row in rows:
        if (row.get("enabled") or "").strip() != "1":
            continue
        symbol = (row.get("symbol") or "").strip()
        bare = symbol.split(":")[-1]
        if bare and bare not in nse_symbols:
            stale.append(bare)
    return sorted(stale)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="source", type=Path, default=None,
                    help="local copy of fo_mktlots.csv (skips the network)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="universe CSV to write")
    ap.add_argument("--write", action="store_true", help="apply additions and removals")
    ap.add_argument("--check", action="store_true", help="exit 1 if the universe is out of date")
    ap.add_argument("--audit", action="store_true",
                    help="also check the 40-name price-band watchlist against the F&O list")
    args = ap.parse_args()

    if args.source:
        text = args.source.read_text(encoding="utf-8", errors="replace")
        origin = str(args.source)
    else:
        text = fetch_nse()
        origin = "NSE fo_mktlots.csv"

    nse = parse_mktlots(text)
    existing = read_universe(args.out)

    if not existing:
        print(f"universe        : {args.out} does not exist yet - it will be created")

    rows, added, removed = merge(existing, nse)

    print(f"source          : {origin}")
    print(f"NSE symbols     : {len(nse)} single-stock underlyings")
    print(f"universe file   : {len(existing)} row(s)")
    print(f"added           : {len(added)}" + (f"  {', '.join(added[:12])}" if added else ""))
    print(f"removed         : {len(removed)}" + (f"  {', '.join(removed[:12])}" if removed else ""))
    unknown = sum(1 for r in rows if r["sector"] == "Unknown")
    print(f"unclassified    : {unknown} symbol(s) with sector 'Unknown'")

    stale_watchlist: list[str] = []
    if args.audit:
        nse_set = {s for s, _ in nse}
        stale_watchlist = audit_watchlist(nse_set, WATCHLIST)
        if stale_watchlist:
            print(
                f"watchlist audit : {len(stale_watchlist)} enabled name(s) NOT in the F&O "
                f"segment: {', '.join(stale_watchlist)}"
            )
            print(
                "                  these will never resolve as F&O - disable them or replace "
                "them in data/fno_watchlist_price_band.csv"
            )
        else:
            print("watchlist audit : OK (all enabled names are current F&O underlyings)")

    stale = bool(added or removed)
    if args.check:
        if stale:
            print("\ncheck: OUT OF DATE - run without --check to update the universe")
            return 1
        if stale_watchlist:
            print("\ncheck: FAILED - the price-band watchlist holds non-F&O names")
            return 1
        print("\ncheck: OK (universe matches the source)")
        return 0

    if args.write:
        write_universe(args.out, rows)
        print(f"\nwritten         : {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")
    elif stale:
        print("\n(report only - pass --write to apply, or --check in CI)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
