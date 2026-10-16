#!/usr/bin/env python3
"""
Stock Drop Confirmation Entry (v2)
----------------------------------
Runs ~12:45 PM PST (15 min before close) on the session AFTER a stock drop
alert. Reads the previous session's candidate list, applies the validated v2
selection rules, and outputs the final entries for execution near the close.

Per candidate:
  1. Day-1 gate: calls need a GREEN Day 1 (current price > today's open);
     puts pass unconditionally.
  2. Tier check: drop-size x VIX cell must be tradable per stock_drop_v2_rules.json.
  3. Score: composite of OOS-validated features (percentile-transformed
     against train deciles from the rules file).
  4. Floor: score must exceed the validated floor.
  5. Cap: top 15 by score.

Inputs:  Stock_Drop_Alert/Stock_Drop_Candidates_{prev_session}.csv
         Stock_Drop_Alert/stock_drop_v2_rules.json
         Stock_Drop_Alert/Stock_Drop_Strategy_Results_*.csv  (prior trade record)

Outputs: Stock_Drop_Alert/Stock_Drop_Confirmed_Entries_{today}.txt / .csv
         Stock_Drop_Alert/Stock_Drop_3m_Confirmed_Entries_{today}.csv / .txt
         iOS reminders (3-month book only):
           • "3 Month Options" — ~90 DTE, strike re-ranked by volume on the
             3-month chain itself (±25% band), Hold 20 trading days
             (12:45 PT green-gate path; verified higher-ROI setup)
         Monthly (~30-45 DTE) path still writes CSVs and feeds the main P&L
         book, but no longer posts Reminders (decommissioned in favor of 3m).

Usage:
    /usr/bin/python3 stock_drop_confirmation_entry.py
    /usr/bin/python3 stock_drop_confirmation_entry.py --date 2026-06-10   # force "today"
    /usr/bin/python3 stock_drop_confirmation_entry.py --dry-run           # no reminders/files
"""

import os
import re
import sys
import json
import glob
import argparse
import subprocess
from datetime import datetime, timedelta, date as date_type

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
from tabulate import tabulate

from stock_drop_errors import public_error, require_env
from market_holidays import is_market_holiday
from stock_drop_exit_rules import STANDARD_SL_PCT, standard_take_profit
from stock_drop_pit import pit_trade_record

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_SCRIPT_DIR, '.env'))

FMP_API_KEY = os.getenv('FMP_API_KEY')
FMP_BASE_V3 = 'https://financialmodelingprep.com/api/v3'

ALERT_DIR = os.path.join(_SCRIPT_DIR, 'Stock_Drop_Alert')
RULES_FILE = os.path.join(ALERT_DIR, 'stock_drop_v2_rules.json')
REMINDERS_LIST = 'Stocks/Options To Look At'
# Parallel book: ~90 DTE options, same v2 confirmation gate, 20-day max hold
THREE_M_REMINDERS_LIST = '3 Month Options'
THREE_M_TARGET_DTE = 90
THREE_M_BAND = 30
THREE_M_MAX_HOLD_DAYS = 20
# Strike guardrail on the 3m chain — same ±25% band the monthly book uses;
# inside it, volume on the 3m expiry (NOT the monthly) picks the strike.
THREE_M_STRIKE_BAND_PCT = 0.25

_DATE_RE = re.compile(r'_(\d{4}-\d{2}-\d{2})\.csv$')

DROP_BUCKETS = [(5, 6, '5-6%'), (6, 8, '6-8%'), (8, 10, '8-10%'),
                (10, 15, '10-15%'), (15, 999, '15%+')]
VIX_BANDS = [(0, 20, 'VIX<20'), (20, 25, 'VIX20-25'),
             (25, 30, 'VIX25-30'), (30, 999, 'VIX30+')]


class TeeOutput:
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


def is_trading_day(d):
    return d.weekday() < 5 and not is_market_holiday(d.strftime('%Y-%m-%d'))


def prev_trading_day(d):
    p = d - timedelta(days=1)
    while not is_trading_day(p):
        p -= timedelta(days=1)
    return p


QUOTE_RETRIES = 4
QUOTE_RETRY_PAUSE = 20   # seconds — ride out transient DNS/network blips


def fetch_quotes(symbols):
    """Batch FMP quotes with retries. Returns {symbol: quote dict}."""
    out = {}
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i + 50]
        url = f'{FMP_BASE_V3}/quote/{",".join(chunk)}?apikey={FMP_API_KEY}'
        for attempt in range(1, QUOTE_RETRIES + 1):
            try:
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                for q in resp.json():
                    out[q.get('symbol')] = q
                break
            except (requests.RequestException, ValueError, TypeError) as e:
                print(f'  ⚠ quote fetch attempt {attempt}/{QUOTE_RETRIES} failed '
                      f'({chunk[0]}..): {public_error(e)}')
                if attempt < QUOTE_RETRIES:
                    import time
                    time.sleep(QUOTE_RETRY_PAUSE)
    return out


def fetch_vix():
    for attempt in range(1, QUOTE_RETRIES + 1):
        try:
            resp = requests.get(f'{FMP_BASE_V3}/quote/%5EVIX?apikey={FMP_API_KEY}',
                                timeout=10)
            data = resp.json()
            if isinstance(data, list) and data:
                return float(data[0].get('price') or 0) or None
            print(f'  ⚠ VIX quote attempt {attempt}/{QUOTE_RETRIES} returned no price.')
            if attempt < QUOTE_RETRIES:
                import time
                time.sleep(QUOTE_RETRY_PAUSE)
        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f'  ⚠ VIX quote attempt {attempt}/{QUOTE_RETRIES} failed: {public_error(exc)}')
            if attempt < QUOTE_RETRIES:
                import time
                time.sleep(QUOTE_RETRY_PAUSE)
    return None


def bucket_label(drop_pct, vix):
    a = abs(drop_pct)
    db = next((lab for lo, hi, lab in DROP_BUCKETS if lo <= a < hi), None)
    vb = next((lab for lo, hi, lab in VIX_BANDS if lo <= vix < hi), None)
    return db, vb


def load_prior_records():
    """Per-symbol prior option-trade record as of today from the latest
    strategy results CSV. Returns {symbol: (n, avg)}."""
    candidates = []
    for f in glob.glob(os.path.join(ALERT_DIR, 'Stock_Drop_Strategy_Results_*.csv')):
        m = _DATE_RE.search(f)
        if m:
            candidates.append((m.group(1), f))
    if not candidates:
        print("No Stock_Drop_Strategy_Results_YYYY-MM-DD.csv in Stock_Drop_Alert. "
              "Prior-trade scores will use the pool average only. "
              "The latest results file is part of this repo.")
        return {}
    candidates.sort(reverse=True)
    try:
        sr = pd.read_csv(candidates[0][1])
    except (OSError, ValueError) as exc:
        print(f"Could not read strategy results {candidates[0][1]}: {public_error(exc)}")
        return {}
    if sr.empty or 'Exit_Pct' not in sr.columns:
        return {}
    sr['_dt'] = pd.to_datetime(sr['Entry_Date'], errors='coerce')
    cutoff = pd.Timestamp(datetime.now().date() - timedelta(days=15))
    settled = sr[sr['_dt'] <= cutoff]
    out = {}
    for sym, grp in settled.groupby('Symbol'):
        vals = pd.to_numeric(grp['Exit_Pct'], errors='coerce').dropna()
        if len(vals):
            out[sym] = (int(len(vals)), float(vals.mean()))
    return out


def compute_score(feat_values, rules):
    """Composite score from rules['score_features'] given a feature dict."""
    comps = []
    for f in rules['score_features']:
        v = feat_values.get(f['col'])
        if v is None or (isinstance(v, float) and np.isnan(v)):
            pct = 50.0
        else:
            pct = float(np.interp(float(v), f['train_deciles'],
                                  np.arange(0, 101, 10), left=0, right=100))
        if f['orientation'] < 0:
            pct = 100 - pct
        comps.append(pct)
    return float(np.mean(comps)) if comps else 50.0


def ensure_reminders_list(list_name):
    """Create the Reminders list if it does not exist."""
    name_esc = list_name.replace('\\', '\\\\').replace('"', '\\"')
    script = (
        'tell application "Reminders"\n'
        f'  if not (exists list "{name_esc}") then\n'
        f'    make new list with properties {{name:"{name_esc}"}}\n'
        '  end if\n'
        'end tell\n'
    )
    try:
        r = subprocess.run(['osascript', '-e', script],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            print(f"Reminders list setup failed: {r.stderr.strip() or r.stdout.strip()}")
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Reminders list setup failed: {public_error(exc)}")
        return False


def create_reminder(title, body, list_name=None):
    # osascript is invoked WITHOUT a shell — escape only for the AppleScript
    # double-quoted string literal (backslashes first, then double quotes).
    list_name = list_name or REMINDERS_LIST
    title_esc = title.replace('\\', '\\\\').replace('"', '\\"')
    body_esc = body.replace('"', '\\"')
    list_esc = list_name.replace('\\', '\\\\').replace('"', '\\"')
    script = (
        'tell application "Reminders"\n'
        f'  tell list "{list_esc}"\n'
        '    set dueDate to (current date) + (5 * minutes)\n'
        f'    make new reminder with properties '
        f'{{name:"{title_esc}", body:"{body_esc}", '
        f'due date:dueDate, remind me date:dueDate}}\n'
        '  end tell\n'
        'end tell\n'
    )
    try:
        r = subprocess.run(['osascript', '-e', script],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            print(f"Reminder creation failed: {r.stderr.strip() or r.stdout.strip()}")
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Reminder creation failed: {public_error(exc)}")
        return False


def _rank_3m_strikes(m3, symbol, direction, expiry, center_price):
    """
    Rank strikes ON THE 3-MONTH EXPIRY ITSELF by today's option volume via
    the Polygon options snapshot, within ±THREE_M_STRIKE_BAND_PCT of
    center_price. Open interest breaks volume ties, proximity breaks both.
    Returns [{'strike','ticker','volume','oi'}, ...] best-first ([] on failure).
    """
    if not m3.POLYGON_API_KEY or not center_price or center_price <= 0:
        return []
    opt_type = 'call' if str(direction).lower().startswith('c') else 'put'
    band = THREE_M_STRIKE_BAND_PCT * center_price
    url = f'{m3.POLYGON_BASE}/v3/snapshot/options/{symbol.upper()}'
    params = {
        'contract_type': opt_type,
        'expiration_date': expiry,
        'strike_price.gte': round(center_price - band, 2),
        'strike_price.lte': round(center_price + band, 2),
        'limit': 250,
        'apiKey': m3.POLYGON_API_KEY,
    }
    out = []
    pages = 0
    while url and pages < 4:
        resp = m3._polygon_get(url, params)
        if not resp or resp.status_code != 200:
            break
        try:
            data = resp.json()
        except ValueError:
            break
        for c in data.get('results') or []:
            det = c.get('details') or {}
            strike = det.get('strike_price')
            ticker = det.get('ticker')
            if strike is None or not ticker:
                continue
            vol = int((c.get('day') or {}).get('volume') or 0)
            oi = int(c.get('open_interest') or 0)
            out.append({'strike': float(strike), 'ticker': ticker,
                        'volume': vol, 'oi': oi})
        url = data.get('next_url')
        params = {'apiKey': m3.POLYGON_API_KEY} if url else None
        pages += 1
    out.sort(key=lambda x: (-x['volume'], -x['oi'],
                            abs(x['strike'] - center_price)))
    return out


def resolve_3m_contract(symbol, direction, strike, as_of_str, price_now=None):
    """
    Resolve the ~90 DTE (3-month) contract to recommend.

    Picks the expiry closest to THREE_M_TARGET_DTE, then re-ranks strikes on
    that expiry by today's volume on the 3m chain (±25% of spot, OI tiebreak)
    so the recommendation is the liquid contract at 3 months out — NOT the
    strike the monthly book picked from monthly-chain volume. Falls back to
    the monthly-book strike only if the snapshot is unavailable.
    Returns dict with expiry/ticker/dte/strike/volume/oi/top3/source or None.
    """
    try:
        import stock_drop_3m_pnl_tracker as m3
    except ImportError as e:
        print(f'  ⚠ cannot import 3m helpers: {e}')
        return None

    monthly_strike = None
    if strike is not None and not (isinstance(strike, float) and np.isnan(strike)):
        try:
            monthly_strike = float(strike)
        except (TypeError, ValueError):
            monthly_strike = None

    expiry = m3.pick_3m_expiry(
        symbol, direction, as_of_str,
        target_dte=THREE_M_TARGET_DTE, band=THREE_M_BAND,
    )
    if not expiry:
        return None

    center = None
    try:
        if price_now is not None and float(price_now) > 0:
            center = float(price_now)
    except (TypeError, ValueError):
        pass
    if center is None:
        center = monthly_strike

    ranked = []
    try:
        ranked = _rank_3m_strikes(m3, symbol, direction, expiry, center)
    except Exception as e:
        print(f'  ⚠ {symbol}: 3m chain volume ranking failed ({e}) — '
              f'falling back to monthly-book strike')

    if ranked:
        best = ranked[0]
        chosen_strike, ticker = best['strike'], best['ticker']
        volume, oi = best['volume'], best['oi']
        top3 = ';'.join(f"{r['strike']:g}:{r['volume']}:{r['oi']}"
                        for r in ranked[:3])
        source = '3m_chain_volume'
    else:
        if monthly_strike is None:
            return None
        chosen_strike = monthly_strike
        ticker = m3.resolve_contract(symbol, direction, monthly_strike, expiry)
        if not ticker:
            return None
        volume = oi = None
        top3 = None
        source = 'monthly_strike_fallback'

    try:
        as_of = datetime.strptime(as_of_str, '%Y-%m-%d').date()
        exp_d = datetime.strptime(expiry, '%Y-%m-%d').date()
        dte = (exp_d - as_of).days
    except ValueError:
        dte = None
    return {
        'expiry': expiry,
        'ticker': ticker,
        'dte': dte,
        'strike': chosen_strike,
        'volume': volume,
        'oi': oi,
        'top3': top3,
        'source': source,
    }


def _fmt_top3_3m(raw):
    """'60:217:1024;55:88:310' (strike:vol:oi) -> reminder lines for the top
    in-band strikes ON THE 3M EXPIRY, best first."""
    if not raw or not str(raw).strip():
        return ''
    parts = []
    for i, item in enumerate(str(raw).split(';'), 1):
        try:
            strike, vol, oi = item.split(':')
            marker = ' <- CHOSEN' if i == 1 else ''
            parts.append(f"  {i}. ${float(strike):g} (vol {int(vol):,}, "
                         f"OI {int(oi):,}){marker}")
        except ValueError:
            continue
    if not parts:
        return ''
    return '\\n3m strikes by volume (±25% band):\\n' + '\\n'.join(parts)


def emit_3m_reminders(final, today_str, dry_run=False):
    """
    For each v2-confirmed entry, resolve the ~90 DTE contract — strike picked
    by volume on the 3m expiry itself (±25% band; monthly-book strike only as
    fallback) — and post a reminder to the "3 Month Options" list with TP/SL
    + 20d hold. Also writes Stock_Drop_3m_Confirmed_Entries_{date}.csv / .txt.
    """
    if not final:
        print('\n3m book: no confirmed entries — nothing to post.')
        return []

    print('\n' + '=' * 96)
    print(f'3 MONTH OPTIONS (~{THREE_M_TARGET_DTE} DTE, hold {THREE_M_MAX_HOLD_DAYS} trading days)')
    print(f'Same v2 confirmation gate · enter NOW (~12:45-1:00 PM PT)')
    print('=' * 96)

    rows = []
    for c in final:
        sym = c['Symbol']
        direction = c['Direction']
        strike = c.get('Strike')
        monthly_strike = (float(strike) if strike is not None and not (
            isinstance(strike, float) and np.isnan(strike)) else None)
        resolved = resolve_3m_contract(sym, direction, strike, today_str,
                                       price_now=c.get('Price_Now'))
        # This reminder is the 20-day book. 10-15% with VIX under 20 keeps
        # +150% here and holds on the 10-day books.
        vix_row = c.get('VIX_Now')
        std20 = (standard_take_profit(
            c.get('Drop_Pct'), vix_row, hold_days=THREE_M_MAX_HOLD_DAYS)
            if vix_row else None)
        tp20 = std20[0] if std20 else c.get('TP')
        sl20 = std20[1] if std20 else c.get('SL')
        row = {
            'Symbol': sym,
            'Direction': direction,
            'Score': c.get('Score'),
            'Drop_Pct': c.get('Drop_Pct'),
            'D1_Ret_Pct': c.get('D1_Ret_Pct'),
            'Tier_Cell': c.get('Tier_Cell'),
            'TP': tp20,
            'SL': sl20,
            'Strike': resolved['strike'] if resolved else monthly_strike,
            'Monthly_Strike': monthly_strike,
            'Monthly_Expiry': c.get('Expiry'),
            'Monthly_Ticker': c.get('Option_Ticker'),
            'Expiry': resolved['expiry'] if resolved else None,
            'Option_Ticker': resolved['ticker'] if resolved else None,
            'DTE': resolved['dte'] if resolved else None,
            'Volume_3m': resolved.get('volume') if resolved else None,
            'OI_3m': resolved.get('oi') if resolved else None,
            'Top3_Strikes_3m': resolved.get('top3') if resolved else None,
            'Strike_Source': resolved.get('source') if resolved else None,
            'Max_Hold_Days': THREE_M_MAX_HOLD_DAYS,
            'Entry_Fill': 'day_close',
            'Entry_Window': '12:45 PT',
            'Price_Now': c.get('Price_Now'),
            'Top3_Strikes': c.get('Top3_Strikes'),
            'Decision': 'confirmed',
            'Reason': 'confirmed_3m_parallel',
        }
        rows.append(row)

    table = []
    for i, r in enumerate(rows, 1):
        table.append([
            i, r['Symbol'], r['Direction'],
            f"{r['Score']:.0f}" if r.get('Score') is not None else '-',
            f"${r['Strike']:g}" if r.get('Strike') is not None else '-',
            r.get('Expiry') or '—',
            r.get('DTE') if r.get('DTE') is not None else '—',
            r.get('Option_Ticker') or 'NO 3M CONTRACT',
            f"{r['Volume_3m']:,}" if r.get('Volume_3m') is not None else '—',
            f"{r['OI_3m']:,}" if r.get('OI_3m') is not None else '—',
            f"+{r['TP']}%" if r.get('TP') else f'Hold D{THREE_M_MAX_HOLD_DAYS}',
            f"{r['SL']}%",
            f"{THREE_M_MAX_HOLD_DAYS}d",
        ])
    print(tabulate(
        table,
        headers=['#', 'Sym', 'Dir', 'Score', 'Strike', '3m Exp', 'DTE',
                 'Ticker', '3mVol', 'OI', 'TP', 'SL', 'Hold'],
        tablefmt='simple', numalign='right',
    ))

    out_csv = os.path.join(ALERT_DIR, f'Stock_Drop_3m_Confirmed_Entries_{today_str}.csv')
    out_txt = os.path.join(ALERT_DIR, f'Stock_Drop_3m_Confirmed_Entries_{today_str}.txt')
    if dry_run:
        print(f'\n[dry-run] would write {out_csv} and reminders → "{THREE_M_REMINDERS_LIST}"')
        return rows

    pd.DataFrame(rows).to_csv(out_csv, index=False)
    with open(out_txt, 'w') as f:
        f.write(f'Stock Drop 3-Month Confirmed Entries — {today_str}\n')
        f.write(f'Target DTE ~{THREE_M_TARGET_DTE} | Max hold {THREE_M_MAX_HOLD_DAYS} trading days\n')
        f.write(f'Entry: ~12:45 PM PT (same v2 green gate as monthly book)\n')
        f.write(f'Strike picked by volume on the 3m expiry itself '
                f'(±{THREE_M_STRIKE_BAND_PCT:.0%} band, OI tiebreak)\n')
        f.write('=' * 96 + '\n')
        f.write(tabulate(
            table,
            headers=['#', 'Sym', 'Dir', 'Score', 'Strike', '3m Exp', 'DTE',
                     'Ticker', '3mVol', 'OI', 'TP', 'SL', 'Hold'],
            tablefmt='simple', numalign='right',
        ))
        f.write('\n')
    print(f'\n3m confirmed CSV: {out_csv}')
    print(f'3m confirmed TXT: {out_txt}')

    if not ensure_reminders_list(THREE_M_REMINDERS_LIST):
        print(f"  ⚠ could not ensure Reminders list '{THREE_M_REMINDERS_LIST}' "
              f"— create it once in the Reminders app if missing")

    made = 0
    skipped = 0
    for i, r in enumerate(rows, 1):
        if not r.get('Option_Ticker') or not r.get('Expiry'):
            skipped += 1
            print(f"  ✗ {r['Symbol']}: no ~{THREE_M_TARGET_DTE}DTE contract for "
                  f"strike {r.get('Strike')} — skipped reminder")
            continue
        tp_str = f"+{r['TP']}%" if r.get('TP') else f'Hold D{THREE_M_MAX_HOLD_DAYS}'
        sl_str = f"{r['SL']}%" if r.get('SL') is not None else '-40%'
        d1 = r.get('D1_Ret_Pct')
        d1_str = f"{d1:+.1f}%" if d1 is not None else 'n/a'
        if r.get('Strike_Source') == '3m_chain_volume':
            vol_line = (f"3m chain vol today: {r['Volume_3m']:,} | "
                        f"OI: {r['OI_3m']:,}")
            vol_line += _fmt_top3_3m(r.get('Top3_Strikes_3m'))
        else:
            vol_line = ("3m chain vol: unavailable — monthly-book strike "
                        "carried over (verify liquidity before entry)")
        body = (
            f"3M STRATEGY — enter NOW (~12:45-1:00 PM PT)\\n"
            f"Strike: ${r['Strike']:g} | Expiry: {r['Expiry']} "
            f"(DTE {r.get('DTE', '?')})\\n"
            f"Ticker: {r['Option_Ticker']}\\n"
            f"{vol_line}\\n"
            f"TP: {tp_str} | SL: {sl_str} | Hold: {THREE_M_MAX_HOLD_DAYS} trading days max\\n"
            f"Day 1: {d1_str} | Tier: {r.get('Tier_Cell') or '-'}\\n"
            f"Score: {r['Score']:.0f} | Drop: {r.get('Drop_Pct')}%\\n"
            f"Monthly book ref (not this trade): {r.get('Monthly_Ticker')} "
            f"exp {r.get('Monthly_Expiry')} strike ${r.get('Monthly_Strike') or 0:g}"
        )
        ok = create_reminder(
            f"3M ENTER #{i} {r['Symbol']} {r['Direction']} — "
            f"score {r['Score']:.0f} | hold {THREE_M_MAX_HOLD_DAYS}d",
            body,
            list_name=THREE_M_REMINDERS_LIST,
        )
        made += int(ok)
    print(f"3m Reminders: created {made}/{len(rows) - skipped} in "
          f"'{THREE_M_REMINDERS_LIST}'"
          + (f" ({skipped} skipped — no 3m contract)" if skipped else ''))
    return rows


def main():
    parser = argparse.ArgumentParser(description='v2 Day-1 confirmation entry')
    parser.add_argument('--date', type=str, default=None,
                        help='Treat this date as today (YYYY-MM-DD)')
    parser.add_argument('--dry-run', action='store_true',
                        help='No output files or reminders')
    args = parser.parse_args()

    today = (datetime.strptime(args.date, '%Y-%m-%d').date()
             if args.date else datetime.now().date())
    today_str = today.strftime('%Y-%m-%d')

    if not is_trading_day(today):
        print(f'{today_str} is not a trading day — nothing to confirm.')
        return

    # Guard against deferred launchd fires (machine asleep at 12:45):
    # entering "near the close" after 1 PM PT is stale — skip instead.
    if not args.date and datetime.now().hour >= 13:
        print('⚠ Market already closed (fired after 1 PM PT) — skipping; '
              'entries would be stale.')
        return

    out_txt = os.path.join(ALERT_DIR, f'Stock_Drop_Confirmed_Entries_{today_str}.txt')
    out_csv = os.path.join(ALERT_DIR, f'Stock_Drop_Confirmed_Entries_{today_str}.csv')
    if os.path.exists(out_txt) and not args.dry_run:
        print(f'Already confirmed today: {out_txt}')
        return

    prev = prev_trading_day(today)
    cand_file = os.path.join(ALERT_DIR,
                             f'Stock_Drop_Candidates_{prev.strftime("%Y-%m-%d")}.csv')
    if not os.path.exists(cand_file):
        print(f'No candidates file from previous session ({cand_file}). '
              f'Run stock_drop_watchlist.py for {prev.strftime("%Y-%m-%d")} first.')
        return
    require_env('FMP_API_KEY', 'POLYGON_API_KEY')

    if not os.path.exists(RULES_FILE):
        print(f'⚠ {RULES_FILE} missing — run stock_drop_v2_validation.py first.')
        sys.exit(1)
    with open(RULES_FILE) as f:
        rules = json.load(f)

    cands = pd.read_csv(cand_file)
    if cands.empty:
        print('Candidates file is empty — done.')
        return

    # Fetch quotes BEFORE creating the output file: if the network is down,
    # exit non-zero with no output so a retry/rerun is not blocked by the
    # already-confirmed check.
    symbols = cands['Symbol'].astype(str).tolist()
    quotes = fetch_quotes(symbols)
    if not quotes:
        print(f'⚠ No quotes fetched for {len(symbols)} candidates — '
              f'aborting without output (rerun will retry).')
        sys.exit(1)
    vix_now = fetch_vix()
    if vix_now is None:
        # Tier tradability depends on the live VIX band; a stale fallback
        # would default into the most permissive band. Abort and let the
        # retry fire / manual rerun handle it.
        print('⚠ VIX unavailable after retries — aborting without output '
              '(rerun will retry).')
        sys.exit(1)

    tee = None
    failed = False
    try:
        if not args.dry_run:
            tee = TeeOutput(out_txt)
            sys.stdout = tee
        _run(cands, rules, today_str, prev, out_csv, args.dry_run,
             quotes, vix_now)
    except BaseException:
        failed = True
        raise
    finally:
        if tee:
            sys.stdout = tee.terminal
            tee.close()
            if failed:
                # Don't let a crashed run satisfy the already-confirmed
                # check and block the retry.
                try:
                    os.remove(out_txt)
                    print(f'Removed partial output after failure: {out_txt}')
                except OSError as exc:
                    print(f'Could not remove partial output {out_txt}: {public_error(exc)}')
            else:
                print(f'Saved: {out_txt}')


def _run(cands, rules, today_str, prev, out_csv, dry_run, quotes, vix_now):
    print('=' * 96)
    print('STOCK DROP v2 — DAY-1 CONFIRMATION ENTRY')
    print(f'Candidates from: {prev}  |  Confirmation: {today_str} '
          f'{datetime.now().strftime("%H:%M:%S")} PT')
    print(f'Rules: derived {rules.get("derived")} (train cutoff '
          f'{rules.get("train_cutoff")})')
    print('=' * 96)

    symbols = cands['Symbol'].astype(str).tolist()
    print(f'\n{len(symbols)} candidates: {", ".join(symbols)}')
    print(f'Quotes fetched: {len(quotes)}/{len(symbols)}')
    print(f'VIX now: {vix_now if vix_now else "unavailable"}')

    prior_records = load_prior_records()
    pool_avg = float(rules.get('pool_avg', 0.0))
    shrink_k = float(rules.get('shrink_k', 5))

    # Build a full record per candidate — features and score are computed for
    # EVERYONE so the shadow book can forward-test rejected cohorts too.
    def _num(v):
        return float(v) if pd.notna(v) else np.nan

    records = []
    for _, c in cands.iterrows():
        # Entry-day VIX picks the replay table for the 10-day books.
        # The 3-month reminder recomputes the 20-day cell from this same VIX.
        std = (standard_take_profit(float(c['Drop_Pct']), vix_now, hold_days=10)
               if vix_now else None)
        if std:
            tp_pct, sl_pct, _tp_rule = std
        else:
            tp_pct, sl_pct = None, STANDARD_SL_PCT
        rec = {
            'Symbol': c['Symbol'], 'Direction': c['Direction'],
            'Drop_Pct': float(c['Drop_Pct']), 'Score': None,
            'D1_Ret_Pct': None, 'Tier_Cell': None,
            'TP': tp_pct,
            'SL': sl_pct,
            'Strike': c.get('Strike'), 'Expiry': c.get('Expiry'),
            'Option_Ticker': c.get('Option_Ticker'), 'DTE': c.get('DTE'),
            'Top3_Strikes': c.get('Top3_Strikes'),
            'Price_Now': None, 'Prior_N': 0, 'Prior_Avg': None,
            'VIX_Now': round(vix_now, 2),
            'Decision': None, 'Reason': '',
        }
        records.append(rec)
        sym, direction = rec['Symbol'], rec['Direction']

        q = quotes.get(sym)
        if not q or not q.get('open') or not q.get('price'):
            rec['Decision'], rec['Reason'] = 'no_quote', 'no live quote'
            continue

        o, p = float(q['open']), float(q['price'])
        d1_ret = (p - o) / o * 100 if o > 0 else np.nan
        rec['D1_Ret_Pct'] = round(d1_ret, 2) if pd.notna(d1_ret) else None
        rec['Price_Now'] = p

        # Score (computed for every quoted candidate, decision or not)
        drop_close = float(c['Drop_Close'])
        day_low = float(q.get('dayLow') or 0)
        day_high = float(q.get('dayHigh') or 0)
        vol = float(q.get('volume') or 0)
        drop_vol = float(c.get('Drop_Day_Volume') or 0)
        n_prior, avg_prior = prior_records.get(sym, (0, np.nan))
        shrunk = ((n_prior * (avg_prior if not np.isnan(avg_prior) else 0)
                   + shrink_k * pool_avg) / (n_prior + shrink_k))
        feat = {
            'prior_avg_shrunk': shrunk,
            'pit_conviction': _num(c.get('PIT_Conviction')),
            'pit_wr': _num(c.get('PIT_WR')),
            'dvix': (vix_now - _num(c.get('VIX_At_Drop'))
                     if pd.notna(c.get('VIX_At_Drop')) else np.nan),
            'consec_down_days': _num(c.get('Consec_Down_Days')),
            'pct_below_20d_high': _num(c.get('Pct_Below_20d_High')),
            'ret_5d_pct': _num(c.get('Ret_5d_Pct')),
            'd1_ret_pct': d1_ret,
            'd1_close_loc': ((p - day_low) / (day_high - day_low)
                             if day_high > day_low else np.nan),
            'd1_vol_ratio': (vol / drop_vol) if drop_vol > 0 else np.nan,
            'd1_reclaim': float(p > drop_close),
            'gap_pct': (o - drop_close) / drop_close * 100 if drop_close > 0 else np.nan,
        }
        rec['Score'] = round(compute_score(feat, rules), 1)
        rec['Prior_N'] = n_prior
        rec['Prior_Avg'] = round(avg_prior, 1) if not np.isnan(avg_prior) else None

        # Tier cell (recorded for every quoted candidate)
        db, vb = bucket_label(rec['Drop_Pct'], vix_now)
        rec['Tier_Cell'] = f'{db}|{vb}'
        cell = rules['tier_table'].get(f'{db}|{vb}') if db and vb else None

        # 1. Day-1 gate
        if direction == 'Call' and not p > o:
            rec['Decision'] = 'gate_rejected'
            rec['Reason'] = f'gate: Day 1 red ({d1_ret:+.1f}%)'
            continue

        # 2. Tier check (vix_now is guaranteed non-None by main())
        if not cell or not cell.get('tradable'):
            reason = cell.get('reason') if cell else 'no tier cell'
            rec['Decision'] = 'tier_cut'
            rec['Reason'] = f'tier cut: {db}|{vb} ({reason})'
            continue

    # 4. Floor + 5. Cap (floor may be null = disabled by validation)
    floor_raw = rules.get('score_floor')
    floor = float(floor_raw) if floor_raw is not None else None
    cap = int(rules.get('cap_per_day', 15))
    eligible = [r for r in records if r['Decision'] is None]
    for r in eligible:
        if floor is not None and r['Score'] <= floor:
            r['Decision'] = 'below_floor'
            r['Reason'] = f'below score floor ({r["Score"]:.0f} <= {floor:.0f})'
    above = [r for r in eligible if r['Decision'] is None]
    above.sort(key=lambda x: -x['Score'])
    final = above[:cap]
    for r in above[cap:]:
        r['Decision'] = 'capped'
        r['Reason'] = f'capped (rank past {cap})'
    for r in final:
        r['Decision'] = 'confirmed'
        r['Reason'] = 'confirmed'

    rejected = [(r['Symbol'], r['Direction'], r['Score'], r['Reason'])
                for r in records if r['Decision'] != 'confirmed']

    # ── Output ──
    print('\n' + '=' * 96)
    print(f'CONFIRMED ENTRIES ({len(final)}) — enter NOW (~12:45-1:00 PM PT)')
    print('=' * 96)
    if final:
        table = [[i + 1, c['Symbol'], c['Direction'], f"{c['Score']:.0f}",
                  f"{c['Drop_Pct']:.1f}%", f"{c['D1_Ret_Pct']:+.1f}%",
                  c['Tier_Cell'],
                  f"+{c['TP']}%" if c['TP'] else 'D10', f"{c['SL']}%",
                  c['Strike'], c['Expiry'], c['Option_Ticker']]
                 for i, c in enumerate(final)]
        print(tabulate(table, headers=['#', 'Sym', 'Dir', 'Score', 'Drop',
                                       'D1', 'Tier', 'TP', 'SL', 'Strike',
                                       'Expiry', 'Ticker'],
                       tablefmt='simple', numalign='right'))
    else:
        print('  None — no candidate passed gate + tier + floor today.')

    if rejected:
        print(f'\nREJECTED ({len(rejected)}):')
        for sym, direction, score, reason in rejected:
            s = f' score {score:.0f}' if score is not None else ''
            print(f'  ✗ {sym} {direction}{s} — {reason}')

    CSV_COLS = ['Symbol', 'Direction', 'Score', 'Drop_Pct', 'D1_Ret_Pct',
                'Tier_Cell', 'TP', 'SL', 'Strike', 'Expiry', 'Option_Ticker',
                'DTE', 'Top3_Strikes', 'Price_Now', 'Prior_N', 'Prior_Avg']
    DECISIONS_COLS = CSV_COLS + ['VIX_Now', 'Decision', 'Reason']
    def _fmt_top3(raw):
        """'240:1211;280:845' -> reminder lines listing strikes by volume."""
        if raw is None or (isinstance(raw, float) and np.isnan(raw)) or not str(raw).strip():
            return ''
        parts = []
        for i, item in enumerate(str(raw).split(';'), 1):
            try:
                strike, vol = item.split(':')
                marker = ' <- BEST' if i == 1 else ''
                parts.append(f"  {i}. ${float(strike):g} (vol {int(float(vol)):,}){marker}")
            except ValueError:
                continue
        if not parts:
            return ''
        return '\\nStrikes by volume (±25% band):\\n' + '\\n'.join(parts)

    if not dry_run:
        pd.DataFrame(final, columns=CSV_COLS).to_csv(out_csv, index=False)
        print(f'\nConfirmed CSV: {out_csv}')
        # Full per-candidate decision log — consumed by the shadow P&L book
        decisions_csv = os.path.join(
            ALERT_DIR, f'Stock_Drop_Confirmation_Decisions_{today_str}.csv')
        pd.DataFrame(records, columns=DECISIONS_COLS).to_csv(
            decisions_csv, index=False)
        print(f'Decisions CSV: {decisions_csv}')
        # Monthly ENTER reminders decommissioned — same names go to 3m list only.
        # Main monthly P&L book is still fed via --add-from-confirmed in the shell.
        print(f"Monthly Reminders: skipped "
              f"(decommissioned; use '{THREE_M_REMINDERS_LIST}' instead)")

        # Active book: 3-month (~90 DTE, 20d hold) → "3 Month Options"
        emit_3m_reminders(final, today_str, dry_run=False)
    else:
        # dry-run still previews the 3m mapping
        emit_3m_reminders(final, today_str, dry_run=True)


if __name__ == '__main__':
    main()
