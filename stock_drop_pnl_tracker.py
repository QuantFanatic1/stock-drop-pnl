#!/usr/bin/env python3
"""
Stock Drop P&L Tracker
-----------------------
Tracks option positions from the stock drop watchlist with $1,000 entry
per trade, enforcing TP/SL rules.

Positions are logged when the watchlist generates plays. A daily update
checks current option prices and closes positions that hit TP, SL, or
reach day 10 (max hold).

Usage:
    python stock_drop_pnl_tracker.py                # Update all open positions
    python stock_drop_pnl_tracker.py --summary      # Show P&L summary only
    python stock_drop_pnl_tracker.py --add-from-watchlist 2026-03-23  # Add from watchlist
    python stock_drop_pnl_tracker.py --force        # Run on weekends

Output: Stock_Drop_Alert/stock_drop_pnl.json (positions)
        Stock_Drop_Alert/stock_drop_pnl.txt  (human-readable log)
"""

import math
import os
import sys
import json
import time
import argparse
import requests
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from tabulate import tabulate

from stock_drop_exit_rules import STANDARD_SL_PCT, resolve_take_profit

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_SCRIPT_DIR, '.env'))

POLYGON_API_KEY = os.getenv('POLYGON_API_KEY')
POLYGON_BASE = 'https://api.polygon.io'

ALERT_DIR = os.path.join(_SCRIPT_DIR, 'Stock_Drop_Alert')
os.makedirs(ALERT_DIR, exist_ok=True)

PNL_JSON = os.path.join(ALERT_DIR, 'stock_drop_pnl.json')
PNL_TXT = os.path.join(ALERT_DIR, 'stock_drop_pnl.txt')

ENTRY_AMOUNT = 2_000  # $2,000 per position
MAX_HOLD_DAYS = 10    # 10 trading days max
HARD_SL_PCT = STANDARD_SL_PCT  # Flat -40% stop on every position
MIN_CONTRACT_VOLUME = 50   # Min option volume on entry day to fill (else wait/skip)
FILL_WINDOW_DAYS = 3       # Trading days after entry_date to keep checking for fill

# Polygon rate limiting
_last_polygon_ts = 0.0
POLYGON_MIN_INTERVAL = 0.35


def _polygon_get(url, params):
    """GET with pacing for Polygon rate limits."""
    global _last_polygon_ts
    elapsed = time.time() - _last_polygon_ts
    if elapsed < POLYGON_MIN_INTERVAL:
        time.sleep(POLYGON_MIN_INTERVAL - elapsed)
    try:
        resp = requests.get(url, params=params, timeout=15)
    except requests.RequestException:
        return None
    _last_polygon_ts = time.time()
    if resp.status_code == 429:
        time.sleep(2)
        try:
            resp = requests.get(url, params=params, timeout=15)
        except requests.RequestException:
            return None
        _last_polygon_ts = time.time()
    return resp


# ── Data persistence ──────────────────────────────────────────────────────────

def load_pnl_data():
    """Load P&L tracker data."""
    if os.path.exists(PNL_JSON):
        with open(PNL_JSON, 'r') as f:
            return json.load(f)
    return {
        'positions': [],
        'last_update': None,
        'total_invested': 0,
        'total_pnl': 0,
    }


def save_pnl_data(data):
    """Save P&L tracker data."""
    with open(PNL_JSON, 'w') as f:
        json.dump(data, f, indent=2)


# ── Option price fetching ────────────────────────────────────────────────────

def get_option_quote(option_ticker):
    """
    Get current option price from Polygon snapshot.
    Returns dict with 'price', 'bid', 'ask', 'high', 'low', 'volume' or None.
    """
    url = f'{POLYGON_BASE}/v3/snapshot/options/{option_ticker.split(":")[-1] if ":" in option_ticker else option_ticker}'

    # Try the direct ticker snapshot
    url2 = f'{POLYGON_BASE}/v2/snapshot/locale/us/markets/options/{option_ticker}'
    params = {'apiKey': POLYGON_API_KEY}

    resp = _polygon_get(url2, params)
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            results = data.get('results', data)
            if isinstance(results, dict):
                day = results.get('day', {})
                last = results.get('last_quote', results.get('lastQuote', {}))
                return {
                    'price': day.get('close') or day.get('last', 0) or results.get('value', 0),
                    'bid': last.get('bid', 0),
                    'ask': last.get('ask', 0),
                    'high': day.get('high', 0),
                    'low': day.get('low', 0),
                    'volume': day.get('volume', 0),
                }
        except (ValueError, KeyError):
            pass

    # Fallback: use previous day close from aggregates
    prev_url = f'{POLYGON_BASE}/v2/aggs/ticker/{option_ticker}/prev'
    resp = _polygon_get(prev_url, {'adjusted': 'false', 'apiKey': POLYGON_API_KEY})
    if resp and resp.status_code == 200:
        try:
            results = resp.json().get('results', [])
            if results:
                bar = results[0]
                return {
                    'price': bar.get('c', 0),
                    'bid': 0,
                    'ask': 0,
                    'high': bar.get('h', 0),
                    'low': bar.get('l', 0),
                    'volume': bar.get('v', 0),
                }
        except (ValueError, KeyError):
            pass

    return None


def get_option_entry_price(option_ticker, entry_date_str,
                           min_volume=MIN_CONTRACT_VOLUME,
                           window_days=FILL_WINDOW_DAYS):
    """
    Find the first day with volume >= min_volume in the window
    [entry_date, entry_date + window_days trading days] and return
    (open_price, fill_date_str) for that day.

    If no day in the window meets the volume threshold, returns
    (None, None) and the caller decides whether to wait (window not
    yet elapsed) or close as low_volume (window elapsed).
    """
    start = datetime.strptime(entry_date_str, '%Y-%m-%d').date()
    # Walk forward to find the last trading day of the window
    end = start
    counted = 0
    cursor = start
    while counted <= window_days:
        if cursor.weekday() < 5:
            end = cursor
            counted += 1
        cursor += timedelta(days=1)

    today = date.today()
    if end > today:
        end = today  # don't query future bars

    url = (f'{POLYGON_BASE}/v2/aggs/ticker/{option_ticker}/range/1/day/'
           f'{start.strftime("%Y-%m-%d")}/{end.strftime("%Y-%m-%d")}')
    params = {'adjusted': 'false', 'sort': 'asc', 'limit': 20, 'apiKey': POLYGON_API_KEY}

    resp = _polygon_get(url, params)
    if not resp or resp.status_code != 200:
        return None, None

    try:
        bars = resp.json().get('results', []) or []
    except (ValueError, KeyError):
        return None, None

    for bar in bars:
        vol = int(bar.get('v', 0) or 0)
        open_px = float(bar.get('o', 0) or 0)
        if vol >= min_volume and open_px > 0:
            bar_date = datetime.utcfromtimestamp(bar['t'] / 1000).date()
            return open_px, bar_date.strftime('%Y-%m-%d')

    return None, None


def _ts_to_pt(ms):
    """Convert a Polygon ms timestamp to 'HH:MM' Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo('America/Los_Angeles'))
        return dt.strftime('%H:%M')
    except Exception:
        # Fallback: approximate PDT (UTC-7) Mar-Oct, PST (UTC-8) otherwise
        dt = datetime.utcfromtimestamp(ms / 1000)
        offset = 7 if 3 <= dt.month <= 10 else 8
        return (dt - timedelta(hours=offset)).strftime('%H:%M')


def _fetch_minute_bars(option_ticker, date_str):
    """All minute bars for an option on date_str, or []."""
    url = (f'{POLYGON_BASE}/v2/aggs/ticker/{option_ticker}/range/1/minute/'
           f'{date_str}/{date_str}')
    params = {'adjusted': 'false', 'sort': 'asc', 'limit': 50000,
              'apiKey': POLYGON_API_KEY}
    resp = _polygon_get(url, params)
    if not resp or resp.status_code != 200:
        return []
    try:
        return resp.json().get('results', []) or []
    except (ValueError, KeyError):
        return []


def _minute_price_near(option_ticker, date_str, hour_et=15, minute_et=45):
    """
    Price of the option minute bar closest to HH:MM ET on date_str (default
    15:45 ET = 12:45 PT, the v2 confirmation/entry time). Returns
    (price, 'HH:MM' PT) or (None, None) if no minute data.
    """
    bars = _fetch_minute_bars(option_ticker, date_str)
    if not bars:
        return None, None

    try:
        from zoneinfo import ZoneInfo
        target_dt = datetime.strptime(date_str, '%Y-%m-%d').replace(
            hour=hour_et, minute=minute_et,
            tzinfo=ZoneInfo('America/New_York'))
        target_ms = target_dt.timestamp() * 1000
    except Exception:
        # Fallback: approximate EDT (UTC-4) Mar-Oct, EST (UTC-5) otherwise
        d = datetime.strptime(date_str, '%Y-%m-%d')
        offset = 4 if 3 <= d.month <= 10 else 5
        target_ms = ((d.replace(hour=hour_et, minute=minute_et)
                      - datetime(1970, 1, 1)).total_seconds()
                     + offset * 3600) * 1000

    best = min(bars, key=lambda b: abs(b.get('t', 0) - target_ms))
    px = float(best.get('c', 0) or 0)
    if px <= 0:
        return None, None
    return px, _ts_to_pt(best.get('t', target_ms))


def _exit_time_from_minutes(option_ticker, date_str, level_price, mode):
    """
    'HH:MM' PT of the first minute bar on date_str that crossed level_price
    (mode 'sl': bar low <= level; mode 'tp': bar high >= level), or None if
    no minute data shows the cross.
    """
    for bar in _fetch_minute_bars(option_ticker, date_str):
        if mode == 'sl':
            lo = float(bar.get('l', 0) or 0)
            if lo > 0 and lo <= level_price:
                return _ts_to_pt(bar.get('t', 0))
        else:
            hi = float(bar.get('h', 0) or 0)
            if hi >= level_price > 0:
                return _ts_to_pt(bar.get('t', 0))
    return None


def get_option_close_price(option_ticker, entry_date_str,
                           min_volume=MIN_CONTRACT_VOLUME,
                           window_days=FILL_WINDOW_DAYS):
    """
    v2 confirmation-entry fill: find the first day with volume >= min_volume
    in the window [entry_date, entry_date + window_days trading days], then
    fill at the minute bar closest to 15:45 ET (12:45 PT — the actual
    confirmation time) on that day. Falls back to the daily close when no
    minute data exists. Returns (price, fill_date_str).
    """
    start = datetime.strptime(entry_date_str, '%Y-%m-%d').date()
    end = start
    counted = 0
    cursor = start
    while counted <= window_days:
        if cursor.weekday() < 5:
            end = cursor
            counted += 1
        cursor += timedelta(days=1)

    today = date.today()
    if end > today:
        end = today

    url = (f'{POLYGON_BASE}/v2/aggs/ticker/{option_ticker}/range/1/day/'
           f'{start.strftime("%Y-%m-%d")}/{end.strftime("%Y-%m-%d")}')
    params = {'adjusted': 'false', 'sort': 'asc', 'limit': 20, 'apiKey': POLYGON_API_KEY}

    resp = _polygon_get(url, params)
    if not resp or resp.status_code != 200:
        return None, None

    try:
        bars = resp.json().get('results', []) or []
    except (ValueError, KeyError):
        return None, None

    for bar in bars:
        vol = int(bar.get('v', 0) or 0)
        close_px = float(bar.get('c', 0) or 0)
        if vol >= min_volume and close_px > 0:
            bar_date = datetime.utcfromtimestamp(bar['t'] / 1000).date()
            fill_date_str = bar_date.strftime('%Y-%m-%d')
            px_1245, fill_time = _minute_price_near(option_ticker, fill_date_str)
            if px_1245:
                return px_1245, fill_date_str, fill_time
            return close_px, fill_date_str, '13:00'

    return None, None, None


def window_elapsed(entry_date_str, window_days=FILL_WINDOW_DAYS):
    """True if today is past the last trading day of the fill window."""
    start = datetime.strptime(entry_date_str, '%Y-%m-%d').date()
    end = start
    counted = 0
    cursor = start
    while counted <= window_days:
        if cursor.weekday() < 5:
            end = cursor
            counted += 1
        cursor += timedelta(days=1)
    return date.today() > end


def get_option_daily_bars(option_ticker, start_date, end_date):
    """Get daily OHLCV bars for tracking TP/SL hits."""
    url = (f'{POLYGON_BASE}/v2/aggs/ticker/{option_ticker}/range/1/day/'
           f'{start_date}/{end_date}')
    params = {'adjusted': 'false', 'sort': 'asc', 'limit': 50, 'apiKey': POLYGON_API_KEY}

    resp = _polygon_get(url, params)
    if resp and resp.status_code == 200:
        try:
            return resp.json().get('results', [])
        except (ValueError, KeyError):
            pass
    return []


# ── Position management ──────────────────────────────────────────────────────

def add_position(data, symbol, direction, drop_pct, stock_price, strike,
                 expiry, option_ticker, tp_pct, sl_pct, tier, entry_date,
                 options_volume=None, win_rate=None, avg_return=None,
                 **extras):
    """Add a new position to the tracker. Extra keyword fields (e.g. v2
    decision/score/entry_fill) are stored on the position as-is."""
    # Check for duplicates
    for pos in data['positions']:
        if (pos['symbol'] == symbol and pos['entry_date'] == entry_date
                and pos['status'] == 'open'):
            return False  # Already exists

    position = {
        'symbol': symbol,
        'direction': direction,
        'drop_pct': drop_pct,
        'stock_price_at_drop': stock_price,
        'strike': strike,
        'expiry': expiry,
        'option_ticker': option_ticker,
        'tp_pct': tp_pct,
        'sl_pct': HARD_SL_PCT,
        'tier': tier,
        'entry_date': entry_date,
        'entry_amount': ENTRY_AMOUNT,
        'entry_price': None,       # Filled on first update after entry
        'num_contracts': None,     # Calculated from entry_price
        'current_price': None,
        'current_pnl': 0,
        'current_pnl_pct': 0,
        'peak_pnl_pct': 0,
        'status': 'open',          # open, tp_hit, sl_hit, d10_close, expired
        'exit_date': None,
        'exit_price': None,
        'exit_pnl': 0,
        'exit_pnl_pct': 0,
        'exit_reason': None,
        'trading_days_held': 0,
        'options_volume': options_volume,
        'hist_win_rate': win_rate,
        'hist_avg_return': avg_return,
        'created': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    position.update(extras)

    data['positions'].append(position)
    data['total_invested'] += ENTRY_AMOUNT
    return True


def count_trading_days(start_str, end_date):
    """Count weekdays between start date string and end date."""
    start = datetime.strptime(start_str, '%Y-%m-%d').date()
    count = 0
    cursor = start
    while cursor <= end_date:
        if cursor.weekday() < 5:
            count += 1
        cursor += timedelta(days=1)
    return count - 1  # Don't count entry day itself


def _tp_int(value):
    """Stored take-profit as an int, or None for a hold / blank."""
    if value is None:
        return None
    try:
        if isinstance(value, float) and value != value:
            return None
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def restamp_open_take_profits(data, raise_only=True, hold_days=None,
                              apply_new_targets=False):
    """Set open positions to the replay exit for this book's hold.

    A percentage the replay retired is cleared, so the name holds the
    window. That does not sell it. An open target already above a percentage
    the cell still uses is left in place, so the next mark does not stop the
    trade out at a lower number.

    An open hold does not gain a new percentage unless apply_new_targets is
    set. Putting +150% on a hold whose high already cleared it would sell
    the name on the next price walk. New entries get their percentage at
    intake. Closed trades are left as recorded. The stop stays -40%.
    """
    if hold_days is None:
        hold_days = MAX_HOLD_DAYS
    changed = []
    for pos in data.get('positions') or []:
        if pos.get('status') != 'open':
            continue
        pos['sl_pct'] = HARD_SL_PCT
        std = resolve_take_profit(
            pos.get('drop_pct'), tier=pos.get('tier'), hold_days=hold_days)
        if not std:
            continue
        new_tp, _sl, label = std
        old = pos.get('tp_pct')
        old_n = _tp_int(old)
        new_n = None if new_tp is None else int(new_tp)
        if new_n is None:
            chosen = None
        elif old_n is None:
            if not apply_new_targets:
                continue
            chosen = new_n
        elif raise_only:
            chosen = max(old_n, new_n)
        else:
            chosen = new_n
        if _tp_int(old) != chosen:
            pos['tp_pct_prev'] = old
            pos['tp_pct'] = chosen
            pos['tp_rule'] = label
            changed.append((
                pos.get('symbol'), pos.get('entry_date'), old, chosen, label,
            ))
        elif pos.get('tp_rule') != label:
            pos['tp_rule'] = label
    return changed


def _row_take_profit(drop_pct, vix, tier, csv_tp, hold_days=None):
    """Replay table for a new entry. CSV percentiles are not the exit."""
    if hold_days is None:
        hold_days = MAX_HOLD_DAYS
    std = resolve_take_profit(
        drop_pct, vix=vix, tier=tier, hold_days=hold_days)
    if std:
        tp_pct, sl_pct, _label = std
        return tp_pct, sl_pct
    if csv_tp is None or (isinstance(csv_tp, float) and csv_tp != csv_tp):
        return None, HARD_SL_PCT
    return int(float(csv_tp)), HARD_SL_PCT


def update_positions(data):
    """Update all open positions with current prices and check TP/SL."""
    raised = restamp_open_take_profits(data, hold_days=MAX_HOLD_DAYS)
    if raised:
        print(f"  Restamped {len(raised)} open take-profit(s) to the replay table:")
        for sym, entry, old, new, label in raised:
            old_s = 'hold' if old is None else f'+{old}%'
            new_s = 'hold' if new is None else f'+{new}%'
            print(f"    {sym} {entry}: {old_s} → {new_s} ({label})")
    today = date.today()
    updated = 0
    closed = 0

    open_positions = [p for p in data['positions'] if p['status'] == 'open']
    if not open_positions:
        print("  No open positions to update.")
        return 0, 0

    for pos in open_positions:
        ticker = pos['option_ticker']
        entry_date_str = pos['entry_date']
        entry_date = datetime.strptime(entry_date_str, '%Y-%m-%d').date()

        print(f"  {pos['symbol']} {pos['direction']} ${pos['strike']}...", end='', flush=True)

        # Step 1: Get entry price if not yet set (first update after entry day)
        if pos['entry_price'] is None:
            if today < entry_date:
                print(f" pending (entry {entry_date_str})")
                continue

            if pos.get('entry_fill') == 'day_close':
                entry_px, fill_date, fill_time = get_option_close_price(ticker, entry_date_str)
            else:
                entry_px, fill_date = get_option_entry_price(ticker, entry_date_str)
                fill_time = '06:30'  # legacy fill at market open PT
            if entry_px and entry_px > 0:
                pos['entry_time'] = fill_time
                # If the fill happened on a later day (entry-day vol < 50),
                # roll entry_date forward and preserve original as signal_date
                if fill_date and fill_date != entry_date_str:
                    pos['signal_date'] = entry_date_str
                    pos['entry_date'] = fill_date
                    entry_date_str = fill_date
                    entry_date = datetime.strptime(fill_date, '%Y-%m-%d').date()
                    roll_note = f" (rolled from {pos['signal_date']})"
                else:
                    roll_note = ''
                pos['entry_price'] = round(entry_px, 2)
                num_c = max(1, math.floor(ENTRY_AMOUNT / (entry_px * 100)))
                pos['num_contracts'] = num_c
                pos['entry_amount'] = round(num_c * entry_px * 100, 2)
                print(f" entry ${entry_px:.2f}{roll_note} ({num_c} contracts, ${pos['entry_amount']:,.0f} invested)", end='')
            else:
                # No qualifying volume yet. If 4-day window has passed, close as low_volume.
                if window_elapsed(pos['entry_date']):
                    pos['status'] = 'low_volume'
                    pos['exit_date'] = today.strftime('%Y-%m-%d')
                    pos['exit_reason'] = f'No fill (vol < {MIN_CONTRACT_VOLUME} for {FILL_WINDOW_DAYS + 1} days)'
                    pos['exit_pnl'] = 0
                    pos['exit_pnl_pct'] = 0
                    closed += 1
                    print(f" LOW VOLUME → closed at $0 P&L")
                else:
                    print(f" awaiting volume ≥{MIN_CONTRACT_VOLUME}")
                continue

        entry_px = pos['entry_price']
        num_contracts = pos['num_contracts']
        tp_price = entry_px * (1 + pos['tp_pct'] / 100) if pos['tp_pct'] else None
        sl_price = entry_px * (1 + pos['sl_pct'] / 100)

        # Step 2: Get daily bars from entry to today to check TP/SL
        bars = get_option_daily_bars(ticker, entry_date_str, today.strftime('%Y-%m-%d'))

        trading_days = count_trading_days(entry_date_str, today)
        pos['trading_days_held'] = trading_days

        # Walk through bars checking TP/SL
        hit_tp = False
        hit_sl = False
        exit_day_price = None

        for bar in bars:
            bar_date = datetime.utcfromtimestamp(bar['t'] / 1000).date() if 't' in bar else None
            high = bar.get('h', 0)
            low = bar.get('l', 0)
            close = bar.get('c', 0)

            # Day-close fills own only the last minutes of the entry session —
            # the entry-day high/low mostly predate the fill, so skip TP/SL
            # evaluation on that bar (a hit in the final minutes is negligible).
            if (pos.get('entry_fill') == 'day_close' and bar_date is not None
                    and bar_date == entry_date):
                continue

            if low > 0 and low <= sl_price:
                hit_sl = True
                exit_day_price = sl_price
                pos['exit_date'] = bar_date.strftime('%Y-%m-%d') if bar_date else today.strftime('%Y-%m-%d')
                break

            if tp_price and high >= tp_price:
                hit_tp = True
                exit_day_price = tp_price
                pos['exit_date'] = bar_date.strftime('%Y-%m-%d') if bar_date else today.strftime('%Y-%m-%d')
                break

        actual_invested = pos.get('entry_amount', num_contracts * entry_px * 100)

        if hit_tp:
            pos['status'] = 'tp_hit'
            pos['exit_price'] = round(exit_day_price, 2)
            pos['exit_time'] = _exit_time_from_minutes(
                ticker, pos['exit_date'], tp_price, 'tp') or ''
            pos['exit_reason'] = f"TP +{pos['tp_pct']}% hit"
            pnl_pct = pos['tp_pct']
            pos['exit_pnl_pct'] = pnl_pct
            pos['exit_pnl'] = round(actual_invested * pnl_pct / 100, 2)
            data['total_pnl'] += pos['exit_pnl']
            closed += 1
            print(f" TP HIT → +${pos['exit_pnl']:,.2f} (+{pnl_pct:.0f}%)")

        elif hit_sl:
            pos['status'] = 'sl_hit'
            pos['exit_price'] = round(exit_day_price, 2)
            pos['exit_time'] = _exit_time_from_minutes(
                ticker, pos['exit_date'], sl_price, 'sl') or ''
            pos['exit_reason'] = f"SL {pos['sl_pct']}% hit"
            pnl_pct = pos['sl_pct']
            pos['exit_pnl_pct'] = pnl_pct
            pos['exit_pnl'] = round(actual_invested * pnl_pct / 100, 2)
            data['total_pnl'] += pos['exit_pnl']
            closed += 1
            print(f" SL HIT → -${abs(pos['exit_pnl']):,.2f} ({pnl_pct:.0f}%)")

        elif trading_days >= MAX_HOLD_DAYS:
            # Max-hold close — use last available close
            if bars:
                last_close = bars[-1].get('c', 0)
            else:
                # Try current quote
                quote = get_option_quote(ticker)
                last_close = quote['price'] if quote else 0

            if last_close > 0:
                pnl_pct = round((last_close - entry_px) / entry_px * 100, 1)
                # Keep 'd10_close' status label for the main 10d book so
                # existing reports stay stable; use d{N}_close for other holds.
                pos['status'] = (
                    'd10_close' if MAX_HOLD_DAYS == 10
                    else f'd{MAX_HOLD_DAYS}_close'
                )
                pos['exit_price'] = round(last_close, 2)
                pos['exit_date'] = today.strftime('%Y-%m-%d')
                pos['exit_time'] = '13:00'
                pos['exit_reason'] = f"D{MAX_HOLD_DAYS} max hold"
                pos['exit_pnl_pct'] = pnl_pct
                pos['exit_pnl'] = round(actual_invested * pnl_pct / 100, 2)
                data['total_pnl'] += pos['exit_pnl']
                closed += 1
                sign = '+' if pnl_pct >= 0 else ''
                print(f" D10 CLOSE → {sign}${pos['exit_pnl']:,.2f} ({sign}{pnl_pct:.0f}%)")
            else:
                print(f" D10 reached, no price available")

        else:
            # Still open — update current price
            if bars:
                current = bars[-1].get('c', 0)
            else:
                quote = get_option_quote(ticker)
                current = quote['price'] if quote else 0

            if current > 0:
                pnl_pct = round((current - entry_px) / entry_px * 100, 1)
                pos['current_price'] = round(current, 2)
                pos['current_pnl_pct'] = pnl_pct
                pos['current_pnl'] = round(ENTRY_AMOUNT * pnl_pct / 100, 2)
                pos['peak_pnl_pct'] = max(pos.get('peak_pnl_pct', 0), pnl_pct)
                sign = '+' if pnl_pct >= 0 else ''
                print(f" ${current:.2f} ({sign}{pnl_pct:.0f}%) day {trading_days}/{MAX_HOLD_DAYS}")
            else:
                print(f" no price, day {trading_days}/{MAX_HOLD_DAYS}")

            updated += 1

    # Check for expired options
    for pos in data['positions']:
        if pos['status'] == 'open':
            exp = datetime.strptime(pos['expiry'], '%Y-%m-%d').date()
            if today > exp:
                if pos.get('entry_price'):
                    pos['status'] = 'expired'
                    pos['exit_reason'] = 'Option expired'
                    pos['exit_date'] = pos['expiry']
                    pos['exit_time'] = '13:00'
                    pos['exit_pnl'] = -ENTRY_AMOUNT
                    pos['exit_pnl_pct'] = -100
                    data['total_pnl'] -= ENTRY_AMOUNT
                else:
                    # Never filled and expired — treat as low_volume (no money invested)
                    pos['status'] = 'low_volume'
                    pos['exit_date'] = today.strftime('%Y-%m-%d')
                    pos['exit_reason'] = 'No fill before expiry'
                    pos['exit_pnl'] = 0
                    pos['exit_pnl_pct'] = 0

    data['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return updated, closed


# ── Reporting ────────────────────────────────────────────────────────────────

def _open_tp_cell(pos):
    """Take-profit label for an open row. Blank means hold the window."""
    tp = pos.get('tp_pct')
    if tp is None or tp == '':
        return 'Hold'
    try:
        if isinstance(tp, float) and tp != tp:
            return 'Hold'
        return f"+{int(round(float(tp)))}%"
    except (TypeError, ValueError):
        return 'Hold'


def generate_report(data):
    """Generate human-readable P&L report."""
    positions = data['positions']
    if not positions:
        print("\nNo positions tracked yet.")
        return

    open_pos = [p for p in positions if p['status'] == 'open']
    low_vol_pos = [p for p in positions if p['status'] == 'low_volume']
    closed_pos = [p for p in positions if p['status'] not in ('open', 'low_volume')]

    lines = []
    lines.append("=" * 100)
    lines.append("STOCK DROP P&L TRACKER")
    lines.append(f"Last Updated: {data.get('last_update', 'Never')}")
    lines.append(f"Entry Size: ${ENTRY_AMOUNT:,} per position  |  Max Hold: {MAX_HOLD_DAYS} trading days")
    lines.append("=" * 100)

    def _dt_cell(date_val, time_val):
        """'YYYY-MM-DD HH:MM' when a time stamp exists, else just the date."""
        d = date_val or '-'
        t = time_val or ''
        return f"{d} {t}".strip()

    # ── Open positions ──
    if open_pos:
        lines.append(f"\nOPEN POSITIONS ({len(open_pos)}):")
        lines.append("-" * 100)
        table = []
        for p in sorted(open_pos, key=lambda x: x['entry_date']):
            entry_px = p.get('entry_price')
            if entry_px:
                pnl = p.get('current_pnl', 0)
                pnl_pct = p.get('current_pnl_pct', 0)
                sign = '+' if pnl >= 0 else ''
                table.append([
                    p['symbol'],
                    p['direction'],
                    f"${p['strike']:.2f}",
                    p['expiry'],
                    _dt_cell(p['entry_date'], p.get('entry_time')),
                    f"${entry_px:.2f}",
                    f"{int(p.get('num_contracts', 0))}",
                    f"${p.get('current_price', 0):.2f}",
                    f"{sign}{pnl_pct:.0f}%",
                    f"{sign}${pnl:,.2f}",
                    f"{p.get('trading_days_held', 0)}/{MAX_HOLD_DAYS}",
                    _open_tp_cell(p),
                    p['tier'],
                ])
            else:
                table.append([
                    p['symbol'], p['direction'], f"${p['strike']:.2f}",
                    p['expiry'], p['entry_date'],
                    'Pending', '-', '-', '-', '-',
                    f"0/{MAX_HOLD_DAYS}", _open_tp_cell(p), p['tier'],
                ])
        lines.append(tabulate(table, headers=[
            'Symbol', 'Dir', 'Strike', 'Expiry', 'Entry Date',
            'Entry $', 'Contracts', 'Current $', 'P&L %', 'P&L $',
            'Days', 'TP', 'Tier'
        ], tablefmt='simple', numalign='right'))

        open_pnl = sum(p.get('current_pnl', 0) for p in open_pos)
        lines.append(f"\n  Open P&L: {'+'if open_pnl>=0 else ''}${open_pnl:,.2f}")

    # ── Closed positions ──
    if closed_pos:
        lines.append(f"\nCLOSED POSITIONS ({len(closed_pos)}):")
        lines.append("-" * 100)
        table = []
        for p in sorted(closed_pos, key=lambda x: x.get('exit_date') or x.get('entry_date') or ''):
            pnl = p.get('exit_pnl', 0)
            pnl_pct = p.get('exit_pnl_pct', 0)
            sign = '+' if pnl >= 0 else ''
            table.append([
                p['symbol'],
                p['direction'],
                f"${p['strike']:.2f}",
                p.get('expiry') or '-',
                _dt_cell(p['entry_date'], p.get('entry_time')),
                _dt_cell(p.get('exit_date'), p.get('exit_time')),
                f"${p.get('entry_price') or 0:.2f}",
                f"${p.get('exit_price') or 0:.2f}",
                f"{sign}{pnl_pct:.0f}%",
                f"{sign}${pnl:,.2f}",
                p.get('exit_reason', '-'),
            ])
        lines.append(tabulate(table, headers=[
            'Symbol', 'Dir', 'Strike', 'Expiry', 'Entry', 'Exit',
            'Entry $', 'Exit $', 'P&L %', 'P&L $', 'Reason'
        ], tablefmt='simple', numalign='right'))

    # ── Low-volume (never filled) ──
    if low_vol_pos:
        lines.append(f"\nLOW VOLUME — NOT ENTERED ({len(low_vol_pos)}):")
        lines.append("-" * 100)
        table = []
        for p in sorted(low_vol_pos, key=lambda x: x.get('entry_date') or ''):
            table.append([
                p['symbol'],
                p['direction'],
                f"${p['strike']:.2f}",
                p['expiry'],
                p.get('signal_date') or p['entry_date'],
                p.get('exit_reason', '-'),
            ])
        lines.append(tabulate(table, headers=[
            'Symbol', 'Dir', 'Strike', 'Expiry', 'Signal Date', 'Reason'
        ], tablefmt='simple', numalign='right'))

    # ── Summary ──
    # low_volume positions never invested capital — exclude from invested/wins/losses
    funded_pos = [p for p in positions if p['status'] != 'low_volume']
    total_trades = len(funded_pos)
    total_closed = len(closed_pos)
    wins = [p for p in closed_pos if p.get('exit_pnl', 0) > 0]
    losses = [p for p in closed_pos if p.get('exit_pnl', 0) <= 0]
    total_invested = total_trades * ENTRY_AMOUNT
    realized_pnl = sum(p.get('exit_pnl', 0) for p in closed_pos)
    unrealized_pnl = sum(p.get('current_pnl', 0) for p in open_pos)
    total_pnl = realized_pnl + unrealized_pnl

    lines.append(f"\n{'=' * 100}")
    lines.append("P&L SUMMARY")
    lines.append("=" * 100)
    lines.append(f"  Total positions:   {total_trades} ({len(open_pos)} open, {total_closed} closed)")
    if low_vol_pos:
        lines.append(f"  Low-volume skips:  {len(low_vol_pos)} (not entered, $0 P&L)")
    lines.append(f"  Total invested:    ${total_invested:,.2f}")
    lines.append(f"  Realized P&L:      {'+'if realized_pnl>=0 else ''}${realized_pnl:,.2f}")
    lines.append(f"  Unrealized P&L:    {'+'if unrealized_pnl>=0 else ''}${unrealized_pnl:,.2f}")
    lines.append(f"  Total P&L:         {'+'if total_pnl>=0 else ''}${total_pnl:,.2f}")

    if total_invested > 0:
        roi = total_pnl / total_invested * 100
        lines.append(f"  ROI:               {'+'if roi>=0 else ''}{roi:.1f}%")

    if total_closed > 0:
        win_rate = len(wins) / total_closed * 100
        avg_win = sum(p['exit_pnl'] for p in wins) / len(wins) if wins else 0
        avg_loss = sum(p['exit_pnl'] for p in losses) / len(losses) if losses else 0
        rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0

        lines.append(f"\n  Win Rate:          {len(wins)}/{total_closed} ({win_rate:.0f}%)")
        lines.append(f"  Avg Win:           +${avg_win:,.2f}")
        lines.append(f"  Avg Loss:          ${avg_loss:,.2f}")
        lines.append(f"  Risk:Reward:       {rr:.1f}x")

        # By exit type
        tp_hits = [p for p in closed_pos if p['status'] == 'tp_hit']
        sl_hits = [p for p in closed_pos if p['status'] == 'sl_hit']
        max_hold_closes = [
            p for p in closed_pos
            if str(p.get('status', '')).endswith('_close')
            and str(p.get('status', '')).startswith('d')
        ]
        lines.append(f"\n  TP hits:           {len(tp_hits)}")
        lines.append(f"  SL hits:           {len(sl_hits)}")
        lines.append(f"  D{MAX_HOLD_DAYS} closes:        {len(max_hold_closes)}")
        if low_vol_pos:
            lines.append(f"  Low-volume skips:  {len(low_vol_pos)}")

    lines.append("=" * 100)

    # ── v2 research sections (positions carrying a decision tag) ──
    tagged_closed = [p for p in closed_pos if p.get('decision')]
    if tagged_closed:
        decisions_present = sorted(set(p['decision'] for p in tagged_closed))
        if len(decisions_present) > 1 or tagged_closed[0].get('decision') != 'confirmed':
            lines.append(f"\nBY V2 DECISION (closed, funded):")
            lines.append("-" * 100)
            rows = []
            for dec in decisions_present:
                grp = [p for p in tagged_closed if p['decision'] == dec]
                n = len(grp)
                w = sum(1 for p in grp if p.get('exit_pnl', 0) > 0)
                avg_pct = sum(p.get('exit_pnl_pct', 0) for p in grp) / n
                tot = sum(p.get('exit_pnl', 0) for p in grp)
                rows.append([dec, n, f"{100 * w / n:.0f}%", f"{avg_pct:+.1f}%",
                             f"{'+' if tot >= 0 else ''}${tot:,.0f}"])
            lines.append(tabulate(rows, headers=[
                'Decision', 'N', 'Win%', 'Avg P&L %', 'Total P&L'],
                tablefmt='simple', numalign='right'))

        # Per-symbol granularity (closed v2 positions)
        by_sym = {}
        for p in tagged_closed:
            by_sym.setdefault(p['symbol'], []).append(p)
        sym_rows = []
        for sym, grp in by_sym.items():
            n = len(grp)
            w = sum(1 for p in grp if p.get('exit_pnl', 0) > 0)
            avg_pct = sum(p.get('exit_pnl_pct', 0) for p in grp) / n
            tot = sum(p.get('exit_pnl', 0) for p in grp)
            n_conf = sum(1 for p in grp if p['decision'] == 'confirmed')
            sym_rows.append([sym, n, n_conf, f"{100 * w / n:.0f}%",
                             f"{avg_pct:+.1f}%",
                             f"{'+' if tot >= 0 else ''}${tot:,.0f}"])
        sym_rows.sort(key=lambda r: -float(r[5].replace('$', '').replace(',', '').replace('+', '')))
        lines.append(f"\nPER-SYMBOL (closed v2 positions):")
        lines.append("-" * 100)
        lines.append(tabulate(sym_rows, headers=[
            'Symbol', 'Trades', 'Confirmed', 'Win%', 'Avg P&L %', 'Total P&L'],
            tablefmt='simple', numalign='right'))
        lines.append("=" * 100)

    report = '\n'.join(lines)

    with open(PNL_TXT, 'w') as f:
        f.write(report)

    print(f"\n{report}")
    print(f"\nReport saved to: {PNL_TXT}")


# ── Add from watchlist ───────────────────────────────────────────────────────

def add_from_watchlist(data, watchlist_date):
    """Read a watchlist file and add positions."""
    watchlist_file = os.path.join(ALERT_DIR, f'Stock_Drop_Watchlist_{watchlist_date}.txt')
    if not os.path.exists(watchlist_file):
        print(f"  Watchlist not found: {watchlist_file}")
        return 0

    # Parse the quick reference table for the data we need
    # Also parse the detailed entries for strike/expiry/ticker
    added = 0
    in_detail = False
    current = {}

    with open(watchlist_file, 'r') as f:
        for line in f:
            line = line.rstrip()

            # Match detailed entry headers: "  1. SYMBOL Dir — X.X% drop @ $XX.XX"
            import re
            header_match = re.match(
                r'\s+(\d+)\.\s+(\S+)\s+(Call|Put)\s+—\s+(-?\d+\.?\d*)%\s+drop\s+@\s+\$([\d,.]+)',
                line
            )
            if header_match:
                # Save previous entry if complete
                if current.get('symbol') and current.get('option_ticker'):
                    entry_date_str = _get_next_trading_day(watchlist_date)
                    ok = add_position(
                        data,
                        symbol=current['symbol'],
                        direction=current['direction'],
                        drop_pct=current['drop_pct'],
                        stock_price=current['stock_price'],
                        strike=current['strike'],
                        expiry=current['expiry'],
                        option_ticker=current['option_ticker'],
                        tp_pct=current['tp_pct'],
                        sl_pct=current['sl_pct'],
                        tier=current.get('tier', ''),
                        entry_date=entry_date_str,
                        options_volume=current.get('options_volume'),
                        win_rate=current.get('win_rate'),
                        avg_return=current.get('avg_return'),
                    )
                    if ok:
                        added += 1
                        print(f"  + {current['symbol']} {current['direction']} ${current['strike']} "
                              f"exp {current['expiry']} → entry {entry_date_str}")

                current = {
                    'symbol': header_match.group(2),
                    'direction': header_match.group(3),
                    'drop_pct': float(header_match.group(4)),
                    'stock_price': float(header_match.group(5).replace(',', '')),
                }
                continue

            # Match tier line
            if current and 'Tier:' in line:
                tier_match = re.search(r'Tier:\s+(.+)', line)
                if tier_match:
                    current['tier'] = tier_match.group(1).strip()

            # Match TP/SL line
            if current and 'TP:' in line and 'SL:' in line:
                tp_match = re.search(r'TP:\s+\+?(\d+)%', line)
                sl_match = re.search(r'SL:\s+(-\d+)%', line)
                if tp_match:
                    current['tp_pct'] = int(tp_match.group(1))
                else:
                    current['tp_pct'] = None  # Hold to D10
                if sl_match:
                    current['sl_pct'] = int(sl_match.group(1))

            # Match strike/expiry/ticker — supports both old and new formats
            # Old: "Strike: $123.00 (first OTM call)  |  Expiry: 2026-05-15  |  Ticker: O:..."
            # New: "Expiry: 2026-05-15" then "1. $123.00  (vol: 5,432) ← BEST" then "Ticker: O:..."
            if current and 'Strike:' in line:
                strike_match = re.search(r'Strike:\s+\$([\d.]+)', line)
                expiry_match = re.search(r'Expiry:\s+(\d{4}-\d{2}-\d{2})', line)
                ticker_match = re.search(r'Ticker:\s+(\S+)', line)
                if strike_match:
                    current['strike'] = float(strike_match.group(1))
                if expiry_match:
                    current['expiry'] = expiry_match.group(1)
                if ticker_match:
                    current['option_ticker'] = ticker_match.group(1)

            # New format: Expiry on its own line
            if current and 'Expiry:' in line and 'Strike:' not in line:
                expiry_match = re.search(r'Expiry:\s+(\d{4}-\d{2}-\d{2})', line)
                if expiry_match:
                    current['expiry'] = expiry_match.group(1)

            # New format: highest-volume strike marked with ← BEST
            if current and '← BEST' in line:
                best_match = re.search(r'\$\s*([\d.]+)', line)
                if best_match:
                    current['strike'] = float(best_match.group(1))

            # New format: Ticker on its own line
            if current and 'Ticker:' in line and 'Strike:' not in line and 'Expiry:' not in line:
                ticker_match = re.search(r'Ticker:\s+(\S+)', line)
                if ticker_match:
                    current['option_ticker'] = ticker_match.group(1)

            # Match history line for WR and avg return
            if current and 'History:' in line:
                wr_match = re.search(r'WR:\s+(\d+)%', line)
                avg_match = re.search(r'avg 10D return:\s+([+-]?[\d.]+)%', line)
                if wr_match:
                    current['win_rate'] = float(wr_match.group(1))
                if avg_match:
                    current['avg_return'] = float(avg_match.group(1))

            # Match volume
            if current and 'Opt Vol:' in line:
                vol_match = re.search(r'Opt Vol:\s+([\d,]+)', line)
                if vol_match:
                    current['options_volume'] = int(vol_match.group(1).replace(',', ''))

    # Don't forget the last entry
    if current.get('symbol') and current.get('option_ticker'):
        entry_date_str = _get_next_trading_day(watchlist_date)
        ok = add_position(
            data,
            symbol=current['symbol'],
            direction=current['direction'],
            drop_pct=current['drop_pct'],
            stock_price=current['stock_price'],
            strike=current['strike'],
            expiry=current['expiry'],
            option_ticker=current['option_ticker'],
            tp_pct=current['tp_pct'],
            sl_pct=current['sl_pct'],
            tier=current.get('tier', ''),
            entry_date=entry_date_str,
            options_volume=current.get('options_volume'),
            win_rate=current.get('win_rate'),
            avg_return=current.get('avg_return'),
        )
        if ok:
            added += 1
            print(f"  + {current['symbol']} {current['direction']} ${current['strike']} "
                  f"exp {current['expiry']} → entry {entry_date_str}")

    return added


def add_from_3m_confirmed(data, decisions_date):
    """
    Add positions from Stock_Drop_3m_Confirmed_Entries_{date}.csv
    (parallel ~90 DTE book written by stock_drop_confirmation_entry.py).

    Entry date = confirmation date, fill at ~12:45 PT / day_close.
    """
    import pandas as pd
    path = os.path.join(
        ALERT_DIR, f'Stock_Drop_3m_Confirmed_Entries_{decisions_date}.csv')
    if not os.path.exists(path):
        print(f"  3m confirmed file not found: {path}")
        return 0
    try:
        df = pd.read_csv(path)
    except Exception as e:
        print(f"  Could not read {path}: {e}")
        return 0
    if df.empty:
        print("  3m confirmed file is empty.")
        return 0

    added = 0
    for _, r in df.iterrows():
        ticker = r.get('Option_Ticker')
        if not isinstance(ticker, str) or not ticker.startswith('O:'):
            print(f"  - {r.get('Symbol')}: no 3m option ticker — skipped")
            continue
        tp_pct, sl_pct = _row_take_profit(
            r.get('Drop_Pct'), None, r.get('Tier_Cell'), r.get('TP'))
        ok = add_position(
            data,
            symbol=str(r['Symbol']),
            direction=str(r['Direction']),
            drop_pct=float(r['Drop_Pct']) if pd.notna(r.get('Drop_Pct')) else 0.0,
            stock_price=float(r['Price_Now']) if pd.notna(r.get('Price_Now')) else 0.0,
            strike=float(r['Strike']) if pd.notna(r.get('Strike')) else 0.0,
            expiry=str(r['Expiry']),
            option_ticker=ticker,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            tier=str(r.get('Tier_Cell') or ''),
            entry_date=decisions_date,
            entry_fill='day_close',
            source='v2_3m',
            decision='confirmed',
            decision_reason=str(r.get('Reason') or 'confirmed_3m_parallel'),
            # Hold length is defined by the active book (3m10 vs 3m20), not the CSV
            max_hold_days=MAX_HOLD_DAYS,
            dte_at_entry=int(r['DTE']) if pd.notna(r.get('DTE')) else None,
            monthly_option_ticker=(
                str(r['Monthly_Ticker']) if pd.notna(r.get('Monthly_Ticker')) else None
            ),
            monthly_expiry=(
                str(r['Monthly_Expiry']) if pd.notna(r.get('Monthly_Expiry')) else None
            ),
        )
        if ok:
            added += 1
            print(f"  + 3m {r['Symbol']} {r['Direction']} ${r.get('Strike')} "
                  f"exp {r.get('Expiry')} DTE {r.get('DTE')} "
                  f"→ entry {decisions_date} @ 12:45 PT")
    return added


def add_from_decisions(data, decisions_date, only_confirmed):
    """
    Add positions from a v2 confirmation decisions CSV
    (Stock_Drop_Confirmation_Decisions_{date}.csv).

    only_confirmed=True  → main book: only Decision == 'confirmed' rows.
    only_confirmed=False → shadow book: EVERY candidate, tagged with its v2
                           decision so cohorts (gate_rejected / tier_cut /
                           below_floor / capped) can be compared later.

    Entry date = the decisions date itself, filled at that day's CLOSE
    (entry_fill='day_close') — matching the v2 "enter near the close" rule.
    """
    import pandas as pd
    path = os.path.join(
        ALERT_DIR, f'Stock_Drop_Confirmation_Decisions_{decisions_date}.csv')
    if not os.path.exists(path):
        print(f"  Decisions file not found: {path}")
        return 0

    try:
        df = pd.read_csv(path)
    except Exception as e:
        print(f"  Could not read {path}: {e}")
        return 0
    if df.empty:
        print("  Decisions file is empty.")
        return 0

    added = 0
    for _, r in df.iterrows():
        decision = str(r.get('Decision') or 'no_quote')
        if only_confirmed and decision != 'confirmed':
            continue
        ticker = r.get('Option_Ticker')
        if not isinstance(ticker, str) or not ticker.startswith('O:'):
            print(f"  - {r.get('Symbol')}: no option ticker — skipped")
            continue
        vix_now = r.get('VIX_Now')
        vix_now = None if pd.isna(vix_now) else vix_now
        tp_pct, sl_pct = _row_take_profit(
            r.get('Drop_Pct'), vix_now, r.get('Tier_Cell'), r.get('TP'))

        ok = add_position(
            data,
            symbol=str(r['Symbol']),
            direction=str(r['Direction']),
            drop_pct=float(r['Drop_Pct']),
            stock_price=float(r['Price_Now']) if pd.notna(r.get('Price_Now')) else 0.0,
            strike=float(r['Strike']) if pd.notna(r.get('Strike')) else 0.0,
            expiry=str(r['Expiry']),
            option_ticker=ticker,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            tier=str(r.get('Tier_Cell') or ''),
            entry_date=decisions_date,
            entry_fill='day_close',
            source='v2',
            decision=decision,
            decision_reason=str(r.get('Reason') or ''),
            v2_score=float(r['Score']) if pd.notna(r.get('Score')) else None,
            d1_ret_pct=float(r['D1_Ret_Pct']) if pd.notna(r.get('D1_Ret_Pct')) else None,
        )
        if ok:
            added += 1
            tag = '' if only_confirmed else f' [{decision}]'
            print(f"  + {r['Symbol']} {r['Direction']} ${r.get('Strike')} "
                  f"exp {r.get('Expiry')}{tag} → entry {decisions_date} @ close")
    return added


def _get_next_trading_day(date_str):
    """Get next weekday after a date string."""
    dt = datetime.strptime(date_str, '%Y-%m-%d').date()
    nxt = dt + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.strftime('%Y-%m-%d')


# ── Manual sell ───────────────────────────────────────────────────────────────

def sell_position(data, symbol, sell_price, sell_date=None, entry_price_override=None):
    """
    Manually close a position at a specific price.
    Matches the most recent open position for the symbol.

    Args:
        symbol: Stock ticker (case-insensitive)
        sell_price: Price per contract the option was sold at
        sell_date: Date sold (YYYY-MM-DD), defaults to today
        entry_price_override: Override the entry price (user-provided)
    """
    symbol = symbol.upper()
    sell_time = ''
    if sell_date is None:
        sell_date = date.today().strftime('%Y-%m-%d')
        sell_time = datetime.now().strftime('%H:%M')

    # Find the matching open position (most recent entry first)
    matches = [p for p in data['positions']
               if p['symbol'] == symbol and p['status'] == 'open']

    if not matches:
        print(f"\n  No open position found for {symbol}")
        open_pos = [p for p in data['positions'] if p['status'] == 'open']
        if open_pos:
            print(f"  Open positions: {', '.join(p['symbol'] for p in open_pos)}")
        return False

    # Take the most recent one if multiple
    pos = sorted(matches, key=lambda x: x['entry_date'], reverse=True)[0]

    # Set entry price from override or existing
    if entry_price_override:
        pos['entry_price'] = round(entry_price_override, 2)
        num_c = max(1, math.floor(ENTRY_AMOUNT / (entry_price_override * 100)))
        pos['num_contracts'] = num_c
        pos['entry_amount'] = round(num_c * entry_price_override * 100, 2)

    entry_px = pos.get('entry_price')
    if not entry_px or entry_px <= 0:
        print(f"\n  {symbol} has no entry price. Use --entry-price with --sell, "
              f"or --set-entry SYMBOL PRICE first.")
        return False

    num_contracts = pos.get('num_contracts', 0)
    actual_invested = pos.get('entry_amount', num_contracts * entry_px * 100)
    pnl_pct = round((sell_price - entry_px) / entry_px * 100, 1)
    pnl_dollar = round(actual_invested * pnl_pct / 100, 2)

    pos['status'] = 'manual_sell'
    pos['exit_date'] = sell_date
    pos['exit_time'] = sell_time
    pos['exit_price'] = round(sell_price, 2)
    pos['exit_pnl_pct'] = pnl_pct
    pos['exit_pnl'] = pnl_dollar
    pos['exit_reason'] = f"Sold @ ${sell_price:.2f}"
    pos['current_price'] = round(sell_price, 2)
    pos['current_pnl'] = pnl_dollar
    pos['current_pnl_pct'] = pnl_pct

    data['total_pnl'] += pnl_dollar

    sign = '+' if pnl_pct >= 0 else ''
    print(f"\n  Sold {symbol} {pos['direction']} ${pos['strike']:.2f}")
    print(f"  Entry: ${entry_px:.2f} ({pos['entry_date']}) → Exit: ${sell_price:.2f} ({sell_date})")
    print(f"  Contracts: {num_contracts:.1f} × 100 shares")
    print(f"  P&L: {sign}{pnl_pct:.1f}% → {sign}${pnl_dollar:,.2f}")
    return True


def set_entry_price(data, symbol, entry_price):
    """Manually set the entry price for a position that couldn't auto-fill."""
    symbol = symbol.upper()
    matches = [p for p in data['positions']
               if p['symbol'] == symbol and p['status'] == 'open'
               and not p.get('entry_price')]

    if not matches:
        print(f"\n  No open position without entry price found for {symbol}")
        return False

    pos = matches[0]
    pos['entry_price'] = round(entry_price, 2)
    num_c = max(1, math.floor(ENTRY_AMOUNT / (entry_price * 100)))
    pos['num_contracts'] = num_c
    pos['entry_amount'] = round(num_c * entry_price * 100, 2)
    print(f"\n  Set {symbol} entry price: ${entry_price:.2f} ({num_c} contracts, ${pos['entry_amount']:,.0f} invested)")
    return True


def list_open_positions(data):
    """Show a quick list of open positions for reference."""
    open_pos = [p for p in data['positions'] if p['status'] == 'open']
    if not open_pos:
        print("\n  No open positions.")
        return

    print(f"\n  OPEN POSITIONS ({len(open_pos)}):")
    for i, p in enumerate(sorted(open_pos, key=lambda x: x['entry_date']), 1):
        entry_px = p.get('entry_price')
        px_str = f"${entry_px:.2f}" if entry_px else "pending"
        print(f"    {i}. {p['symbol']} {p['direction']} ${p['strike']:.2f} "
              f"exp {p['expiry']} | entry {p['entry_date']} @ {px_str} "
              f"| TP {_open_tp_cell(p)} | SL {p.get('sl_pct')}% "
              f"| {p.get('trading_days_held', 0)}/{MAX_HOLD_DAYS} days")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Stock Drop P&L Tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s                                    # Update all open positions\n"
            "  %(prog)s --summary                          # Show P&L report\n"
            "  %(prog)s --sell EL 12.50 --entry-price 8.50  # Sell EL: bought $8.50, sold $12.50\n"
            "  %(prog)s --sell EL 12.50 --entry-price 8.50 --sell-date 2026-03-25\n"
            "  %(prog)s --set-entry EL 8.50                # Set entry price without selling\n"
            "  %(prog)s --open                             # List open positions\n"
            "  %(prog)s --add-from-watchlist 2026-03-23    # Add from watchlist\n"
        ),
    )
    parser.add_argument('--summary', action='store_true',
                        help='Show P&L summary only (no price updates)')
    parser.add_argument('--book', choices=['main', 'shadow', '3m10', '3m20'],
                        default='main',
                        help='Which P&L book to operate on. main = confirmed '
                             'v2 monthly (~30-45 DTE, 10d hold); shadow = research; '
                             '3m10 = ~90 DTE / 10d hold; 3m20 = ~90 DTE / 20d hold')
    parser.add_argument('--add-from-watchlist', type=str, metavar='DATE',
                        help='Add positions from a watchlist date (YYYY-MM-DD) '
                             '[legacy v0 intake — superseded by --add-from-confirmed]')
    parser.add_argument('--add-from-confirmed', type=str, metavar='DATE',
                        help='Add CONFIRMED entries from the v2 decisions CSV '
                             'for DATE (main book intake)')
    parser.add_argument('--add-from-decisions', type=str, metavar='DATE',
                        help='Add ALL candidates from the v2 decisions CSV '
                             'for DATE with decision tags (shadow book intake)')
    parser.add_argument('--add-from-3m-confirmed', type=str, metavar='DATE',
                        help='Add 3-month confirmed entries from '
                             'Stock_Drop_3m_Confirmed_Entries_DATE.csv '
                             '(use with --book 3m10 or 3m20)')
    parser.add_argument('--sell', nargs=2, metavar=('SYMBOL', 'PRICE'),
                        help='Sell a position: --sell SYMBOL EXIT_PRICE')
    parser.add_argument('--entry-price', type=float, metavar='PRICE',
                        help='Entry price per contract (use with --sell)')
    parser.add_argument('--sell-date', type=str, default=None, metavar='DATE',
                        help='Date of sale (YYYY-MM-DD). Default: today.')
    parser.add_argument('--set-entry', nargs=2, metavar=('SYMBOL', 'PRICE'),
                        help='Manually set entry price: --set-entry SYMBOL PRICE')
    parser.add_argument('--open', action='store_true', dest='show_open',
                        help='List all open positions')
    parser.add_argument('--force', '-f', action='store_true',
                        help='Run even on weekends')
    parser.add_argument('--reset', action='store_true',
                        help='Clear all tracked positions')
    args = parser.parse_args()

    # Switch data files / hold rules per book before anything loads
    global PNL_JSON, PNL_TXT, MAX_HOLD_DAYS, MIN_CONTRACT_VOLUME
    if args.book == 'shadow':
        PNL_JSON = os.path.join(ALERT_DIR, 'stock_drop_shadow_pnl.json')
        PNL_TXT = os.path.join(ALERT_DIR, 'stock_drop_shadow_pnl.txt')
    elif args.book == '3m10':
        # Live 3-month book, 10 trading-day max hold (same contracts as 3m20)
        PNL_JSON = os.path.join(ALERT_DIR, 'stock_drop_3m10_live_pnl.json')
        PNL_TXT = os.path.join(ALERT_DIR, 'stock_drop_3m10_live_pnl.txt')
        MAX_HOLD_DAYS = 10
        MIN_CONTRACT_VOLUME = 10  # thinner far-dated chains
    elif args.book == '3m20':
        # Live 3-month book, 20 trading-day max hold (higher-ROI setup)
        PNL_JSON = os.path.join(ALERT_DIR, 'stock_drop_3m20_live_pnl.json')
        PNL_TXT = os.path.join(ALERT_DIR, 'stock_drop_3m20_live_pnl.txt')
        MAX_HOLD_DAYS = 20
        MIN_CONTRACT_VOLUME = 10  # thinner far-dated chains

    # Allow --sell, --summary, --open on weekends without --force
    needs_market = not (args.summary or args.sell or args.show_open
                        or args.set_entry or args.reset)
    if not args.force and needs_market and datetime.now().weekday() >= 5:
        print("Weekend — use --force to run or --summary to view.")
        return

    if args.book == 'shadow':
        book_label = 'SHADOW (all candidates)'
    elif args.book == '3m10':
        book_label = '3M10 LIVE (~90 DTE, 10d hold)'
    elif args.book == '3m20':
        book_label = '3M20 LIVE (~90 DTE, 20d hold)'
    else:
        book_label = 'MAIN (monthly ~30-45 DTE, 10d hold)'
    print("=" * 60)
    print(f"STOCK DROP P&L TRACKER — {book_label} BOOK")
    print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Entry Size: ${ENTRY_AMOUNT:,} per position | Max Hold: {MAX_HOLD_DAYS}d")
    print("=" * 60)

    if args.reset:
        if os.path.exists(PNL_JSON):
            os.remove(PNL_JSON)
            print("\nTracker data cleared.")

    data = load_pnl_data()

    # ── --open: quick list ──
    if args.show_open:
        list_open_positions(data)
        return

    # ── --set-entry: manual entry price ──
    if args.set_entry:
        symbol, price_str = args.set_entry
        try:
            price = float(price_str)
        except ValueError:
            print(f"  Invalid price: {price_str}")
            return
        set_entry_price(data, symbol, price)
        save_pnl_data(data)
        return

    # ── --sell: manual close ──
    if args.sell:
        symbol, price_str = args.sell
        try:
            sell_price = float(price_str)
        except ValueError:
            print(f"  Invalid price: {price_str}")
            return
        sold = sell_position(data, symbol, sell_price, args.sell_date, args.entry_price)
        if sold:
            save_pnl_data(data)
        generate_report(data)
        return

    # ── --add-from-watchlist ──
    if args.add_from_watchlist:
        print(f"\nAdding positions from watchlist {args.add_from_watchlist}...")
        added = add_from_watchlist(data, args.add_from_watchlist)
        if added:
            print(f"\n  Added {added} new position(s)")
        else:
            print(f"\n  No new positions to add")
        save_pnl_data(data)

    # ── --add-from-confirmed / --add-from-decisions (v2 pipeline) ──
    if args.add_from_confirmed:
        print(f"\nAdding CONFIRMED v2 entries from decisions {args.add_from_confirmed}...")
        added = add_from_decisions(data, args.add_from_confirmed, only_confirmed=True)
        print(f"\n  Added {added} new position(s)")
        save_pnl_data(data)

    if args.add_from_decisions:
        print(f"\nAdding ALL candidates from decisions {args.add_from_decisions} (shadow)...")
        added = add_from_decisions(data, args.add_from_decisions, only_confirmed=False)
        print(f"\n  Added {added} new position(s)")
        save_pnl_data(data)

    if args.add_from_3m_confirmed:
        print(f"\nAdding 3-month confirmed entries from {args.add_from_3m_confirmed}...")
        if args.book not in ('3m10', '3m20'):
            print("  (tip: use --book 3m10 or --book 3m20 for the live 3m P&L files)")
        added = add_from_3m_confirmed(data, args.add_from_3m_confirmed)
        print(f"\n  Added {added} new 3m position(s) "
              f"(max hold {MAX_HOLD_DAYS}d on this book)")
        save_pnl_data(data)

    # ── Default: update positions ──
    if not args.summary:
        print("\nUpdating open positions...")
        updated, closed = update_positions(data)
        print(f"\n  Updated: {updated} | Closed: {closed}")
        save_pnl_data(data)

    generate_report(data)


if __name__ == '__main__':
    main()
