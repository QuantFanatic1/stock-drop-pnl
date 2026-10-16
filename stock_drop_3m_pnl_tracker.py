#!/usr/bin/env python3
"""
Stock Drop 3-Month P&L Tracker  *** EXPERIMENT ONLY ***
-------------------------------------------------------
READ-ONLY vs the live monthly book. Does NOT modify:
  - stock_drop_pnl_tracker.py
  - stock_drop_watchlist.py monthly expiry logic
  - Stock_Drop_Alert/stock_drop_pnl.json (main book — read only)

Writes only to its own experiment files:
  stock_drop_3m_pnl.json / .txt
  stock_drop_monthly_vs_3m_pnl.txt

Side-by-side test of the stock-drop book using ~3-month (~90 DTE) expirations.

Takes every entry from the main stock_drop_pnl.json book — same symbol,
direction, strike, TP/SL, tier, entry date / fill rules — and swaps only
the option expiration to the listed expiry closest to ~90 calendar days
out from entry (typically a monthly cycle in that window).

Thesis
------
Longer-dated contracts carry fatter premiums. With the same sizing rule
(target ~$2,000, min 1 contract), expensive premiums often force more
capital per trade. If % P&L stays near the monthly book, dollar P&L rises.

Default expiry rule
-------------------
Among listed expiries with DTE in [target − band, target + band]
(default target=90, band=30 → 60–120 DTE), pick the closest to 90 DTE.
On a tie, prefer a true monthly (3rd Friday). Expand the search window
if nothing is listed in-band.

Usage:
    python stock_drop_3m_pnl_tracker.py --rebuild
    python stock_drop_3m_pnl_tracker.py --rebuild --limit 20
    python stock_drop_3m_pnl_tracker.py --update
    python stock_drop_3m_pnl_tracker.py --summary
    python stock_drop_3m_pnl_tracker.py --compare
    python stock_drop_3m_pnl_tracker.py --rebuild --target-dte 90 --band 30

Output:
    Stock_Drop_Alert/stock_drop_3m_pnl.json
    Stock_Drop_Alert/stock_drop_3m_pnl.txt
    Stock_Drop_Alert/stock_drop_monthly_vs_3m_pnl.txt
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import time
from collections import Counter
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv
from tabulate import tabulate

import stock_drop_pnl_tracker as main_pnl

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_SCRIPT_DIR, ".env"))

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
POLYGON_BASE = "https://api.polygon.io"

ALERT_DIR = os.path.join(_SCRIPT_DIR, "Stock_Drop_Alert")
os.makedirs(ALERT_DIR, exist_ok=True)

SOURCE_PNL_JSON = os.path.join(ALERT_DIR, "stock_drop_pnl.json")

# Paths set by configure_experiment() — default = 10d hold experiment
THREE_M_PNL_JSON = os.path.join(ALERT_DIR, "stock_drop_3m_pnl.json")
THREE_M_PNL_TXT = os.path.join(ALERT_DIR, "stock_drop_3m_pnl.txt")
COMPARE_TXT = os.path.join(ALERT_DIR, "stock_drop_monthly_vs_3m_pnl.txt")

DEFAULT_TARGET_DTE = 90
DEFAULT_BAND = 30          # search 60–120 DTE by default
EXPIRY_SEARCH_OUTER = 150  # hard outer bound when expanding
DEFAULT_MAX_HOLD_DAYS = 10  # experiment default; production monthly stays at 10

# Experiment-only fill gate. Farther expiries trade thinner; market makers still
# quote so exits are realistic, but drop-day volume is much lower than front-month.
# Production monthly book keeps MIN_CONTRACT_VOLUME=50 — this does NOT change it.
EXP_MIN_CONTRACT_VOLUME = 10

# Active experiment max-hold (set via configure_experiment / --max-hold)
EXP_MAX_HOLD_DAYS = DEFAULT_MAX_HOLD_DAYS

_expiry_cache: dict = {}
_contract_cache: dict = {}
_last_polygon_ts = 0.0
POLYGON_MIN_INTERVAL = 0.35


def configure_experiment(max_hold_days: int = DEFAULT_MAX_HOLD_DAYS):
    """
    Point this experiment process at the correct output files.

    - max_hold=10 → stock_drop_3m_pnl.*  (existing 10d run)
    - max_hold=20 → stock_drop_3m_20d_pnl.*  (separate; does not overwrite 10d)

    Never writes the live monthly book.
    """
    global THREE_M_PNL_JSON, THREE_M_PNL_TXT, COMPARE_TXT, EXP_MAX_HOLD_DAYS
    EXP_MAX_HOLD_DAYS = int(max_hold_days)
    if EXP_MAX_HOLD_DAYS == 10:
        stem = "stock_drop_3m_pnl"
        cmp_name = "stock_drop_monthly_vs_3m_pnl.txt"
    else:
        stem = f"stock_drop_3m_{EXP_MAX_HOLD_DAYS}d_pnl"
        cmp_name = f"stock_drop_monthly_vs_3m_{EXP_MAX_HOLD_DAYS}d_pnl.txt"
    THREE_M_PNL_JSON = os.path.join(ALERT_DIR, f"{stem}.json")
    THREE_M_PNL_TXT = os.path.join(ALERT_DIR, f"{stem}.txt")
    COMPARE_TXT = os.path.join(ALERT_DIR, cmp_name)
    return THREE_M_PNL_JSON, THREE_M_PNL_TXT, COMPARE_TXT


def _polygon_get(url, params=None):
    global _last_polygon_ts
    elapsed = time.time() - _last_polygon_ts
    if elapsed < POLYGON_MIN_INTERVAL:
        time.sleep(POLYGON_MIN_INTERVAL - elapsed)
    try:
        resp = requests.get(url, params=params, timeout=20)
    except requests.RequestException:
        return None
    _last_polygon_ts = time.time()
    if resp is not None and resp.status_code == 429:
        time.sleep(2)
        try:
            resp = requests.get(url, params=params, timeout=20)
        except requests.RequestException:
            return None
        _last_polygon_ts = time.time()
    return resp


# ── Persistence ──────────────────────────────────────────────────────────────

def load_3m_data():
    if os.path.exists(THREE_M_PNL_JSON):
        with open(THREE_M_PNL_JSON, "r") as f:
            return json.load(f)
    return {
        "positions": [],
        "last_update": None,
        "total_invested": 0,
        "total_pnl": 0,
        "source_file": SOURCE_PNL_JSON,
        "build_meta": {},
    }


def save_3m_data(data):
    # Hard guard: never overwrite the live monthly book
    if os.path.abspath(THREE_M_PNL_JSON) == os.path.abspath(SOURCE_PNL_JSON):
        raise RuntimeError("Refusing to write experiment data over main stock_drop_pnl.json")
    with open(THREE_M_PNL_JSON, "w") as f:
        json.dump(data, f, indent=2)


def load_source_positions():
    if not os.path.exists(SOURCE_PNL_JSON):
        print(f"  Source book not found: {SOURCE_PNL_JSON}")
        return []
    with open(SOURCE_PNL_JSON, "r") as f:
        src = json.load(f)
    return src.get("positions", [])


# ── Expiry / contract resolution ─────────────────────────────────────────────

def build_option_ticker(symbol: str, expiry: date, direction: str, strike: float) -> str:
    yy = expiry.strftime("%y%m%d")
    cp = "C" if str(direction).lower().startswith("c") else "P"
    strike_int = int(round(float(strike) * 1000))
    return f"O:{symbol.upper()}{yy}{cp}{strike_int:08d}"


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    days_to_fri = (4 - first.weekday()) % 7
    return first + timedelta(days=days_to_fri + 14)


def _holiday_adjusted_monthly(year: int, month: int) -> date:
    """
    Standard equity monthly: 3rd Friday, or prior trading day if that Friday
    is a market holiday (e.g. Juneteenth 2026-06-19 → expiry 2026-06-18).
    """
    try:
        from market_holidays import is_market_holiday
    except ImportError:
        is_market_holiday = None

    d = _third_friday(year, month)
    if is_market_holiday is None:
        return d
    if not is_market_holiday(d.strftime("%Y-%m-%d")):
        return d
    adj = d - timedelta(days=1)
    while adj.weekday() >= 5 or is_market_holiday(adj.strftime("%Y-%m-%d")):
        adj -= timedelta(days=1)
    return adj


def _is_monthly_expiry(d: date) -> bool:
    """True if d is the (holiday-adjusted) monthly options expiry for its month."""
    return d == _holiday_adjusted_monthly(d.year, d.month)


def _monthlies_in_range(start: date, end: date) -> list:
    """All holiday-adjusted monthly expiries with start <= d <= end."""
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end + timedelta(days=31):
        monthly = _holiday_adjusted_monthly(y, m)
        if start <= monthly <= end:
            out.append(monthly)
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def list_expiries(symbol: str, direction: str, entry_date_str: str,
                  min_dte: int = 1, max_dte: int = EXPIRY_SEARCH_OUTER) -> list:
    """
    Sorted list of expiration_date strings in [entry+min_dte, entry+max_dte].

    Paginates Polygon results (1000/page) so wide DTE windows still surface
    true monthlies, not just the first page of weekly strikes.
    """
    opt_type = "call" if str(direction).lower().startswith("c") else "put"
    cache_key = (symbol.upper(), opt_type, entry_date_str, min_dte, max_dte)
    if cache_key in _expiry_cache:
        return _expiry_cache[cache_key]

    entry = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
    gte = (entry + timedelta(days=min_dte)).strftime("%Y-%m-%d")
    lte = (entry + timedelta(days=max_dte)).strftime("%Y-%m-%d")

    url = f"{POLYGON_BASE}/v3/reference/options/contracts"
    base = {
        "underlying_ticker": symbol.upper(),
        "contract_type": opt_type,
        "expiration_date.gte": gte,
        "expiration_date.lte": lte,
        "limit": 1000,
        "apiKey": POLYGON_API_KEY,
        "sort": "expiration_date",
        "order": "asc",
    }

    expiries = set()
    for expired_flag in ("true", "false"):
        params = dict(base)
        params["expired"] = expired_flag
        next_url = url
        next_params = params
        pages = 0
        while next_url and pages < 20:
            if pages == 0:
                resp = _polygon_get(next_url, next_params)
            else:
                # next_url already has query string; still need apiKey if absent
                if "apiKey=" not in next_url:
                    sep = "&" if "?" in next_url else "?"
                    next_url = f"{next_url}{sep}apiKey={POLYGON_API_KEY}"
                resp = _polygon_get(next_url, None if "?" in next_url else next_params)
            pages += 1
            if not resp or resp.status_code != 200:
                break
            try:
                payload = resp.json()
            except ValueError:
                break
            results = payload.get("results") or []
            for c in results:
                exp = c.get("expiration_date")
                if exp:
                    expiries.add(exp)
            next_url = payload.get("next_url")
            next_params = None
            if not results:
                break
        if expiries:
            break

    # Seed known monthly candidates so we don't miss them if pagination is thin
    for monthly in _monthlies_in_range(
        entry + timedelta(days=min_dte),
        entry + timedelta(days=max_dte),
    ):
        probe_key = (symbol.upper(), opt_type, monthly.isoformat(), "probe")
        if probe_key in _expiry_cache:
            if _expiry_cache[probe_key]:
                expiries.add(monthly.isoformat())
            continue
        probe_params = {
            "underlying_ticker": symbol.upper(),
            "contract_type": opt_type,
            "expiration_date": monthly.isoformat(),
            "limit": 1,
            "expired": "true",
            "apiKey": POLYGON_API_KEY,
        }
        resp = _polygon_get(url, probe_params)
        exists = False
        if resp and resp.status_code == 200:
            try:
                exists = bool((resp.json() or {}).get("results"))
            except ValueError:
                exists = False
        if not exists:
            probe_params["expired"] = "false"
            resp = _polygon_get(url, probe_params)
            if resp and resp.status_code == 200:
                try:
                    exists = bool((resp.json() or {}).get("results"))
                except ValueError:
                    exists = False
        _expiry_cache[probe_key] = exists
        if exists:
            expiries.add(monthly.isoformat())

    ordered = sorted(expiries)
    _expiry_cache[cache_key] = ordered
    return ordered


def pick_3m_expiry(symbol: str, direction: str, entry_date_str: str,
                   target_dte: int = DEFAULT_TARGET_DTE,
                   band: int = DEFAULT_BAND) -> str | None:
    """
    Pick listed expiry closest to target_dte (default 90).

    Strongly prefer true monthlies (holiday-adjusted 3rd Friday): score them
    as if they were 21 DTE closer so we don't land on illiquid weeklies near
    90 DTE when a monthly is in band. On a pure distance tie, monthly wins.
    """
    entry = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
    lo = max(1, target_dte - band)
    hi = target_dte + band

    expiries = list_expiries(symbol, direction, entry_date_str, min_dte=lo, max_dte=hi)
    if not expiries:
        expiries = list_expiries(
            symbol, direction, entry_date_str,
            min_dte=max(30, target_dte - 60),
            max_dte=EXPIRY_SEARCH_OUTER,
        )
    if not expiries:
        return None

    def score(exp_str: str):
        exp = datetime.strptime(exp_str, "%Y-%m-%d").date()
        dte = (exp - entry).days
        dist = abs(dte - target_dte)
        is_monthly = _is_monthly_expiry(exp)
        # Prefer monthlies heavily: ~83 DTE monthly beats ~91 DTE weekly.
        effective = dist - (21 if is_monthly else 0)
        monthly_pref = 0 if is_monthly else 1
        return (effective, monthly_pref, dist, dte)

    return min(expiries, key=score)


def resolve_contract(symbol: str, direction: str, strike: float,
                     expiry_str: str) -> str:
    opt_type = "call" if str(direction).lower().startswith("c") else "put"
    cache_key = (symbol.upper(), opt_type, expiry_str, float(strike))
    if cache_key in _contract_cache:
        return _contract_cache[cache_key]

    url = f"{POLYGON_BASE}/v3/reference/options/contracts"
    base = {
        "underlying_ticker": symbol.upper(),
        "contract_type": opt_type,
        "expiration_date": expiry_str,
        "strike_price": float(strike),
        "limit": 10,
        "apiKey": POLYGON_API_KEY,
    }

    ticker = None
    for expired_flag in ("true", "false"):
        params = dict(base)
        params["expired"] = expired_flag
        resp = _polygon_get(url, params)
        if not resp or resp.status_code != 200:
            continue
        try:
            results = resp.json().get("results") or []
        except ValueError:
            results = []
        for c in results:
            try:
                if abs(float(c.get("strike_price", 0)) - float(strike)) < 0.001:
                    ticker = c.get("ticker")
                    break
            except (TypeError, ValueError):
                continue
        if ticker:
            break

    if not ticker:
        exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        ticker = build_option_ticker(symbol, exp, direction, strike)

    _contract_cache[cache_key] = ticker
    return ticker


# ── Position build + historical sim ──────────────────────────────────────────

def _blank_trade_fields(pos: dict) -> dict:
    pos = copy.deepcopy(pos)
    pos["entry_price"] = None
    pos["num_contracts"] = None
    pos["entry_amount"] = main_pnl.ENTRY_AMOUNT
    pos["current_price"] = None
    pos["current_pnl"] = 0
    pos["current_pnl_pct"] = 0
    pos["peak_pnl_pct"] = 0
    pos["status"] = "open"
    pos["exit_date"] = None
    pos["exit_price"] = None
    pos["exit_pnl"] = 0
    pos["exit_pnl_pct"] = 0
    pos["exit_reason"] = None
    pos["exit_time"] = None
    pos["entry_time"] = None
    pos["trading_days_held"] = 0
    if pos.get("signal_date"):
        pos["entry_date"] = pos["signal_date"]
        del pos["signal_date"]
    return pos


def source_key(pos: dict) -> str:
    return (
        f"{pos.get('symbol')}|{pos.get('direction')}|{pos.get('strike')}|"
        f"{pos.get('entry_date')}|{pos.get('option_ticker')}"
    )


def build_3m_position(src: dict, target_dte: int = DEFAULT_TARGET_DTE,
                      band: int = DEFAULT_BAND) -> dict:
    pos = _blank_trade_fields(src)
    entry_date = pos["entry_date"]
    monthly_expiry = src.get("expiry")
    monthly_ticker = src.get("option_ticker")

    chosen = pick_3m_expiry(
        pos["symbol"], pos["direction"], entry_date,
        target_dte=target_dte, band=band,
    )

    pos["monthly_expiry"] = monthly_expiry
    pos["monthly_option_ticker"] = monthly_ticker
    pos["source_status"] = src.get("status")
    pos["source_exit_pnl"] = src.get("exit_pnl")
    pos["source_exit_pnl_pct"] = src.get("exit_pnl_pct")
    pos["source_key"] = source_key(src)
    pos["book"] = "3m"
    pos["target_dte"] = target_dte
    pos["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not chosen:
        pos["status"] = "no_3m_contract"
        pos["exit_date"] = entry_date
        pos["exit_reason"] = f"No listed expiry near {target_dte} DTE"
        pos["exit_pnl"] = 0
        pos["exit_pnl_pct"] = 0
        pos["expiry"] = monthly_expiry
        pos["option_ticker"] = monthly_ticker
        pos["resolved_3m"] = False
        return pos

    ticker = resolve_contract(
        pos["symbol"], pos["direction"], pos["strike"], chosen
    )
    pos["expiry"] = chosen
    pos["option_ticker"] = ticker
    pos["resolved_3m"] = True

    entry_d = datetime.strptime(entry_date, "%Y-%m-%d").date()
    exp_d = datetime.strptime(chosen, "%Y-%m-%d").date()
    pos["dte_at_entry"] = (exp_d - entry_d).days
    return pos


def _bar_date(bar) -> date | None:
    if "t" not in bar:
        return None
    return datetime.utcfromtimestamp(bar["t"] / 1000).date()


def simulate_position_historical(pos: dict, max_hold_days: int | None = None,
                                 min_volume: int | None = None) -> str:
    """Walk-forward fill + TP/SL + max-hold + expiry (experiment sim)."""
    if pos.get("status") != "open":
        return pos.get("status", "skip")

    max_hold = int(max_hold_days if max_hold_days is not None else EXP_MAX_HOLD_DAYS)
    min_vol = int(min_volume if min_volume is not None else EXP_MIN_CONTRACT_VOLUME)
    ticker = pos["option_ticker"]
    signal_date = pos["entry_date"]
    today = date.today()

    if pos.get("entry_fill") == "day_close":
        entry_px, fill_date, fill_time = main_pnl.get_option_close_price(
            ticker, signal_date, min_volume=min_vol
        )
    else:
        entry_px, fill_date = main_pnl.get_option_entry_price(
            ticker, signal_date, min_volume=min_vol
        )
        fill_time = "06:30"

    if not entry_px or entry_px <= 0:
        if main_pnl.window_elapsed(signal_date):
            pos["status"] = "low_volume"
            pos["exit_date"] = today.strftime("%Y-%m-%d")
            pos["exit_reason"] = (
                f"No fill (vol < {min_vol} for "
                f"{main_pnl.FILL_WINDOW_DAYS + 1} days)"
            )
            pos["exit_pnl"] = 0
            pos["exit_pnl_pct"] = 0
            return "low_volume"
        return "awaiting_volume"

    if fill_date and fill_date != signal_date:
        pos["signal_date"] = signal_date
        pos["entry_date"] = fill_date
    entry_date_str = pos["entry_date"]
    entry_date = datetime.strptime(entry_date_str, "%Y-%m-%d").date()

    pos["entry_time"] = fill_time or ""
    pos["entry_price"] = round(float(entry_px), 2)
    num_c = max(1, math.floor(main_pnl.ENTRY_AMOUNT / (entry_px * 100)))
    pos["num_contracts"] = num_c
    pos["entry_amount"] = round(num_c * entry_px * 100, 2)

    entry_px = pos["entry_price"]
    tp_price = entry_px * (1 + pos["tp_pct"] / 100) if pos.get("tp_pct") else None
    sl_price = entry_px * (1 + pos["sl_pct"] / 100)
    actual_invested = pos["entry_amount"]

    try:
        expiry = datetime.strptime(pos["expiry"], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        expiry = None

    end = today
    if expiry:
        end = min(end, expiry)
    # Calendar buffer: ~1.5× trading days covers weekends/holidays
    cal_buffer = max(21, int(max_hold * 2) + 5)
    end = max(end, entry_date + timedelta(days=cal_buffer))
    end = min(end, today)

    bars = main_pnl.get_option_daily_bars(
        ticker, entry_date_str, end.strftime("%Y-%m-%d")
    ) or []

    trading_days_held = 0
    last_close = None
    last_bar_date = None
    max_hold_status = f"d{max_hold}_close"

    for bar in bars:
        bar_date = _bar_date(bar)
        if bar_date is None or bar_date < entry_date:
            continue

        high = float(bar.get("h", 0) or 0)
        low = float(bar.get("l", 0) or 0)
        close = float(bar.get("c", 0) or 0)
        if close > 0:
            last_close = close
            last_bar_date = bar_date

        skip_tpsl = (
            pos.get("entry_fill") == "day_close" and bar_date == entry_date
        )

        if not skip_tpsl:
            if low > 0 and low <= sl_price:
                pos["status"] = "sl_hit"
                pos["exit_price"] = round(sl_price, 2)
                pos["exit_date"] = bar_date.strftime("%Y-%m-%d")
                pos["exit_time"] = (
                    main_pnl._exit_time_from_minutes(
                        ticker, pos["exit_date"], sl_price, "sl"
                    ) or ""
                )
                pos["exit_reason"] = f"SL {pos['sl_pct']}% hit"
                pos["exit_pnl_pct"] = pos["sl_pct"]
                pos["exit_pnl"] = round(actual_invested * pos["sl_pct"] / 100, 2)
                pos["trading_days_held"] = trading_days_held
                return "sl_hit"

            if tp_price and high >= tp_price:
                pos["status"] = "tp_hit"
                pos["exit_price"] = round(tp_price, 2)
                pos["exit_date"] = bar_date.strftime("%Y-%m-%d")
                pos["exit_time"] = (
                    main_pnl._exit_time_from_minutes(
                        ticker, pos["exit_date"], tp_price, "tp"
                    ) or ""
                )
                pos["exit_reason"] = f"TP +{pos['tp_pct']}% hit"
                pos["exit_pnl_pct"] = pos["tp_pct"]
                pos["exit_pnl"] = round(actual_invested * pos["tp_pct"] / 100, 2)
                pos["trading_days_held"] = trading_days_held
                return "tp_hit"

        if bar_date > entry_date and bar_date.weekday() < 5:
            trading_days_held += 1
            pos["trading_days_held"] = trading_days_held

            if trading_days_held >= max_hold and close > 0:
                pnl_pct = round((close - entry_px) / entry_px * 100, 1)
                pos["status"] = max_hold_status
                pos["exit_price"] = round(close, 2)
                pos["exit_date"] = bar_date.strftime("%Y-%m-%d")
                pos["exit_time"] = "13:00"
                pos["exit_reason"] = f"D{max_hold} max hold"
                pos["exit_pnl_pct"] = pnl_pct
                pos["exit_pnl"] = round(actual_invested * pnl_pct / 100, 2)
                return max_hold_status

        if expiry and bar_date >= expiry:
            if close > 0:
                pnl_pct = round((close - entry_px) / entry_px * 100, 1)
                pos["status"] = "expired"
                pos["exit_price"] = round(close, 2)
                pos["exit_date"] = expiry.strftime("%Y-%m-%d")
                pos["exit_time"] = "13:00"
                pos["exit_reason"] = "Option expired"
                pos["exit_pnl_pct"] = pnl_pct
                pos["exit_pnl"] = round(actual_invested * pnl_pct / 100, 2)
            else:
                pos["status"] = "expired"
                pos["exit_price"] = 0
                pos["exit_date"] = expiry.strftime("%Y-%m-%d")
                pos["exit_time"] = "13:00"
                pos["exit_reason"] = "Option expired (no print)"
                pos["exit_pnl_pct"] = -100
                pos["exit_pnl"] = round(-actual_invested, 2)
            pos["trading_days_held"] = trading_days_held
            return "expired"

    if expiry and today > expiry:
        if last_close and last_close > 0:
            pnl_pct = round((last_close - entry_px) / entry_px * 100, 1)
            pos["status"] = "expired"
            pos["exit_price"] = round(last_close, 2)
            pos["exit_date"] = (
                last_bar_date.strftime("%Y-%m-%d")
                if last_bar_date
                else expiry.strftime("%Y-%m-%d")
            )
            pos["exit_time"] = "13:00"
            pos["exit_reason"] = "Option expired (last print)"
            pos["exit_pnl_pct"] = pnl_pct
            pos["exit_pnl"] = round(actual_invested * pnl_pct / 100, 2)
        else:
            pos["status"] = "expired"
            pos["exit_price"] = 0
            pos["exit_date"] = expiry.strftime("%Y-%m-%d")
            pos["exit_time"] = "13:00"
            pos["exit_reason"] = "Option expired (no print)"
            pos["exit_pnl_pct"] = -100
            pos["exit_pnl"] = -round(actual_invested, 2)
        pos["trading_days_held"] = trading_days_held
        return "expired"

    if last_close and last_close > 0:
        pnl_pct = round((last_close - entry_px) / entry_px * 100, 1)
        pos["current_price"] = round(last_close, 2)
        pos["current_pnl_pct"] = pnl_pct
        pos["current_pnl"] = round(actual_invested * pnl_pct / 100, 2)
        pos["peak_pnl_pct"] = max(pos.get("peak_pnl_pct", 0) or 0, pnl_pct)
    pos["trading_days_held"] = trading_days_held
    return "open"


def rebuild_3m_book(target_dte: int = DEFAULT_TARGET_DTE, band: int = DEFAULT_BAND,
                    limit: int | None = None, only_funded: bool = False,
                    max_hold_days: int | None = None) -> dict:
    max_hold = int(max_hold_days if max_hold_days is not None else EXP_MAX_HOLD_DAYS)
    source = load_source_positions()
    if only_funded:
        source = [p for p in source if p.get("status") != "low_volume"]
    if limit:
        source = source[:limit]

    print(f"\nRebuilding 3-month book from {len(source)} main-book entries...")
    print(f"  Target DTE: {target_dte} ± {band} days")
    print(f"  Max hold: {max_hold} trading days (experiment; prod monthly stays 10)")
    print(f"  Output: {THREE_M_PNL_JSON}")
    print(f"  Source: {SOURCE_PNL_JSON} (read-only)")

    positions = []
    stats = Counter()
    capital_over_budget = 0  # trades where entry_amount > ENTRY_AMOUNT

    for i, src in enumerate(source, 1):
        sym = src.get("symbol", "?")
        strike = src.get("strike", 0)
        entry = src.get("entry_date", "?")
        print(
            f"  [{i}/{len(source)}] {sym} {src.get('direction')} "
            f"${strike} @ {entry}...",
            end="",
            flush=True,
        )
        pos = build_3m_position(src, target_dte=target_dte, band=band)
        pos["max_hold_days"] = max_hold
        if not pos.get("resolved_3m"):
            print(f" NO 3M ({pos.get('exit_reason')})")
            stats["no_3m"] += 1
            positions.append(pos)
            continue

        print(
            f" 3m {pos['expiry']} "
            f"(DTE {pos.get('dte_at_entry')}, was {pos.get('monthly_expiry')})",
            end="",
            flush=True,
        )
        result = simulate_position_historical(pos, max_hold_days=max_hold)
        stats["resolved"] += 1
        stats[f"sim_{result}"] += 1

        if pos.get("entry_price"):
            if (pos.get("entry_amount") or 0) > main_pnl.ENTRY_AMOUNT + 1:
                capital_over_budget += 1
            pnl = (
                pos.get("exit_pnl")
                if pos["status"] not in ("open",)
                else pos.get("current_pnl", 0)
            )
            pct = (
                pos.get("exit_pnl_pct")
                if pos["status"] not in ("open",)
                else pos.get("current_pnl_pct", 0)
            )
            sign = "+" if (pnl or 0) >= 0 else ""
            print(
                f" → {result} entry ${pos['entry_price']:.2f} "
                f"({pos.get('num_contracts')}x ${pos.get('entry_amount', 0):,.0f}) "
                f"{sign}${pnl or 0:,.0f} ({sign}{pct or 0:.0f}%)"
            )
        else:
            print(f" → {result}")
        positions.append(pos)

    data = {
        "positions": positions,
        "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_invested": 0,
        "total_pnl": 0,
        "source_file": SOURCE_PNL_JSON,
        "build_meta": {
            "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "n_source": len(source),
            "n_resolved": stats["resolved"],
            "n_no_3m": stats["no_3m"],
            "target_dte": target_dte,
            "band": band,
            "only_funded": only_funded,
            "entry_amount_budget": main_pnl.ENTRY_AMOUNT,
            "max_hold_days": max_hold,
            "hard_sl_pct": main_pnl.HARD_SL_PCT,
            "min_contract_volume": EXP_MIN_CONTRACT_VOLUME,
            "n_over_budget_capital": capital_over_budget,
            "sim_counts": {
                k.replace("sim_", ""): v
                for k, v in stats.items()
                if k.startswith("sim_")
            },
            "rule": (
                f"listed expiry closest to {target_dte} DTE "
                f"(band ±{band}; prefer 3rd-Friday monthlies on ties)"
            ),
        },
    }

    closed_pos = [
        p for p in data["positions"]
        if p["status"] not in ("open", "low_volume", "no_3m_contract")
    ]
    data["total_pnl"] = sum(p.get("exit_pnl", 0) or 0 for p in closed_pos)
    data["total_invested"] = sum(
        p.get("entry_amount", main_pnl.ENTRY_AMOUNT) or main_pnl.ENTRY_AMOUNT
        for p in data["positions"]
        if p.get("entry_price")
    )

    main_pnl.PNL_JSON = THREE_M_PNL_JSON
    main_pnl.PNL_TXT = THREE_M_PNL_TXT
    save_3m_data(data)
    print(
        f"\n  Resolved: {stats['resolved']} | No 3m: {stats['no_3m']} | "
        f"Over-budget capital trades: {capital_over_budget} | "
        f"Sim: {dict((k.replace('sim_', ''), v) for k, v in stats.items() if k.startswith('sim_'))}"
    )
    return data


def update_3m_book():
    data = load_3m_data()
    if not data["positions"]:
        print("  3m book empty — run with --rebuild first.")
        return data

    max_hold = int(
        (data.get("build_meta") or {}).get("max_hold_days") or EXP_MAX_HOLD_DAYS
    )
    main_pnl.PNL_JSON = THREE_M_PNL_JSON
    main_pnl.PNL_TXT = THREE_M_PNL_TXT
    print(f"\nUpdating open 3m positions (max hold {max_hold}d)...")
    raised = main_pnl.restamp_open_take_profits(data, hold_days=max_hold)
    if raised:
        print(f"  Restamped {len(raised)} open take-profit(s) to the VIX table.")
    open_n = 0
    for pos in data["positions"]:
        if pos.get("status") == "open":
            open_n += 1
            if pos.get("entry_price"):
                filled = {
                    k: pos[k]
                    for k in (
                        "entry_price", "num_contracts", "entry_amount",
                        "entry_time", "entry_date", "signal_date", "entry_fill",
                    )
                    if k in pos
                }
                pos["status"] = "open"
                pos["exit_date"] = None
                pos["exit_price"] = None
                pos["exit_pnl"] = 0
                pos["exit_pnl_pct"] = 0
                pos["exit_reason"] = None
                for k, v in filled.items():
                    pos[k] = v
            simulate_position_historical(pos, max_hold_days=max_hold)

    print(f"  Processed open: {open_n}")
    closed_pos = [
        p for p in data["positions"]
        if p["status"] not in ("open", "low_volume", "no_3m_contract")
    ]
    data["total_pnl"] = sum(p.get("exit_pnl", 0) or 0 for p in closed_pos)
    data["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_3m_data(data)
    return data


def generate_3m_report(data):
    main_pnl.PNL_JSON = THREE_M_PNL_JSON
    main_pnl.PNL_TXT = THREE_M_PNL_TXT
    # generate_report uses main_pnl.MAX_HOLD_DAYS for the header and Days column;
    # pin it to this experiment's hold so 20d books don't print "Max Hold: 10".
    max_hold = int(
        (data.get("build_meta") or {}).get("max_hold_days") or EXP_MAX_HOLD_DAYS
    )
    old_max = main_pnl.MAX_HOLD_DAYS
    main_pnl.MAX_HOLD_DAYS = max_hold
    try:
        main_pnl.generate_report(data)
    finally:
        main_pnl.MAX_HOLD_DAYS = old_max

    if not os.path.exists(THREE_M_PNL_TXT):
        return

    with open(THREE_M_PNL_TXT, "r") as f:
        text = f.read()
    meta = data.get("build_meta") or {}
    banner = (
        "STOCK DROP 3-MONTH P&L TRACKER "
        f"(~{meta.get('target_dte', 90)} DTE; rule: {meta.get('rule', 'n/a')})\n"
        f"Built: {meta.get('built_at', data.get('last_update', 'n/a'))} | "
        f"Resolved: {meta.get('n_resolved', '?')}/{meta.get('n_source', '?')} | "
        f"No 3m: {meta.get('n_no_3m', 0)} | "
        f"Over-budget capital: {meta.get('n_over_budget_capital', 0)}\n"
    )
    text = text.replace(
        "STOCK DROP P&L TRACKER",
        banner + "STOCK DROP 3-MONTH P&L TRACKER",
        1,
    )

    missing = [p for p in data["positions"] if p.get("status") == "no_3m_contract"]
    if missing:
        lines = [f"\nNO 3M CONTRACT ({len(missing)}):", "-" * 100]
        rows = []
        for p in sorted(missing, key=lambda x: x.get("entry_date") or ""):
            rows.append([
                p.get("symbol"), p.get("direction"),
                f"${p.get('strike', 0):.2f}", p.get("entry_date"),
                p.get("monthly_expiry"), p.get("exit_reason", "-"),
            ])
        lines.append(tabulate(
            rows,
            headers=["Symbol", "Dir", "Strike", "Entry", "Monthly Exp", "Reason"],
            tablefmt="simple",
        ))
        text = text.rstrip() + "\n" + "\n".join(lines) + "\n"

    # Capital deployment note
    filled = [p for p in data["positions"] if p.get("entry_price")]
    if filled:
        amts = [p.get("entry_amount") or 0 for p in filled]
        avg_amt = sum(amts) / len(amts)
        over = sum(1 for a in amts if a > main_pnl.ENTRY_AMOUNT + 1)
        text += (
            f"\nCAPITAL DEPLOYMENT (filled trades):\n"
            f"  Budget per trade: ${main_pnl.ENTRY_AMOUNT:,.0f}\n"
            f"  Avg actual invested: ${avg_amt:,.0f}\n"
            f"  Trades over budget (min 1 contract > budget): {over}/{len(filled)}\n"
            f"  Max single entry: ${max(amts):,.0f}\n"
        )

    with open(THREE_M_PNL_TXT, "w") as f:
        f.write(text)
    print(f"\n3m report saved to: {THREE_M_PNL_TXT}")


# ── Monthly vs 3m comparison ─────────────────────────────────────────────────

def _funded_closed(positions):
    return [
        p for p in positions
        if p.get("status") not in ("open", "low_volume", "no_3m_contract", "no_weekly_contract")
        and p.get("entry_price")
    ]


def _summary_stats(positions, label: str) -> dict:
    closed = _funded_closed(positions)
    open_pos = [p for p in positions if p.get("status") == "open" and p.get("entry_price")]
    low_vol = [p for p in positions if p.get("status") == "low_volume"]
    no_c = [p for p in positions if p.get("status") in ("no_3m_contract", "no_weekly_contract")]

    realized = sum(p.get("exit_pnl", 0) or 0 for p in closed)
    unrealized = sum(p.get("current_pnl", 0) or 0 for p in open_pos)
    invested_closed = sum(
        p.get("entry_amount", main_pnl.ENTRY_AMOUNT) or main_pnl.ENTRY_AMOUNT
        for p in closed
    )
    invested_open = sum(
        p.get("entry_amount", main_pnl.ENTRY_AMOUNT) or main_pnl.ENTRY_AMOUNT
        for p in open_pos
    )
    wins = [p for p in closed if (p.get("exit_pnl") or 0) > 0]
    losses = [p for p in closed if (p.get("exit_pnl") or 0) <= 0]
    by_status = Counter(p.get("status") for p in closed)

    avg_entry = 0.0
    if closed or open_pos:
        filled = closed + open_pos
        avg_entry = sum(
            p.get("entry_amount", main_pnl.ENTRY_AMOUNT) or main_pnl.ENTRY_AMOUNT
            for p in filled
        ) / len(filled)

    return {
        "label": label,
        "n_positions": len(positions),
        "n_closed": len(closed),
        "n_open": len(open_pos),
        "n_low_volume": len(low_vol),
        "n_no_contract": len(no_c),
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": realized + unrealized,
        "invested": invested_closed + invested_open,
        "avg_entry_amount": avg_entry,
        "win_rate": (len(wins) / len(closed) * 100) if closed else 0,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "avg_win": (sum(p["exit_pnl"] for p in wins) / len(wins)) if wins else 0,
        "avg_loss": (sum(p["exit_pnl"] for p in losses) / len(losses)) if losses else 0,
        "tp_hits": by_status.get("tp_hit", 0),
        "sl_hits": by_status.get("sl_hit", 0),
        "d10_closes": sum(
            v for k, v in by_status.items()
            if isinstance(k, str) and k.startswith("d") and k.endswith("_close")
        ),
        "expired": by_status.get("expired", 0),
    }


def compare_books():
    if not os.path.exists(SOURCE_PNL_JSON):
        print("Main book missing.")
        return
    if not os.path.exists(THREE_M_PNL_JSON):
        print("3m book missing — run --rebuild first.")
        return

    with open(SOURCE_PNL_JSON) as f:
        monthly = json.load(f)
    with open(THREE_M_PNL_JSON) as f:
        three_m = json.load(f)

    m = _summary_stats(monthly.get("positions", []), "MONTHLY (main book)")
    t = _summary_stats(three_m.get("positions", []), "3-MONTH (~90 DTE)")

    def money(x):
        sign = "+" if x >= 0 else ""
        return f"{sign}${x:,.2f}"

    def pct(x):
        sign = "+" if x >= 0 else ""
        return f"{sign}{x:.1f}%"

    lines = []
    lines.append("=" * 100)
    lines.append("STOCK DROP P&L — MONTHLY vs 3-MONTH COMPARISON")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    meta = three_m.get("build_meta") or {}
    if meta:
        lines.append(
            f"3m rule: {meta.get('rule')} | "
            f"Max hold: {meta.get('max_hold_days', '?')}d | "
            f"Min vol: {meta.get('min_contract_volume', '?')} | "
            f"Resolved {meta.get('n_resolved')}/{meta.get('n_source')} | "
            f"Built {meta.get('built_at')}"
        )
    lines.append("=" * 100)

    table = [
        ["Positions (all)", m["n_positions"], t["n_positions"]],
        ["Closed (funded)", m["n_closed"], t["n_closed"]],
        ["Open (funded)", m["n_open"], t["n_open"]],
        ["Low-volume skips", m["n_low_volume"], t["n_low_volume"]],
        ["No contract", m["n_no_contract"], t["n_no_contract"]],
        ["Total invested", money(m["invested"]), money(t["invested"])],
        ["Avg $ / filled trade", money(m["avg_entry_amount"]), money(t["avg_entry_amount"])],
        ["Realized P&L", money(m["realized_pnl"]), money(t["realized_pnl"])],
        ["Unrealized P&L", money(m["unrealized_pnl"]), money(t["unrealized_pnl"])],
        ["Total P&L", money(m["total_pnl"]), money(t["total_pnl"])],
        [
            "ROI",
            pct(m["total_pnl"] / m["invested"] * 100) if m["invested"] else "-",
            pct(t["total_pnl"] / t["invested"] * 100) if t["invested"] else "-",
        ],
        [
            "Win rate",
            f"{m['n_wins']}/{m['n_closed']} ({m['win_rate']:.0f}%)",
            f"{t['n_wins']}/{t['n_closed']} ({t['win_rate']:.0f}%)",
        ],
        ["Avg win $", money(m["avg_win"]), money(t["avg_win"])],
        ["Avg loss $", money(m["avg_loss"]), money(t["avg_loss"])],
        ["TP hits", m["tp_hits"], t["tp_hits"]],
        ["SL hits", m["sl_hits"], t["sl_hits"]],
        ["Max-hold closes", m["d10_closes"], t["d10_closes"]],
        ["Expired", m["expired"], t["expired"]],
    ]
    hold_label = meta.get("max_hold_days", EXP_MAX_HOLD_DAYS)
    lines.append(tabulate(
        table,
        headers=[
            "Metric",
            "Monthly (~30-45 DTE, 10d hold)",
            f"3-Month (~90 DTE, {hold_label}d hold)",
        ],
        tablefmt="simple",
        numalign="right",
    ))

    delta = t["total_pnl"] - m["total_pnl"]
    lines.append("")
    lines.append(
        f"  3m − Monthly total P&L: {money(delta)} "
        f"({'3m better' if delta > 0 else 'monthlies better' if delta < 0 else 'tie'})"
    )
    if t["avg_entry_amount"] and m["avg_entry_amount"]:
        lines.append(
            f"  Avg capital ratio (3m / monthly): "
            f"{t['avg_entry_amount'] / m['avg_entry_amount']:.2f}x"
        )

    # Pairwise
    t_by_key = {
        p.get("source_key"): p
        for p in three_m.get("positions", [])
        if p.get("source_key")
    }
    pairs = []
    for mp in monthly.get("positions", []):
        key = source_key(mp)
        tp = t_by_key.get(key)
        if not tp or not mp.get("entry_price") or not tp.get("entry_price"):
            continue
        if mp.get("status") in ("open", "low_volume"):
            m_pnl = mp.get("current_pnl", 0) or 0
        else:
            m_pnl = mp.get("exit_pnl", 0) or 0
        if tp.get("status") in ("open", "low_volume", "no_3m_contract"):
            t_pnl = tp.get("current_pnl", 0) or 0 if tp.get("status") == "open" else 0
        else:
            t_pnl = tp.get("exit_pnl", 0) or 0
        pairs.append({
            "symbol": mp["symbol"],
            "entry_date": mp.get("entry_date"),
            "strike": mp.get("strike"),
            "m_exp": mp.get("expiry"),
            "t_exp": tp.get("expiry"),
            "t_dte": tp.get("dte_at_entry"),
            "m_status": mp.get("status"),
            "t_status": tp.get("status"),
            "m_entry_amt": mp.get("entry_amount") or main_pnl.ENTRY_AMOUNT,
            "t_entry_amt": tp.get("entry_amount") or main_pnl.ENTRY_AMOUNT,
            "m_entry_px": mp.get("entry_price"),
            "t_entry_px": tp.get("entry_price"),
            "m_pnl": m_pnl,
            "t_pnl": t_pnl,
            "delta": t_pnl - m_pnl,
            "m_pct": (
                mp.get("exit_pnl_pct")
                if mp.get("status") not in ("open", "low_volume")
                else mp.get("current_pnl_pct", 0)
            ) or 0,
            "t_pct": (
                tp.get("exit_pnl_pct")
                if tp.get("status") not in ("open", "low_volume", "no_3m_contract")
                else tp.get("current_pnl_pct", 0)
            ) or 0,
        })

    if pairs:
        better_t = sum(1 for p in pairs if p["delta"] > 0)
        better_m = sum(1 for p in pairs if p["delta"] < 0)
        ties = sum(1 for p in pairs if p["delta"] == 0)
        pair_delta = sum(p["delta"] for p in pairs)
        avg_m_amt = sum(p["m_entry_amt"] for p in pairs) / len(pairs)
        avg_t_amt = sum(p["t_entry_amt"] for p in pairs) / len(pairs)
        avg_m_px = sum(p["m_entry_px"] for p in pairs) / len(pairs)
        avg_t_px = sum(p["t_entry_px"] for p in pairs) / len(pairs)
        avg_m_pct = sum(p["m_pct"] for p in pairs) / len(pairs)
        avg_t_pct = sum(p["t_pct"] for p in pairs) / len(pairs)

        lines.append("")
        lines.append(f"PAIRWISE (both filled, n={len(pairs)}):")
        lines.append("-" * 100)
        lines.append(
            f"  3m better: {better_t}  |  Monthly better: {better_m}  |  Tie: {ties}"
        )
        lines.append(f"  Sum of (3m − monthly) $ P&L: {money(pair_delta)}")
        lines.append(
            f"  Avg entry premium: monthly ${avg_m_px:.2f} → 3m ${avg_t_px:.2f} "
            f"({avg_t_px / avg_m_px:.2f}x)" if avg_m_px else ""
        )
        lines.append(
            f"  Avg capital deployed: monthly ${avg_m_amt:,.0f} → 3m ${avg_t_amt:,.0f} "
            f"({avg_t_amt / avg_m_amt:.2f}x)" if avg_m_amt else ""
        )
        lines.append(
            f"  Avg P&L % (paired): monthly {avg_m_pct:+.1f}% → 3m {avg_t_pct:+.1f}%"
        )

        ranked = sorted(pairs, key=lambda p: p["delta"], reverse=True)
        lines.append("")
        lines.append("  Top 10 3m OUTPERFORMANCE vs monthly:")
        rows = []
        for p in ranked[:10]:
            rows.append([
                p["symbol"], p["entry_date"], f"${p['strike']}",
                p["m_exp"], p["t_exp"], p.get("t_dte"),
                p["m_status"], p["t_status"],
                f"${p['m_entry_px']:.2f}", f"${p['t_entry_px']:.2f}",
                money(p["m_pnl"]), money(p["t_pnl"]), money(p["delta"]),
            ])
        lines.append(tabulate(
            rows,
            headers=[
                "Sym", "Entry", "Strike", "M-Exp", "3m-Exp", "DTE",
                "M-Stat", "3m-Stat", "M-Px", "3m-Px", "M-PnL", "3m-PnL", "Δ",
            ],
            tablefmt="simple",
        ))

        lines.append("")
        lines.append("  Top 10 3m UNDERPERFORMANCE vs monthly:")
        rows = []
        for p in ranked[-10:][::-1]:
            rows.append([
                p["symbol"], p["entry_date"], f"${p['strike']}",
                p["m_exp"], p["t_exp"], p.get("t_dte"),
                p["m_status"], p["t_status"],
                f"${p['m_entry_px']:.2f}", f"${p['t_entry_px']:.2f}",
                money(p["m_pnl"]), money(p["t_pnl"]), money(p["delta"]),
            ])
        lines.append(tabulate(
            rows,
            headers=[
                "Sym", "Entry", "Strike", "M-Exp", "3m-Exp", "DTE",
                "M-Stat", "3m-Stat", "M-Px", "3m-Px", "M-PnL", "3m-PnL", "Δ",
            ],
            tablefmt="simple",
        ))

        lines.append("")
        lines.append("  Exit reason shift (paired):")
        m_status = Counter(p["m_status"] for p in pairs)
        t_status = Counter(p["t_status"] for p in pairs)
        all_st = sorted(set(m_status) | set(t_status))
        shift_rows = [[s, m_status.get(s, 0), t_status.get(s, 0)] for s in all_st]
        lines.append(tabulate(
            shift_rows,
            headers=["Status", "Monthly", "3-Month"],
            tablefmt="simple",
        ))

        dtes = [p["t_dte"] for p in pairs if p.get("t_dte") is not None]
        if dtes:
            lines.append("")
            lines.append(
                f"  3m DTE at entry — min/med/max: "
                f"{min(dtes)} / {sorted(dtes)[len(dtes)//2]} / {max(dtes)}"
            )

    lines.append("=" * 100)
    report = "\n".join(lines)
    print(f"\n{report}")
    with open(COMPARE_TXT, "w") as f:
        f.write(report)
    print(f"\nComparison saved to: {COMPARE_TXT}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Stock Drop 3-Month P&L Tracker (EXPERIMENT) — same strikes, "
            "~90 DTE expiry. Does not modify the live monthly book."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --rebuild\n"
            "  %(prog)s --rebuild --max-hold 20          # 20d hold, separate files\n"
            "  %(prog)s --rebuild --limit 20\n"
            "  %(prog)s --rebuild --target-dte 90 --band 30\n"
            "  %(prog)s --compare --max-hold 20\n"
        ),
    )
    parser.add_argument("--rebuild", action="store_true",
                        help="Rebuild 3m book from main stock_drop_pnl.json")
    parser.add_argument("--update", action="store_true",
                        help="Update open positions in existing 3m book")
    parser.add_argument("--summary", action="store_true",
                        help="Print 3m P&L report only")
    parser.add_argument("--compare", action="store_true",
                        help="Compare monthly main book vs 3m book")
    parser.add_argument("--target-dte", type=int, default=DEFAULT_TARGET_DTE,
                        help=f"Target days to expiry (default {DEFAULT_TARGET_DTE})")
    parser.add_argument("--band", type=int, default=DEFAULT_BAND,
                        help=f"± days around target DTE (default {DEFAULT_BAND})")
    parser.add_argument(
        "--max-hold", type=int, default=DEFAULT_MAX_HOLD_DAYS, metavar="DAYS",
        help=(
            f"Max trading days to hold (default {DEFAULT_MAX_HOLD_DAYS}). "
            "Use 20 for the extended-hold experiment. Writes to separate "
            "files when not 10 so the 10d run is preserved."
        ),
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Only first N source positions (smoke test)")
    parser.add_argument("--only-funded", action="store_true",
                        help="Skip main-book low_volume rows when rebuilding")
    parser.add_argument("--force", "-f", action="store_true",
                        help="Run on weekends")
    args = parser.parse_args()

    # Route outputs before any load/save (keeps 10d and 20d books separate)
    configure_experiment(args.max_hold)

    if not any([args.rebuild, args.update, args.summary, args.compare]):
        if os.path.exists(THREE_M_PNL_JSON):
            args.update = True
            args.compare = True
        else:
            args.rebuild = True
            args.compare = True

    needs_market = args.rebuild or args.update
    if not args.force and needs_market and datetime.now().weekday() >= 5:
        print("Weekend — use --force to rebuild/update, or --summary / --compare.")
        return

    if not POLYGON_API_KEY:
        print("POLYGON_API_KEY not set in .env")
        return

    print("=" * 60)
    print("STOCK DROP 3-MONTH P&L TRACKER  *** EXPERIMENT ONLY ***")
    print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(
        f"Entry budget: ${main_pnl.ENTRY_AMOUNT:,} | "
        f"Max Hold: {args.max_hold}d (prod monthly stays 10) | "
        f"Target DTE: {args.target_dte}±{args.band} | "
        f"Min fill vol: {EXP_MIN_CONTRACT_VOLUME} (prod=50)"
    )
    print(f"Output: {THREE_M_PNL_JSON}")
    print("=" * 60)

    data = None
    if args.rebuild:
        data = rebuild_3m_book(
            target_dte=args.target_dte,
            band=args.band,
            limit=args.limit,
            only_funded=args.only_funded,
            max_hold_days=args.max_hold,
        )
        generate_3m_report(data)
    elif args.update:
        data = update_3m_book()
        generate_3m_report(data)
    elif args.summary:
        data = load_3m_data()
        if not data["positions"]:
            print("3m book empty — run --rebuild first.")
            return
        generate_3m_report(data)

    if args.compare:
        compare_books()


if __name__ == "__main__":
    main()
