#!/usr/bin/env python3
"""
Stock Drop PIT (Point-In-Time) Module
-------------------------------------
Shared helpers for the stock drop v2 pipeline. Everything here answers the
question "what was knowable at the time of the drop?" — no look-ahead.

Provides:
  PITAnalyzer
    .get_daily(symbol)              cached 2019+ daily OHLCV (FMP)
    .get_drops(symbol)              all 5%+ drops with 10-day forward returns
    .bounce_stats(symbol, as_of)    PIT direction stats (closed windows only,
                                    5-year lookback, equal-weight default)
    .cascade_features(symbol, drop_date)
    .day1_features(symbol, drop_date)
    .is_earnings_drop(symbol, drop_date)
  pit_trade_record(strategy_df, ...)  per-symbol prior option-trade record

Caches (Stock_Drop_Alert/.pit_cache/):
  daily_{SYMBOL}.pkl      per-symbol daily OHLCV DataFrame
  earnings_cache.pkl      {symbol: [(date_str, time_str), ...]}

Used by: stock_drop_pit_features.py, stock_drop_v2_validation.py,
         stock_drop_watchlist.py, stock_drop_confirmation_entry.py
"""

import os
import time
import pickle
from datetime import datetime, timedelta, date as date_type

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_SCRIPT_DIR, '.env'))

FMP_API_KEY = os.getenv('FMP_API_KEY')
FMP_BASE_V3 = 'https://financialmodelingprep.com/api/v3'

CACHE_DIR = os.path.join(_SCRIPT_DIR, 'Stock_Drop_Alert', '.pit_cache')
EARNINGS_CACHE_FILE = os.path.join(CACHE_DIR, 'earnings_cache.pkl')

DAILY_FROM = '2019-01-01'   # covers 5y lookback for drops from 2024 onward
DROP_THRESHOLD = -5.0
FORWARD_DAYS = 10
DAYS_PER_MONTH = 365.25 / 12  # 30.4375
# Live standard after other-side put resim (2026-08-26): 5-year equal-weight.
# 5-month + 4-month half-life lost on 2025+ put $ and full-book $.
LOOKBACK_MONTHS = 60
RECENCY_HALF_LIFE_MONTHS = 0.0  # 0 = equal weight inside the lookback
YEARS_LOOKBACK = 5
RECENCY_HALF_LIFE_YEARS = 0.0

FMP_MIN_INTERVAL = 0.15
_last_fmp_ts = 0.0


def _fmp_get_json(url, timeout=20):
    """Paced FMP GET. Returns parsed JSON or None."""
    global _last_fmp_ts
    elapsed = time.time() - _last_fmp_ts
    if elapsed < FMP_MIN_INTERVAL:
        time.sleep(FMP_MIN_INTERVAL - elapsed)
    try:
        resp = requests.get(url, timeout=timeout)
        _last_fmp_ts = time.time()
        resp.raise_for_status()
        return resp.json()
    except Exception:
        _last_fmp_ts = time.time()
        return None


def _to_date(d):
    """Coerce str/datetime/date to datetime.date."""
    if isinstance(d, date_type) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    return datetime.strptime(str(d)[:10], '%Y-%m-%d').date()


def _recency_weights(drop_dates, as_of, half_life_months):
    """Exponential decay vs as_of on a monthly clock. Newest=1.0;
    age=half_life_months → 0.5.

    Age is measured at `as_of` (PIT). half_life_months <= 0 disables decay.
    """
    n = len(drop_dates)
    if n == 0:
        return np.array([], dtype=float)
    if not half_life_months or float(half_life_months) <= 0:
        return np.ones(n, dtype=float)
    ages = np.array(
        [(_to_date(as_of) - _to_date(d)).days / DAYS_PER_MONTH
         for d in drop_dates],
        dtype=float,
    )
    ages = np.clip(ages, 0.0, None)
    return np.power(2.0, -ages / float(half_life_months))


class PITAnalyzer:
    """Per-symbol daily history + drop tables with point-in-time slicing."""

    def __init__(self, refresh_through=None, verbose=False):
        """
        refresh_through: date — refetch a symbol's daily cache if it does not
        extend through this date (use yesterday for live runs, None for
        historical batch runs where any cache covering the data suffices).
        """
        os.makedirs(CACHE_DIR, exist_ok=True)
        self.refresh_through = _to_date(refresh_through) if refresh_through else None
        self.verbose = verbose
        self._daily = {}        # symbol -> DataFrame
        self._drops = {}        # symbol -> DataFrame
        self._earnings = None   # {symbol: [(date, time), ...]}

    # ── Daily OHLCV ───────────────────────────────────────────────────────

    def _daily_cache_path(self, symbol):
        safe = symbol.replace('/', '_').replace('.', '_')
        return os.path.join(CACHE_DIR, f'daily_{safe}.pkl')

    def get_daily(self, symbol):
        """Daily OHLCV DataFrame (ascending by date) with columns:
        date (datetime.date), open, high, low, close, volume. Empty df if
        unavailable."""
        if symbol in self._daily:
            return self._daily[symbol]

        path = self._daily_cache_path(symbol)
        cached = None
        if os.path.exists(path):
            try:
                with open(path, 'rb') as f:
                    cached = pickle.load(f)
            except Exception:
                cached = None

        needs_fetch = cached is None
        if cached is not None and self.refresh_through is not None:
            last = cached['date'].iloc[-1] if len(cached) else None
            if last is None or last < self.refresh_through:
                needs_fetch = True

        df = cached
        if needs_fetch:
            fresh = self._fetch_daily(symbol)
            if fresh is not None and len(fresh):
                # Never replace a longer cache with a truncated fetch
                if cached is None or len(fresh) >= len(cached):
                    df = fresh
                    try:
                        with open(path, 'wb') as f:
                            pickle.dump(fresh, f)
                    except Exception:
                        pass
                else:
                    df = cached
            elif cached is not None:
                # Fetch failed — fall back to the stale-but-usable cache
                df = cached
            else:
                df = pd.DataFrame(
                    columns=['date', 'open', 'high', 'low', 'close', 'volume'])

        self._daily[symbol] = df
        return df

    def _fetch_daily(self, symbol):
        if self.verbose:
            print(f'    [fmp] daily history {symbol}')
        to = datetime.now().strftime('%Y-%m-%d')
        url = (f'{FMP_BASE_V3}/historical-price-full/{symbol}'
               f'?from={DAILY_FROM}&to={to}&apikey={FMP_API_KEY}')
        data = _fmp_get_json(url)
        if not isinstance(data, dict):
            return None
        hist = data.get('historical', [])
        if not hist:
            return None
        rows = []
        for h in hist:
            try:
                rows.append({
                    'date': _to_date(h['date']),
                    'open': float(h['open']), 'high': float(h['high']),
                    'low': float(h['low']), 'close': float(h['close']),
                    'volume': float(h.get('volume', 0) or 0),
                })
            except (KeyError, ValueError, TypeError):
                continue
        if not rows:
            return None
        df = pd.DataFrame(rows).sort_values('date').reset_index(drop=True)
        df = df[df['close'] > 0]
        return df.reset_index(drop=True)

    # ── Earnings dates ────────────────────────────────────────────────────

    def _load_earnings_cache(self):
        if self._earnings is not None:
            return
        self._earnings = {}
        if os.path.exists(EARNINGS_CACHE_FILE):
            try:
                with open(EARNINGS_CACHE_FILE, 'rb') as f:
                    self._earnings = pickle.load(f)
            except Exception:
                self._earnings = {}

    def _save_earnings_cache(self):
        try:
            with open(EARNINGS_CACHE_FILE, 'wb') as f:
                pickle.dump(self._earnings, f)
        except Exception:
            pass

    EARNINGS_CACHE_MAX_AGE_DAYS = 7

    def get_earnings_dates(self, symbol):
        """List of (date, time_str) earnings reports for symbol, or [].
        Cached entries are stamped with their fetch date and refetched when
        older than EARNINGS_CACHE_MAX_AGE_DAYS (FMP includes upcoming
        scheduled reports, so a week of staleness is safe)."""
        self._load_earnings_cache()
        entry = self._earnings.get(symbol)
        if entry is not None:
            # Old cache format (plain list) is treated as undated → stale
            if isinstance(entry, tuple) and len(entry) == 2:
                fetched, dates = entry
                age = (datetime.now().date() - fetched).days
                if age <= self.EARNINGS_CACHE_MAX_AGE_DAYS:
                    return dates
            elif isinstance(entry, list) and self.refresh_through is None:
                return entry
        if self.verbose:
            print(f'    [fmp] earnings calendar {symbol}')
        url = (f'{FMP_BASE_V3}/historical/earning_calendar/{symbol}'
               f'?apikey={FMP_API_KEY}')
        data = _fmp_get_json(url)
        if data is None and entry is not None:
            # Fetch failed — keep using the stale entry
            return entry[1] if isinstance(entry, tuple) else entry
        out = []
        if isinstance(data, list):
            for e in data:
                d = e.get('date')
                if not d:
                    continue
                try:
                    out.append((_to_date(d), (e.get('time') or '').lower()))
                except ValueError:
                    continue
        self._earnings[symbol] = (datetime.now().date(), out)
        self._save_earnings_cache()
        return out

    def is_earnings_drop(self, symbol, drop_date):
        """True if the drop on drop_date is plausibly an earnings reaction:
        - report dated drop_date with time != 'amc' (reaction same day), or
        - report dated the previous session with time 'amc' or unknown
          (reaction next morning)."""
        drop_date = _to_date(drop_date)
        earnings = self.get_earnings_dates(symbol)
        if not earnings:
            return False
        df = self.get_daily(symbol)
        prev_session = None
        if len(df):
            dates = df['date'].tolist()
            for i, d in enumerate(dates):
                if d == drop_date and i > 0:
                    prev_session = dates[i - 1]
                    break
        for e_date, e_time in earnings:
            if e_date == drop_date and e_time != 'amc':
                return True
            if prev_session and e_date == prev_session and e_time in ('amc', '', '--'):
                return True
            # No daily data — fall back to calendar-day adjacency
            if prev_session is None and drop_date - timedelta(days=3) <= e_date <= drop_date:
                return True
        return False

    # ── Drop tables ───────────────────────────────────────────────────────

    def get_drops(self, symbol):
        """All 5%+ down days for symbol with forward returns. DataFrame:
        date, idx (position in daily df), close, change_pct,
        fwd_ret_10d (NaN if window not closed by end of data),
        is_earnings (bool)."""
        if symbol in self._drops:
            return self._drops[symbol]

        df = self.get_daily(symbol)
        if df is None or len(df) < 2:
            out = pd.DataFrame(columns=['date', 'idx', 'close', 'change_pct',
                                        'fwd_ret_10d', 'is_earnings'])
            self._drops[symbol] = out
            return out

        closes = df['close'].values
        chg = np.full(len(df), np.nan)
        chg[1:] = (closes[1:] - closes[:-1]) / closes[:-1] * 100

        rows = []
        n = len(df)
        for i in np.where(chg <= DROP_THRESHOLD)[0]:
            fwd = np.nan
            if i + FORWARD_DAYS < n:
                fwd = (closes[i + FORWARD_DAYS] - closes[i]) / closes[i] * 100
            rows.append({
                'date': df['date'].iloc[i],
                'idx': int(i),
                'close': float(closes[i]),
                'change_pct': float(chg[i]),
                'fwd_ret_10d': fwd,
            })
        out = pd.DataFrame(rows)
        if len(out):
            out['is_earnings'] = out['date'].map(
                lambda d: self.is_earnings_drop(symbol, d))
        else:
            out = pd.DataFrame(columns=['date', 'idx', 'close', 'change_pct',
                                        'fwd_ret_10d', 'is_earnings'])
        self._drops[symbol] = out
        return out

    # ── PIT bounce stats (direction input) ────────────────────────────────

    def bounce_stats(self, symbol, as_of, earnings_weight=1.0,
                     recency_half_life_months=RECENCY_HALF_LIFE_MONTHS,
                     lookback_months=LOOKBACK_MONTHS,
                     recency_half_life_years=None):
        """
        Direction stats from 5%+ drops strictly knowable at entry time for a
        drop on `as_of`: drop windows must have fully closed before the entry
        session (i.e. prior drop index <= as_of index - FORWARD_DAYS), within
        the trailing `lookback_months` (live default 60 = 5 years).

        Weight per drop = recency_decay(age in months at as_of) ×
        earnings_weight (earnings) or 1.0 (non-earnings). Recency is
        exponential with `recency_half_life_months` (default 4.0; 0 = equal
        weight inside the window). Returns dict:
          direction, wavg_return, win_rate, conviction, n_drops,
          n_earnings, n_non_earnings, avg_earnings, avg_non_earnings,
          wr_earnings, wr_non_earnings, recency_half_life_months,
          lookback_months
        or None if fewer than 1 usable drop / no daily data.
        """
        as_of = _to_date(as_of)
        df = self.get_daily(symbol)
        if df is None or len(df) < 30:
            return None
        pos = df['date'].searchsorted(as_of)
        if pos >= len(df) or df['date'].iloc[pos] != as_of:
            # as_of not a session in our data; use position before it
            pos = max(0, pos - 1)

        drops = self.get_drops(symbol)
        if drops.empty:
            return None

        if recency_half_life_years is not None and recency_half_life_months == RECENCY_HALF_LIFE_MONTHS:
            recency_half_life_months = float(recency_half_life_years) * 12.0

        lookback_months = float(lookback_months) if lookback_months else LOOKBACK_MONTHS
        cutoff_date = as_of - timedelta(days=int(round(lookback_months * DAYS_PER_MONTH)))
        usable = drops[(drops['idx'] <= pos - FORWARD_DAYS)
                       & (drops['date'] >= cutoff_date)
                       & drops['fwd_ret_10d'].notna()]
        if usable.empty:
            return None

        rets = usable['fwd_ret_10d'].values
        is_earn = usable['is_earnings'].values.astype(bool)
        recency = _recency_weights(
            usable['date'].tolist(), as_of, recency_half_life_months)
        earn_w = np.where(is_earn, earnings_weight, 1.0).astype(float)
        weights = recency * earn_w

        if weights.sum() <= 0:
            return None
        wavg = float(np.average(rets, weights=weights))
        win_rate = float(np.average((rets > 0).astype(float), weights=weights) * 100)

        e_rets, ne_rets = rets[is_earn], rets[~is_earn]
        hl = (float(recency_half_life_months)
              if recency_half_life_months and float(recency_half_life_months) > 0
              else 0.0)
        return {
            'direction': 'Call' if wavg > 0 else 'Put',
            'wavg_return': wavg,
            'win_rate': win_rate,
            'conviction': abs(wavg),
            'n_drops': int(len(usable)),
            'n_earnings': int(is_earn.sum()),
            'n_non_earnings': int((~is_earn).sum()),
            'avg_earnings': float(e_rets.mean()) if len(e_rets) else np.nan,
            'avg_non_earnings': float(ne_rets.mean()) if len(ne_rets) else np.nan,
            'wr_earnings': float((e_rets > 0).mean() * 100) if len(e_rets) else np.nan,
            'wr_non_earnings': float((ne_rets > 0).mean() * 100) if len(ne_rets) else np.nan,
            'recency_half_life_months': hl,
            'lookback_months': lookback_months,
        }

    # ── Context features (all observable at drop close / D1 close) ────────

    def cascade_features(self, symbol, drop_date):
        """Sequence features as of the drop close:
        consec_down_days, pct_below_20d_high, ret_5d_pct."""
        drop_date = _to_date(drop_date)
        df = self.get_daily(symbol)
        out = {'consec_down_days': np.nan, 'pct_below_20d_high': np.nan,
               'ret_5d_pct': np.nan}
        if df is None or len(df) < 25:
            return out
        pos = df['date'].searchsorted(drop_date)
        if pos >= len(df) or df['date'].iloc[pos] != drop_date or pos < 21:
            return out

        closes = df['close'].values
        consec = 0
        i = pos
        while i >= 1 and closes[i] < closes[i - 1]:
            consec += 1
            i -= 1
        out['consec_down_days'] = consec

        prior_20_high = closes[pos - 20:pos].max()
        if prior_20_high > 0:
            out['pct_below_20d_high'] = (closes[pos] / prior_20_high - 1) * 100
        if pos >= 5:
            out['ret_5d_pct'] = (closes[pos] / closes[pos - 5] - 1) * 100
        return out

    def day1_features(self, symbol, drop_date):
        """Day-1 (entry-session) observation features, knowable at D1 close:
        d1_ret_pct (close vs open), d1_green, gap_pct (D1 open vs drop close),
        d1_close_loc (position of close in D1 range), d1_vol_ratio (D1 volume
        vs drop-day volume), d1_reclaim (D1 close > drop close)."""
        drop_date = _to_date(drop_date)
        df = self.get_daily(symbol)
        out = {'d1_ret_pct': np.nan, 'd1_green': np.nan, 'gap_pct': np.nan,
               'd1_close_loc': np.nan, 'd1_vol_ratio': np.nan,
               'd1_reclaim': np.nan}
        if df is None or len(df) < 2:
            return out
        pos = df['date'].searchsorted(drop_date)
        if pos >= len(df) or df['date'].iloc[pos] != drop_date or pos + 1 >= len(df):
            return out

        drop = df.iloc[pos]
        d1 = df.iloc[pos + 1]
        if d1['open'] > 0:
            out['d1_ret_pct'] = (d1['close'] - d1['open']) / d1['open'] * 100
            out['d1_green'] = int(d1['close'] > d1['open'])
        if drop['close'] > 0:
            out['gap_pct'] = (d1['open'] - drop['close']) / drop['close'] * 100
            out['d1_reclaim'] = int(d1['close'] > drop['close'])
        rng = d1['high'] - d1['low']
        if rng > 0:
            out['d1_close_loc'] = (d1['close'] - d1['low']) / rng
        if drop['volume'] and drop['volume'] > 0:
            out['d1_vol_ratio'] = d1['volume'] / drop['volume']
        return out


# ── Per-symbol prior option-trade record (no API needed) ──────────────────────

def pit_trade_record(strategy_df, settle_days=15):
    """
    Point-in-time per-symbol option trade record from a strategy results
    DataFrame (needs Symbol, Entry_Date, Exit_Pct). For each row, computes the
    symbol's record over prior trades that had SETTLED (entry + settle_days
    calendar days <= current entry).

    Returns the input df with added columns: prior_n, prior_wr, prior_avg.
    """
    df = strategy_df.copy()
    df['_entry_dt'] = pd.to_datetime(df['Entry_Date'], errors='coerce')
    df = df.sort_values('_entry_dt').reset_index(drop=True)
    df['prior_n'] = 0
    df['prior_wr'] = np.nan
    df['prior_avg'] = np.nan

    for symbol, grp in df.groupby('Symbol'):
        idxs = grp.index.tolist()
        entries = grp['_entry_dt'].tolist()
        exits = grp['Exit_Pct'].tolist()
        for k, i in enumerate(idxs):
            cur = entries[k]
            if pd.isna(cur):
                continue
            settled = [exits[j] for j in range(k)
                       if pd.notna(entries[j])
                       and entries[j] + pd.Timedelta(days=settle_days) <= cur]
            if settled:
                arr = np.array(settled, dtype=float)
                df.at[i, 'prior_n'] = len(arr)
                df.at[i, 'prior_wr'] = float((arr > 0).mean() * 100)
                df.at[i, 'prior_avg'] = float(arr.mean())
    return df.drop(columns=['_entry_dt'])


if __name__ == '__main__':
    # Smoke test
    pit = PITAnalyzer(verbose=True)
    sym = 'TSLA'
    daily = pit.get_daily(sym)
    print(f'{sym}: {len(daily)} daily bars '
          f'({daily["date"].iloc[0]} → {daily["date"].iloc[-1]})')
    drops = pit.get_drops(sym)
    print(f'{sym}: {len(drops)} drops, {int(drops["is_earnings"].sum())} earnings-tagged')
    stats = pit.bounce_stats(sym, drops['date'].iloc[-1])
    print(f'PIT stats at last drop ({drops["date"].iloc[-1]}): {stats}')
    equal = pit.bounce_stats(sym, drops['date'].iloc[-1],
                             recency_half_life_months=0)
    print(f'equal-weight (no recency): {equal["direction"]} '
          f'wavg={equal["wavg_return"]:+.2f}% WR={equal["win_rate"]:.1f}%')
    print('cascade:', pit.cascade_features(sym, drops['date'].iloc[-1]))
    print('day1:', pit.day1_features(sym, drops['date'].iloc[-1]))
