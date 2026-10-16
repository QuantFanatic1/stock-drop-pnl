#!/usr/bin/env python3
"""
Stock Drop Watchlist Generator
-------------------------------
Runs after the daily stock_drop_alert.py to produce a next-morning
watchlist of qualified plays based on tiered VIX / TP / SL rules.

Reads today's (or specified date's) alert file, checks VIX, applies
drop-size filters, determines call/put direction from 5-year history
(equal-weight closed 10-day windows over 5 years),
and outputs a ranked watchlist with entry rules.

Usage:
    python stock_drop_watchlist.py                  # Use today's alert
    python stock_drop_watchlist.py --date 2026-03-18  # Specific date
    python stock_drop_watchlist.py --vix-override 18.5  # Override VIX

Output: Stock_Drop_Alert/Stock_Drop_Watchlist_{date}.txt
"""

import csv
import glob
import json
import os
import re
import sys
import time
import argparse
import subprocess
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from tabulate import tabulate

from stock_drop_errors import public_error, require_env
from stock_drop_exit_rules import (
    DROP_BUCKETS, STANDARD_SL_PCT, numbered_watchlist_blocked,
    standard_take_profit,
)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_SCRIPT_DIR, '.env'))

FMP_API_KEY = os.getenv('FMP_API_KEY')
FMP_BASE_V3 = 'https://financialmodelingprep.com/api/v3'
POLYGON_API_KEY = os.getenv('POLYGON_API_KEY')
POLYGON_BASE = 'https://api.polygon.io'

# Polygon rate limiting
_last_polygon_ts = 0.0
POLYGON_MIN_INTERVAL = 0.35

from datetime import date as _date_type

ALERT_DIR = os.path.join(_SCRIPT_DIR, 'Stock_Drop_Alert')
VIX_HISTORY = os.path.join(_SCRIPT_DIR, 'vix_history.csv')

DROP_THRESHOLD = -5.0
MIN_HIST_DROPS = 5          # closed 10d windows inside the 5-year lookback
FORWARD_DAYS = 10

# ── Quality Filters ───────────────────────────────────────────────────────────
MIN_MARKET_CAP = 100_000_000   # $100M minimum
MIN_STOCK_PRICE = 50           # $50 minimum (0% WR under $50 in backtest)
MIN_DTE_CALENDAR = 30          # 30+ calendar days to expiry — theta decay protection
MIN_HIST_WIN_RATE = 50         # Historical 10D win rate >= 50%
MIN_OPTIONS_VOLUME = 5_000     # Total options volume on drop day >= 5,000 contracts
MIN_CONTRACT_VOLUME = 50       # Best-strike contract must trade >= 50 contracts on drop day
REPEAT_DROP_WARN = 3           # Flag 3rd+ drops as lower confidence


def _is_third_friday(d):
    """Check if a date is the 3rd Friday of its month, or the Thursday before
    when that Friday is a market holiday (e.g. Juneteenth, Good Friday)."""
    from market_holidays import is_market_holiday
    # Standard case: 3rd Friday (day 15-21, weekday=Friday)
    if d.weekday() == 4 and 15 <= d.day <= 21:
        return True
    # Holiday case: Thursday before 3rd Friday when Friday is a market holiday
    if d.weekday() == 3 and 14 <= d.day <= 20:
        friday = d + timedelta(days=1)
        if is_market_holiday(friday.strftime('%Y-%m-%d')):
            return True
    return False

# Take-profit: stock_drop_exit_rules.standard_take_profit (replay table).
# Stop: STANDARD_SL_PCT (-40). The numbered list still uses the old VIX caps.

# Filters
EXCLUDED_SUFFIXES = ('-WT', '-WS', '-UN', '-RT', '-R', '-V', '.WS', '.WT')
KNOWN_LEVERAGED = frozenset({
    'TQQQ', 'SQQQ', 'SPXL', 'SPXS', 'UPRO', 'SPXU', 'TNA', 'TZA',
    'UDOW', 'SDOW', 'LABU', 'LABD', 'FAS', 'FAZ', 'NUGT', 'DUST',
    'JNUG', 'JDST', 'ERX', 'ERY', 'TECL', 'TECS', 'FNGU', 'FNGD',
    'SOXL', 'SOXS', 'UVXY', 'SVXY', 'BOIL', 'KOLD', 'UCO', 'SCO',
    'YANG', 'YINN', 'GUSH', 'DRIP', 'NAIL', 'DRV', 'CURE', 'PILL',
    'BULZ', 'BERZ', 'WEBL', 'WEBS', 'RETL', 'TPOR', 'DPST', 'BNKU',
})
LEVERAGED_NAME_RE = re.compile(
    r'(?i)\b(2x|3x|-2x|-3x|ultra|leveraged|proshares|direxion|microsectors)\b'
)

# ── Alert file parsing ────────────────────────────────────────────────────────

DROP_LINE_RE = re.compile(
    r'✓\s+([A-Z][A-Z0-9]{0,5}):\s+'
    r'(-?\d+\.?\d*)%\s+drop\s+@\s+\$([\d,.]+),\s+'
    r'Market\s+Cap:\s+\$([\d.]+)([TBM])'
)


def find_alert_file(target_date):
    """Find the alert file for a given date."""
    if not os.path.isdir(ALERT_DIR):
        return None
    # Check multiple naming patterns
    patterns = [
        f'Stock_Drop_Alert_{target_date}.txt',
        f'Stock_Drop_Alert_{target_date}_*.txt',
    ]
    import glob
    for pat in patterns:
        matches = glob.glob(os.path.join(ALERT_DIR, pat))
        if matches:
            return sorted(matches)[-1]  # latest if multiple
    # Also check parent dir
    for pat in patterns:
        matches = glob.glob(os.path.join(_SCRIPT_DIR, pat))
        if matches:
            return sorted(matches)[-1]
    return None


def parse_alert_file(filepath):
    """Parse drops from a single alert file."""
    stocks = []
    seen = set()
    with open(filepath, 'r') as f:
        for line in f:
            m = DROP_LINE_RE.search(line)
            if not m:
                continue
            symbol = m.group(1)
            if symbol in seen:
                continue
            seen.add(symbol)
            drop_pct = float(m.group(2))
            price = float(m.group(3).replace(',', ''))
            cap_val = float(m.group(4))
            cap_suffix = m.group(5)
            if cap_suffix == 'T':
                market_cap = cap_val * 1e12
            elif cap_suffix == 'B':
                market_cap = cap_val * 1e9
            else:
                market_cap = cap_val * 1e6
            stocks.append({
                'symbol': symbol,
                'drop_pct': drop_pct,
                'price': price,
                'market_cap': market_cap,
            })
    return stocks


# ── VIX ───────────────────────────────────────────────────────────────────────

def fetch_current_vix():
    """Fetch current VIX from FMP."""
    url = f'{FMP_BASE_V3}/quote/%5EVIX?apikey={FMP_API_KEY}'
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0].get('price', 0)
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"Live VIX quote failed: {public_error(exc)}")
    return None


def get_vix_from_history(target_date):
    """Get VIX for a specific date from vix_history.csv."""
    if not os.path.exists(VIX_HISTORY):
        print(f"vix_history.csv is missing at {VIX_HISTORY}. "
              "The repo includes this file. Without it, historical VIX lookups fail.")
        return None
    try:
        import pandas as pd
        vdf = pd.read_csv(VIX_HISTORY)
        day_rows = vdf[vdf['Date'] == target_date]
        if day_rows.empty:
            print(f"No VIX row for {target_date} in {VIX_HISTORY}.")
            return None
        # Prefer midday reading
        midday = day_rows[day_rows['Time_PST'] == '12:00']
        if not midday.empty:
            return float(midday.iloc[0]['VIX'])
        return float(day_rows.iloc[0]['VIX'])
    except (OSError, ValueError, KeyError) as exc:
        print(f"Could not read VIX for {target_date} from {VIX_HISTORY}: {public_error(exc)}")
        return None


# ── Direction analysis (point-in-time via stock_drop_pit) ────────────────────

V2_RULES_FILE = os.path.join(ALERT_DIR, 'stock_drop_v2_rules.json')


def load_v2_rules():
    """Load the validated v2 rule set. Prints the reason when the file cannot be used."""
    if not os.path.exists(V2_RULES_FILE):
        print(f"stock_drop_v2_rules.json is missing at {V2_RULES_FILE}. "
              "The watchlist cannot mark v2-only names without it.")
        return {}
    try:
        with open(V2_RULES_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read {V2_RULES_FILE}: {public_error(exc)}")
        return {}


_pit_analyzer = None


def get_pit_analyzer(target_date):
    """Shared PITAnalyzer refreshed through the alert date."""
    global _pit_analyzer
    if _pit_analyzer is None:
        from stock_drop_pit import PITAnalyzer
        _pit_analyzer = PITAnalyzer(refresh_through=target_date)
    return _pit_analyzer


def analyze_direction(symbol, target_date=None, earnings_weight=1.0,
                      recency_half_life_months=None, lookback_months=None):
    """
    Determine call vs put from the point-in-time bounce stats: 5%+ drops in
    the trailing lookback (live = 5 years) whose 10-day forward window has
    closed, earnings drops down-weighted by earnings_weight. Recency decay
    is off in the live standard (equal weight).
    Returns dict with direction, avg_return, win_rate, num_drops, conviction,
    plus cascade context, or None.
    """
    target_date = target_date or datetime.now().strftime('%Y-%m-%d')
    pit = get_pit_analyzer(target_date)
    kw = {'earnings_weight': earnings_weight}
    if recency_half_life_months is not None:
        kw['recency_half_life_months'] = recency_half_life_months
    if lookback_months is not None:
        kw['lookback_months'] = lookback_months
    stats = pit.bounce_stats(symbol, target_date, **kw)
    if not stats or stats['n_drops'] < MIN_HIST_DROPS:
        return None
    out = {
        'direction': stats['direction'],
        'avg_return': stats['wavg_return'],
        'win_rate': stats['win_rate'],
        'num_drops': stats['n_drops'],
        'conviction': stats['conviction'],
    }
    out['is_earnings_drop'] = int(pit.is_earnings_drop(symbol, target_date))
    out.update(pit.cascade_features(symbol, target_date))
    # Drop-day volume (the target_date bar, NOT the last bar — they differ
    # when regenerating a past date) for Day-1 volume-ratio at confirmation
    out['drop_day_volume'] = None
    daily = pit.get_daily(symbol)
    if len(daily):
        from stock_drop_pit import _to_date
        row = daily[daily['date'] == _to_date(target_date)]
        if len(row):
            out['drop_day_volume'] = float(row['volume'].iloc[0])
    return out


# ── Polygon helpers ──────────────────────────────────────────────────────────

def _polygon_get(url, params):
    """GET with pacing for Polygon rate limits."""
    global _last_polygon_ts
    elapsed = time.time() - _last_polygon_ts
    if elapsed < POLYGON_MIN_INTERVAL:
        time.sleep(POLYGON_MIN_INTERVAL - elapsed)
    try:
        resp = requests.get(url, params=params, timeout=30)
    except requests.RequestException:
        return None
    _last_polygon_ts = time.time()
    if resp.status_code == 429:
        time.sleep(2)
        try:
            resp = requests.get(url, params=params, timeout=30)
        except requests.RequestException:
            return None
        _last_polygon_ts = time.time()
    return resp


def _get_option_day_volume(option_ticker, date_str):
    """Get the trading volume for a specific option contract on a specific day
    via Polygon aggregates endpoint. Returns volume (int) or 0."""
    url = f'{POLYGON_BASE}/v2/aggs/ticker/{option_ticker}/range/1/day/{date_str}/{date_str}'
    params = {'apiKey': POLYGON_API_KEY}
    resp = _polygon_get(url, params)
    if not resp or resp.status_code != 200:
        return 0
    try:
        results = resp.json().get('results', [])
        if results:
            return int(results[0].get('v', 0))
    except (ValueError, KeyError, IndexError) as exc:
        print(f"Could not read volume for {option_ticker} on {date_str}: {public_error(exc)}")
    return 0


def lookup_option_contract(symbol, price, direction, min_dte=None):
    """
    Look up real option contracts from Polygon and return the best match by
    volume (follow the money) WITHIN ±25% of the stock price.

    Finds the nearest monthly expiry at least min_dte (30) CALENDAR days out,
    then ranks strikes inside the ±25% band by drop-day volume. The band is
    the guardrail: liquidity picks the strike, but it can never drift to the
    far-OTM lottery strikes that barely move (the unbounded version chased
    50%+ OTM picks).

    Returns dict with:
      'strike', 'expiry', 'ticker', 'dte', 'volume' — highest-volume in band
      'top_3' — top 3 in-band strikes by volume [{'strike','volume','ticker'}]
    Or None if no contracts found.
    """
    if not POLYGON_API_KEY:
        return None

    if min_dte is None:
        min_dte = MIN_DTE_CALENDAR

    opt_type = direction.lower()
    today = datetime.now().date()
    min_expiry = today + timedelta(days=min_dte)
    max_expiry = today + timedelta(days=120)

    # Step 1: Get available contracts from reference endpoint (no strike bounds — volume decides)
    url = f'{POLYGON_BASE}/v3/reference/options/contracts'
    params = {
        'underlying_ticker': symbol,
        'contract_type': opt_type,
        'expiration_date.gte': min_expiry.strftime('%Y-%m-%d'),
        'expiration_date.lte': max_expiry.strftime('%Y-%m-%d'),
        'expired': 'false',
        'limit': 1000,
        'apiKey': POLYGON_API_KEY,
    }

    resp = _polygon_get(url, params)
    if not resp or resp.status_code != 200:
        return None

    try:
        results = resp.json().get('results', [])
    except ValueError:
        return None

    if not results:
        return None

    # Group by expiry, filter to monthly (3rd Friday) with 30+ calendar days DTE
    by_expiry = {}
    for c in results:
        exp = c.get('expiration_date', '')
        strike = float(c.get('strike_price', 0))
        if exp and strike > 0:
            try:
                exp_date = datetime.strptime(exp, '%Y-%m-%d').date()
            except ValueError:
                continue
            if _is_third_friday(exp_date):
                by_expiry.setdefault(exp, []).append(c)

    if not by_expiry:
        return None

    nearest_expiry = min(by_expiry.keys())
    contracts = by_expiry[nearest_expiry]

    exp_date = datetime.strptime(nearest_expiry, '%Y-%m-%d').date()
    dte = (exp_date - today).days
    today_str = today.strftime('%Y-%m-%d')

    # Step 2: Rank in-band strikes by drop-day volume. The ±25% band keeps
    # the pick from drifting too far OTM or ITM; inside it, liquidity decides
    # (slightly ITM is fine — ITM still moves hard when the stock runs).
    max_dist = 0.25 * price
    contract_volumes = []
    for c in contracts:
        ticker = c.get('ticker', '')
        strike = float(c.get('strike_price', 0))
        if abs(strike - price) > max_dist:
            continue
        vol = _get_option_day_volume(ticker, today_str)
        contract_volumes.append({
            'strike': strike,
            'volume': vol,
            'ticker': ticker,
        })

    if not contract_volumes:
        return None

    # Sort by volume descending, proximity to price as tiebreaker
    contract_volumes.sort(
        key=lambda x: (-x['volume'], abs(x['strike'] - price))
    )

    best = contract_volumes[0]
    return {
        'strike': best['strike'],
        'expiry': nearest_expiry,
        'ticker': best['ticker'],
        'dte': dte,
        'volume': best['volume'],
        'top_3': contract_volumes[:3],
    }


def get_options_volume(symbol):
    """
    Get total options volume for a symbol on the current day via Polygon
    options snapshot. Sums day.volume across all contracts.
    Returns total volume (int) or None if unavailable.
    """
    if not POLYGON_API_KEY:
        return None

    url = f'{POLYGON_BASE}/v3/snapshot/options/{symbol}'
    all_volume = 0
    params = {
        'limit': 250,
        'apiKey': POLYGON_API_KEY,
    }

    # Paginate through results
    while True:
        resp = _polygon_get(url, params)
        if not resp or resp.status_code != 200:
            return all_volume if all_volume > 0 else None

        try:
            data = resp.json()
            results = data.get('results', [])
        except ValueError:
            break

        for contract in results:
            day_data = contract.get('day', {})
            vol = day_data.get('volume', 0) or 0
            all_volume += vol

        # Check for next page
        next_url = data.get('next_url')
        if next_url and len(results) == 250:
            url = next_url
            params = {'apiKey': POLYGON_API_KEY}
        else:
            break

    return all_volume


# ── Filters ───────────────────────────────────────────────────────────────────

def is_excluded(symbol):
    """Check if symbol should be excluded."""
    if any(symbol.upper().endswith(s) for s in EXCLUDED_SUFFIXES):
        return True
    if symbol in KNOWN_LEVERAGED:
        return True
    return False


def get_tier_rules(drop_pct, vix):
    """
    Numbered-watchlist gate, then the 10-day exit.

    Returns (tp, sl, label). tp is None when the 10-day book holds the
    window. Returns None when the older VIX cap fails, so that name stays
    off the numbered list and v2 decides at confirmation. A 5-6% drop at
    VIX 20-29 is in that group. If v2 confirms it, confirmation stamps
    +150%. Returns None when drop or VIX cannot be read.
    """
    result = standard_take_profit(drop_pct, vix, hold_days=10)
    if result is None:
        return None
    tp, sl, label = result
    if label.startswith('no tier') or numbered_watchlist_blocked(drop_pct, vix):
        return None
    return tp, sl, label


# ── Main ──────────────────────────────────────────────────────────────────────

class TeeOutput:
    """Write to both console and file."""
    def __init__(self, filename):
        self.terminal = sys.stdout
        self.file = open(filename, 'w')

    def write(self, message):
        self.terminal.write(message)
        self.file.write(message)

    def flush(self):
        self.terminal.flush()
        self.file.flush()

    def close(self):
        self.file.close()


def main():
    parser = argparse.ArgumentParser(
        description="Generate next-morning watchlist from stock drop alerts")
    parser.add_argument('--date', type=str, default=None,
                        help='Alert date to process (YYYY-MM-DD). Default: today.')
    parser.add_argument('--vix-override', type=float, default=None,
                        help='Override VIX value instead of fetching live.')
    args = parser.parse_args()

    target_date = args.date or datetime.now().strftime('%Y-%m-%d')

    # Find alert file
    alert_file = find_alert_file(target_date)
    if not alert_file:
        print(f"No alert file found for {target_date}. "
              "Run stock_drop_alert.py first. It writes Stock_Drop_Alert/Stock_Drop_Alert_{target_date}.txt.")
        sys.exit(1)
    require_env('FMP_API_KEY', 'POLYGON_API_KEY')

    # Set up output
    os.makedirs(ALERT_DIR, exist_ok=True)
    output_file = os.path.join(ALERT_DIR, f'Stock_Drop_Watchlist_{target_date}.txt')
    tee = TeeOutput(output_file)
    sys.stdout = tee

    try:
        _run(target_date, alert_file, args.vix_override)
    finally:
        sys.stdout = tee.terminal
        tee.close()
        print(f"\nSaved to: {output_file}")


def _drop_bucket_label(drop_pct):
    a = abs(drop_pct)
    for lo, hi, label in DROP_BUCKETS:
        if lo <= a < hi:
            return label
    return f'{a:.0f}%'


def _apply_basic_filters(drops):
    """Drop warrants, names under the market-cap floor, and names under the price floor."""
    filtered_basic = []
    kept = []
    for d in drops:
        if is_excluded(d['symbol']):
            continue
        if d['market_cap'] < MIN_MARKET_CAP:
            filtered_basic.append((d['symbol'], f"Market cap ${d['market_cap']/1e6:.0f}M < ${MIN_MARKET_CAP/1e6:.0f}M"))
            continue
        if d['price'] < MIN_STOCK_PRICE:
            filtered_basic.append((d['symbol'], f"Price ${d['price']:.2f} < ${MIN_STOCK_PRICE}"))
            continue
        kept.append(d)

    if filtered_basic:
        print(f"\n  Filtered (basic): {len(filtered_basic)} stocks")
        for sym, reason in filtered_basic[:5]:
            print(f"    ✗ {sym} — {reason}")
        if len(filtered_basic) > 5:
            print(f"    ... and {len(filtered_basic) - 5} more")

    print(f"\nParsed {len(kept)} stocks with 5%+ drops (after basic filters)")
    return kept


def _resolve_vix(target_date, vix_override):
    """History file first, then the live quote. Returns (vix, source) or (None, None)."""
    if vix_override is not None:
        return vix_override, 'override'
    vix = get_vix_from_history(target_date)
    if vix:
        return vix, 'history'
    vix = fetch_current_vix()
    if vix:
        return vix, 'live'
    return None, None


def _vix_regime(vix):
    if vix < 15:
        return 'LOW'
    if vix < 20:
        return 'MODERATE'
    if vix < 25:
        return 'ELEVATED'
    if vix < 30:
        return 'HIGH'
    return 'EXTREME'


def _qualify_names(drops, vix, v2_rules):
    """Attach the exit tier. Names the old caps would cut stay on the list as v2-only."""
    qualified = []
    filtered_out = []
    for stock in drops:
        drop_abs = abs(stock['drop_pct'])
        result = get_tier_rules(stock['drop_pct'], vix)
        if result is None:
            if v2_rules:
                preview = standard_take_profit(stock['drop_pct'], vix, hold_days=10)
                stock['tp'] = preview[0] if preview else None
                stock['sl'] = STANDARD_SL_PCT
                stock['tier'] = f'{_drop_bucket_label(stock["drop_pct"])} [v2-only]'
                stock['tp_source'] = 'v2-only — exit if confirmed'
                stock['v0_pass'] = False
            else:
                filtered_out.append((stock['symbol'], stock['drop_pct'],
                                     f"VIX {vix:.0f} too high for {drop_abs:.0f}% drop"))
                continue
        else:
            tp, sl, tier = result
            stock['tp'] = tp
            stock['sl'] = sl
            stock['tier'] = tier
            stock['tp_source'] = 'replay table' if tp else 'hold the window'
            stock['v0_pass'] = True
        qualified.append(stock)

    if filtered_out:
        print(f"\nFiltered out ({len(filtered_out)} stocks):")
        for sym, dp, reason in filtered_out[:10]:
            print(f"  ✗ {sym} ({dp:.1f}% drop) — {reason}")
        if len(filtered_out) > 10:
            print(f"  ... and {len(filtered_out) - 10} more")
    return qualified


def _recent_drop_counts():
    """How often each symbol showed up in the last 30 alert files."""
    recent_alerts = sorted(glob.glob(os.path.join(ALERT_DIR, 'Stock_Drop_Alert_*.txt')))[-30:]
    counts = {}
    for alert_path in recent_alerts:
        for stock in parse_alert_file(alert_path):
            counts[stock['symbol']] = counts.get(stock['symbol'], 0) + 1
    return counts


def _direction_settings(v2_rules):
    from stock_drop_pit import RECENCY_HALF_LIFE_MONTHS, LOOKBACK_MONTHS
    earn_w = float(v2_rules.get('earnings_weight', 1.0))
    if 'recency_half_life_months' in v2_rules:
        rec_hl = float(v2_rules.get('recency_half_life_months'))
    else:
        rec_hl = float(v2_rules.get('recency_half_life_years',
                                    RECENCY_HALF_LIFE_MONTHS / 12)) * 12.0
    lookback_m = float(v2_rules.get('lookback_months', LOOKBACK_MONTHS))
    if v2_rules:
        print(f"  [v2] rules loaded ({v2_rules.get('derived', '?')}), "
              f"earnings weight {earn_w}, lookback {lookback_m:g}mo, "
              f"recency half-life {rec_hl:g}mo")
    return earn_w, rec_hl, lookback_m


def _screen_one_name(stock, target_date, earn_w, rec_hl, lookback_m, drop_history_count, skipped):
    """Direction, option volume, and a fillable contract. Returns the stock or None."""
    symbol = stock['symbol']
    print(f"  {symbol}...", end='', flush=True)

    info = analyze_direction(symbol, target_date=target_date,
                             earnings_weight=earn_w,
                             recency_half_life_months=rec_hl,
                             lookback_months=lookback_m)
    if not info:
        print(f" ✗ insufficient drop history "
              f"(<{MIN_HIST_DROPS} closed drops in {lookback_m:g}mo)")
        skipped['no_history'].append(symbol)
        return None

    stock['direction'] = info['direction']
    stock['avg_return'] = info['avg_return']
    stock['win_rate'] = info['win_rate']
    stock['hist_drops'] = info['num_drops']
    stock['conviction'] = info.get('conviction')
    stock['is_earnings_drop'] = info.get('is_earnings_drop', 0)
    stock['consec_down_days'] = info.get('consec_down_days')
    stock['pct_below_20d_high'] = info.get('pct_below_20d_high')
    stock['ret_5d_pct'] = info.get('ret_5d_pct')
    stock['drop_day_volume'] = info.get('drop_day_volume')

    wr = info['win_rate']
    wr_tag = ' [low WR]' if wr < MIN_HIST_WIN_RATE else ''
    print(f" → {info['direction']} (avg {info['avg_return']:+.1f}%, "
          f"WR: {wr:.0f}%, {info['num_drops']} drops){wr_tag}", end='')

    opt_vol = get_options_volume(symbol)
    if opt_vol is not None and opt_vol < MIN_OPTIONS_VOLUME:
        print(f" ✗ options vol {opt_vol:,} < {MIN_OPTIONS_VOLUME:,}")
        skipped['low_volume'].append(f"{symbol} (vol:{opt_vol:,})")
        return None
    stock['options_volume'] = opt_vol

    contract = lookup_option_contract(symbol, stock['price'], stock['direction'])
    if not contract:
        print(f" ✗ no options (or none with {MIN_DTE_CALENDAR}+ calendar days DTE)")
        skipped['no_options'].append(symbol)
        return None

    best_vol = contract.get('volume', 0)
    if best_vol < MIN_CONTRACT_VOLUME:
        print(f" ✗ chosen contract vol {best_vol} < {MIN_CONTRACT_VOLUME}")
        skipped['low_volume'].append(f"{symbol} (chosen contract vol:{best_vol})")
        return None

    stock['real_strike'] = contract['strike']
    stock['real_expiry'] = contract['expiry']
    stock['option_ticker'] = contract['ticker']
    stock['dte'] = contract['dte']
    stock['top_3_strikes'] = contract.get('top_3', [])

    recent_count = drop_history_count.get(symbol, 1)
    stock['recent_drop_count'] = recent_count
    stock['is_repeat_dropper'] = recent_count >= REPEAT_DROP_WARN
    count_str = f" [{recent_count}x in 30d]" if recent_count >= REPEAT_DROP_WARN else ""
    print(f" | ${contract['strike']:.2f} exp {contract['expiry']} "
          f"({contract['dte']}d){count_str}")
    stock['sl_pre_wr'] = stock['sl']
    return stock


def _screen_names(qualified, target_date, v2_rules):
    skipped = {'no_history': [], 'low_wr': [], 'no_options': [], 'low_volume': []}
    drop_history_count = _recent_drop_counts()
    earn_w, rec_hl, lookback_m = _direction_settings(v2_rules)
    watchlist = []
    for stock in qualified:
        kept = _screen_one_name(
            stock, target_date, earn_w, rec_hl, lookback_m, drop_history_count, skipped)
        if kept:
            watchlist.append(kept)
    return watchlist, skipped


def _report_skipped(skipped):
    if skipped['no_history']:
        print(f"\n  Skipped (no drop history): {', '.join(skipped['no_history'])}")
    if skipped['low_volume']:
        print(f"  Skipped (low volume — underlying <{MIN_OPTIONS_VOLUME:,} or best contract <{MIN_CONTRACT_VOLUME}): {', '.join(skipped['low_volume'])}")
    if skipped['no_options']:
        print(f"  Skipped (no options/{MIN_DTE_CALENDAR}+ calendar days DTE): {', '.join(skipped['no_options'])}")


def _write_candidates_csv(watchlist, target_date, vix):
    candidates_file = os.path.join(ALERT_DIR, f'Stock_Drop_Candidates_{target_date}.csv')
    try:
        with open(candidates_file, 'w', newline='') as cf:
            writer = csv.writer(cf)
            writer.writerow(['Symbol', 'Direction', 'Drop_Pct', 'Drop_Close',
                             'Market_Cap', 'Tier', 'TP', 'SL', 'TP_Conservative',
                             'TP_Aggressive', 'TP_Source', 'Strike', 'Expiry',
                             'Option_Ticker', 'DTE', 'Options_Volume',
                             'Top3_Strikes',
                             'PIT_WAvg', 'PIT_WR', 'PIT_Conviction', 'PIT_N_Drops',
                             'Is_Earnings_Drop', 'Consec_Down_Days',
                             'Pct_Below_20d_High', 'Ret_5d_Pct', 'Drop_Day_Volume',
                             'VIX_At_Drop'])
            for stock in watchlist:
                top3_str = ';'.join(
                    f"{strike['strike']:g}:{int(strike['volume'] or 0)}"
                    for strike in (stock.get('top_3_strikes') or []))
                writer.writerow([
                    stock['symbol'], stock['direction'], stock['drop_pct'], stock['price'],
                    stock['market_cap'], stock['tier'], stock['tp'],
                    stock.get('sl_pre_wr', stock['sl']),
                    stock.get('tp_conservative'), stock.get('tp_aggressive'),
                    stock.get('tp_source'), stock.get('real_strike'),
                    stock.get('real_expiry'), stock.get('option_ticker'),
                    stock.get('dte'), stock.get('options_volume'),
                    top3_str,
                    round(stock.get('avg_return', 0), 3),
                    round(stock.get('win_rate', 0), 1),
                    round(stock.get('conviction') or 0, 3), stock.get('hist_drops'),
                    stock.get('is_earnings_drop', 0), stock.get('consec_down_days'),
                    stock.get('pct_below_20d_high'), stock.get('ret_5d_pct'),
                    stock.get('drop_day_volume'), round(vix, 2),
                ])
        print(f"\nCandidates CSV: {candidates_file}")
    except (OSError, csv.Error, ValueError) as exc:
        print(f"\n⚠ Could not write candidates CSV: {public_error(exc)}")


def _market_cap_label(cap):
    if cap >= 1e12:
        return f"${cap / 1e12:.1f}T"
    if cap >= 1e9:
        return f"${cap / 1e9:.1f}B"
    return f"${cap / 1e6:.0f}M"


def _print_candidate_rows(numbered, vix):
    for rank, stock in enumerate(numbered, 1):
        drop = stock['drop_pct']
        tp = stock['tp']
        tp_str = f"+{tp}%" if tp else "Hold to D10"
        real_strike = stock.get('real_strike')
        opt_ticker = stock.get('option_ticker')
        dte = stock.get('dte', 0)
        top_3 = stock.get('top_3_strikes', [])
        opt_vol = stock.get('options_volume')
        vol_str = f"  |  Opt Vol: {opt_vol:,}" if opt_vol else ""

        print(f"  {rank}. {stock['symbol']} {stock['direction']} — {drop:.1f}% drop @ ${stock['price']:.2f} ({_market_cap_label(stock['market_cap'])})")
        print(f"     Tier: {stock['tier']}")
        print(f"     TP: {tp_str}  |  SL: {stock['sl']}%  |  Hold: 10 trading days max  [{stock.get('tp_source', 'tier')}]")
        std20 = standard_take_profit(drop, vix, hold_days=20)
        if std20 and std20[0] != tp:
            tp20 = std20[0]
            tp20_str = f"+{tp20}%" if tp20 else "Hold to D20"
            print(f"     3-month book (20d): TP {tp20_str}  |  SL: {std20[1]}%  |  "
                  f"Hold: 20 trading days")
        print(f"     Expiry: {stock.get('real_expiry')} ({dte} DTE)")
        if top_3:
            print(f"     Strikes by volume (within ±25% of spot):")
            for i, strike in enumerate(top_3, 1):
                marker = " ← BEST" if i == 1 else ""
                vol_display = f"{strike['volume']:,}" if strike['volume'] else "0"
                print(f"       {i}. ${strike['strike']:.2f}  (vol: {vol_display}){marker}")
            if opt_ticker:
                print(f"     Ticker: {opt_ticker}")
        else:
            print(f"     Strike: ${real_strike:.2f}  |  Ticker: {opt_ticker}" if opt_ticker else f"     Strike: ${real_strike:.2f}")
        print(f"     History: {stock.get('hist_drops', 0)} closed drops in 5yr, avg 10D return: {stock.get('avg_return', 0):+.1f}%, WR: {stock.get('win_rate', 0):.0f}%{vol_str}")

        warnings = []
        if abs(drop) >= 15:
            warnings.append("Extreme drop — higher risk of continued decline")
        if vix >= 25:
            warnings.append(f"VIX elevated ({vix:.0f}) — premiums are expensive")
        if stock.get('is_repeat_dropper'):
            warnings.append(f"Repeat dropper ({stock.get('recent_drop_count', 0)}x in 30d) — lower confidence")
        if warnings:
            print(f"     ⚠ {' | '.join(warnings)}")
        print()


def _print_quick_reference(numbered, watchlist, vix):
    print("-" * 90)
    print("QUICK REFERENCE:")
    print("-" * 90)

    table = []
    for rank, stock in enumerate(numbered, 1):
        tp = stock['tp']
        rs = stock.get('real_strike')
        table.append([
            rank,
            stock['symbol'] + (" *" if stock.get('is_repeat_dropper') else ""),
            stock['direction'],
            f"{stock['drop_pct']:.1f}%",
            f"${stock['price']:.2f}",
            f"${rs:.2f}" if rs else "N/A",
            stock.get('real_expiry') or "N/A",
            stock.get('dte', 0),
            f"{stock.get('avg_return', 0):+.1f}%",
            f"{stock.get('win_rate', 0):.0f}%",
            f"+{tp}%" if tp else "D10",
            f"{stock['sl']}%",
        ])

    print(tabulate(table, headers=[
        '#', 'Symbol', 'Dir', 'Drop', 'Price', 'Strike', 'Expiry',
        'DTE', 'Avg 10D', 'WR', 'TP', 'SL'
    ], tablefmt='simple', numalign='right'))
    split = []
    for stock in numbered:
        std20 = standard_take_profit(stock['drop_pct'], vix, hold_days=20)
        if std20 and std20[0] != stock.get('tp'):
            tp20 = f"+{std20[0]}%" if std20[0] else "Hold D20"
            split.append(f"{stock['symbol']} {tp20}")
    if split:
        print("  3-month 20-day target differs from this 10-day sheet: "
              + ", ".join(split))
    if any(stock.get('is_repeat_dropper') for stock in watchlist):
        print("  * = repeat dropper (3+ drops in last 30 days)")


def _print_rules_footer(vix, regime):
    print(f"\n{'=' * 90}")
    print("RULES REMINDER (v2 — confirmation entry):")
    print("  • These are CANDIDATES, not entries. Do NOT enter at the open.")
    print("  • Tomorrow ~12:45 PM PST the confirmation script checks Day-1 action:")
    print("      calls: enter near the close only if the day is GREEN (close > open)")
    print("      puts:  enter near the close (no condition)")
    print("  • Survivors are scored and capped at 15; weak scores are dropped entirely")
    print(f"  • Current VIX regime: {regime} — {'full sizing' if vix < 20 else 'reduced sizing recommended'}")
    print(f"\nFILTERS APPLIED:")
    print(f"  Market cap >= ${MIN_MARKET_CAP/1e6:.0f}M  |  Stock price >= ${MIN_STOCK_PRICE}")
    print(f"  Historical WR >= {MIN_HIST_WIN_RATE}%  |  DTE >= {MIN_DTE_CALENDAR} calendar days")
    print(f"  Options volume >= {MIN_OPTIONS_VOLUME:,} contracts  |  "
          f"Min {MIN_HIST_DROPS} closed drops in 5yr history")
    print("=" * 90)
    print("\nReminders: monthly candidate summary skipped "
          "(decommissioned — use 3 Month Options ENTER list at 12:45 PT)")


def _print_watchlist(watchlist, vix, regime):
    print("\n\n" + "=" * 90)
    print("CANDIDATES — PENDING DAY-1 CONFIRMATION")
    print("Entry is decided TOMORROW ~12:45 PM PST by stock_drop_confirmation_entry.py:")
    print("  calls need a GREEN Day 1 (close > open), puts pass; top picks by score, max 15.")
    print("=" * 90)
    print(f"VIX: {vix:.1f} ({regime})")
    print()

    v2_only = [stock for stock in watchlist if not stock.get('v0_pass', True)]
    numbered = [stock for stock in watchlist if stock.get('v0_pass', True)]
    if v2_only:
        print(f"v2-only candidates (cut by old VIX rules; tier decided at "
              f"confirmation): {', '.join(stock['symbol'] for stock in v2_only)}")
        print()

    _print_candidate_rows(numbered, vix)
    _print_quick_reference(numbered, watchlist, vix)
    _print_rules_footer(vix, regime)


def _run(target_date, alert_file, vix_override):
    """Build the next-session candidate list from one alert file."""
    print("=" * 90)
    print("STOCK DROP WATCHLIST — Next Morning Plays")
    print(f"Alert Date: {target_date}")
    print(f"Generated:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 90)

    drops = parse_alert_file(alert_file)
    if not drops:
        print(f"\nNo qualifying drops found in {os.path.basename(alert_file)}")
        return

    drops = _apply_basic_filters(drops)
    vix, vix_source = _resolve_vix(target_date, vix_override)
    if vix is None:
        print("\n⚠ Could not determine VIX — cannot apply filters.")
        return

    regime = _vix_regime(vix)
    print(f"\nVIX: {vix:.1f} ({regime}) — source: {vix_source}")
    print(f"\nTake-profit: replay table (10-day book). Stop: {STANDARD_SL_PCT}% "
          f"on every name.")

    v2_rules = load_v2_rules()
    qualified = _qualify_names(drops, vix, v2_rules)
    if not qualified:
        print("\nNo stocks qualify under current VIX regime.")
        return

    qualified.sort(key=lambda stock: stock['market_cap'], reverse=True)
    print(f"\n{len(qualified)} stocks qualify — analyzing direction + options...")

    watchlist, skipped = _screen_names(qualified, target_date, v2_rules)
    _report_skipped(skipped)
    if not watchlist:
        print("\nNo stocks with options qualify today.")
        return

    watchlist.sort(key=lambda stock: (stock['avg_return'], stock['win_rate'], abs(stock['drop_pct'])),
                   reverse=True)
    _write_candidates_csv(watchlist, target_date, vix)
    _print_watchlist(watchlist, vix, regime)

REMINDERS_LIST = 'Stocks/Options To Look At'
REMINDERS_STATE = os.path.join(ALERT_DIR, '.reminders_created.json')


def _load_reminder_state():
    if not os.path.exists(REMINDERS_STATE):
        return {}
    try:
        with open(REMINDERS_STATE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read reminder state {REMINDERS_STATE}: {public_error(exc)}")
        return {}


def _save_reminder_state(state):
    try:
        with open(REMINDERS_STATE, 'w') as f:
            json.dump(state, f, indent=2)
    except OSError as exc:
        print(f"Could not save reminder state {REMINDERS_STATE}: {public_error(exc)}")


def create_reminders(watchlist, target_date, vix):
    """DEPRECATED — monthly Reminders list decommissioned.

    Kept for manual/debug use only. Production no longer calls this.
    Actionable entries go to "3 Month Options" from confirmation_entry.
    """
    print("\nReminders: create_reminders() is deprecated "
          "(monthly list decommissioned).")
    if not watchlist:
        return

    # Parse target_date to get the next trading session for the reminder
    from market_holidays import is_market_holiday
    dt = datetime.strptime(target_date, '%Y-%m-%d')
    next_day = dt + timedelta(days=1)
    while next_day.weekday() >= 5 or is_market_holiday(next_day.strftime('%Y-%m-%d')):
        next_day += timedelta(days=1)
    remind_date = next_day.strftime('%Y-%m-%d')

    state = _load_reminder_state()
    state_key = f'{target_date}|__summary__'
    if state_key in state:
        print(f"\nReminders: candidate summary for {target_date} already "
              f"created — skipped (no duplicates)")
        return

    cand_lines = []
    for stock in watchlist:
        strike = stock.get('real_strike')
        strike_str = f"${strike:g}" if strike else '?'
        expiry = stock.get('real_expiry') or '?'
        cand_lines.append(
            f"{stock['symbol']} {stock['direction']} {strike_str} exp {expiry}"
            f" ({stock['drop_pct']:.1f}% @ ${stock['price']:.2f})")

    title = (f"Stock drop candidates ({len(watchlist)}) — "
             f"confirmation 12:45 PM {remind_date}")
    body = (
        f"PENDING Day-1 confirmation — do NOT enter at open.\\n"
        f"Confirmed entries get their own ENTER reminders ~12:45 PM PST.\\n"
        f"VIX at drop: {vix:.1f}\\n\\n"
        + "\\n".join(cand_lines)
    )

    # osascript is invoked without a shell — escape only for the
    # AppleScript double-quoted literal (body keeps intentional \n).
    title_esc = title.replace('\\', '\\\\').replace('"', '\\"')
    body_esc = body.replace('"', '\\"')

    script = (
        'tell application "Reminders"\n'
        f'  tell list "{REMINDERS_LIST}"\n'
        f'    set dueDate to current date\n'
        f'    set day of dueDate to 1\n'
        f'    set year of dueDate to {next_day.year}\n'
        f'    set month of dueDate to {next_day.month}\n'
        f'    set day of dueDate to {next_day.day}\n'
        f'    set hours of dueDate to 12\n'
        f'    set minutes of dueDate to 30\n'
        f'    set seconds of dueDate to 0\n'
        f'    make new reminder with properties '
        f'{{name:"{title_esc}", body:"{body_esc}", '
        f'due date:dueDate, remind me date:dueDate}}\n'
        f'  end tell\n'
        f'end tell\n'
    )

    created = False
    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, text=True, timeout=10
        )
        created = result.returncode == 0
        if not created:
            print(f"Reminders AppleScript failed: {result.stderr.strip() or result.stdout.strip()}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Reminders AppleScript failed: {public_error(exc)}")

    if created:
        state[state_key] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        _save_reminder_state(state)
    print(f"\nReminders: {'created 1 candidate-summary reminder' if created else 'creation FAILED'} "
          f"in '{REMINDERS_LIST}' ({len(watchlist)} candidates listed)")


if __name__ == '__main__':
    main()
