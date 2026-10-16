"""Live stock-drop take-profit.

The exit target comes from the 2026-10-15 replay of closed funded trades
(Stock_Drop_Alert/Hold_Replay_TP_vs_Hold_2026-10-15.txt). Each drop row was
replayed at its own entry-day VIX with the old percentage and again with no
percentage. The percentage stays only where it made more dollars than the
hold. Everywhere else the book holds to its own time stop.

The monthly sample is the one that decides. The 3-month sample was all VIX
under 20 and nearly a tie, so it does not override the monthly result.

Stop is a flat -40% on every trade. This module does not decide who is
allowed in. v2 still does that. The numbered watchlist keeps the older VIX
caps in numbered_watchlist_blocked.
"""

STANDARD_SL_PCT = -40
PANIC_SPIKE_VIX = 30

# (drop_lo, drop_hi, label). hi is exclusive.
DROP_BUCKETS = (
    (5, 6, '5-6%'),
    (6, 8, '6-8%'),
    (8, 10, '8-10%'),
    (10, 15, '10-15%'),
    (15, 999, '15%+'),
)

# Caps for the numbered watchlist only. Not the exit.
# None = no cap. VIX >= 30 is never a cap (those names stay on the list).
LEGACY_WATCHLIST_VIX_CAP = {
    '5-6%': 20,
    '6-8%': None,
    '8-10%': 20,
    '10-15%': 20,
    '15%+': 20,
}

# Percentage, or None to hold the window.
# A dict is the one split cell: 10-day books hold, a 20-day book keeps +150%.
# 5-6% under 20 was a wash at day 10 (hold ahead by about $9 a trade) and the
# percentage won at day 20, so both windows keep +150%.
# 5-6% at 20-29 keeps +150% on the exit only. The watchlist cap still blocks
# that cell from the numbered list.
EXIT_TP = {
    ('5-6%', 'under20'): 150,
    ('5-6%', '20-29'): 150,
    ('5-6%', '30+'): None,
    ('6-8%', 'under20'): None,
    ('6-8%', '20-29'): None,
    ('6-8%', '30+'): None,
    ('8-10%', 'under20'): None,
    ('8-10%', '20-29'): None,
    ('8-10%', '30+'): None,
    ('10-15%', 'under20'): {10: None, 20: 150},
    ('10-15%', '20-29'): None,
    ('10-15%', '30+'): None,
    ('15%+', 'under20'): 150,
    ('15%+', '20-29'): None,
    ('15%+', '30+'): None,
}


def drop_bucket(drop_pct):
    """Bucket label for a drop, or None when the size is outside 5%+."""
    try:
        drop_abs = abs(float(drop_pct))
    except (TypeError, ValueError):
        return None
    for lo, hi, label in DROP_BUCKETS:
        if lo <= drop_abs < hi:
            return label
    return None


def _vix_band(vix_f):
    if vix_f >= PANIC_SPIKE_VIX:
        return '30+'
    if vix_f >= 20:
        return '20-29'
    return 'under20'


def _window_key(hold_days):
    """10-day rule, or the 20-day rule when the book holds 20 days or more."""
    try:
        n = int(hold_days)
    except (TypeError, ValueError):
        n = 10
    return 20 if n >= 20 else 10


def _exit_label(bucket, band, tp, window):
    if isinstance(EXIT_TP.get((bucket, band)), dict):
        if tp is None:
            return f'{bucket} / VIX<20 hold {window}d'
        return f'{bucket} / VIX<20 +{tp}% ({window}d)'
    if band == '30+':
        where = 'VIX 30+'
    elif band == '20-29':
        where = 'VIX 20-29'
    else:
        where = 'VIX<20'
    if tp is None:
        return f'{bucket} / {where} hold'
    return f'{bucket} / {where} +{tp}%'


def standard_take_profit(drop_pct, vix, hold_days=10):
    """Return (tp_pct or None, sl_pct, label).

    tp_pct is None when this cell holds the window. Returns None when
    drop_pct or vix cannot be read. hold_days is the book's max hold.
    Only the 10-15% / VIX-under-20 cell changes with that window.
    """
    try:
        vix_f = float(vix)
        drop_abs = abs(float(drop_pct))
    except (TypeError, ValueError):
        return None
    bucket = None
    for lo, hi, label in DROP_BUCKETS:
        if lo <= drop_abs < hi:
            bucket = label
            break
    if bucket is None:
        return None, STANDARD_SL_PCT, 'no tier — hold'
    band = _vix_band(vix_f)
    spec = EXIT_TP[(bucket, band)]
    window = _window_key(hold_days)
    tp = spec[window] if isinstance(spec, dict) else spec
    return tp, STANDARD_SL_PCT, _exit_label(bucket, band, tp, window)


def numbered_watchlist_blocked(drop_pct, vix):
    """True when the numbered watchlist leaves this name v2-only.

    Same caps as the table used before the replay switch. A 5-6% drop at
    VIX 22 is blocked here. If v2 confirms it, the exit is still +150%.
    VIX at or above 30 is not a block.
    """
    try:
        vix_f = float(vix)
    except (TypeError, ValueError):
        return True
    bucket = drop_bucket(drop_pct)
    if bucket is None:
        return True
    if vix_f >= PANIC_SPIKE_VIX:
        return False
    cap = LEGACY_WATCHLIST_VIX_CAP[bucket]
    if cap is None:
        return False
    return vix_f >= cap


def vix_proxy_from_tier(tier):
    """A VIX inside the band named on a stored tier label, or None."""
    if not tier:
        return None
    text = str(tier).upper().replace(' ', '')
    if '30+' in text or 'PANIC' in text:
        return 35.0
    if 'VIX25-30' in text:
        return 27.0
    if 'VIX20-25' in text:
        return 22.0
    if 'VIXGATE' in text:
        return 22.0
    if 'VIX<20' in text or 'ANYVIX' in text:
        return 15.0
    return None


def resolve_take_profit(drop_pct, vix=None, tier=None, hold_days=10):
    """standard_take_profit using vix, or a VIX read off the tier label."""
    if vix is None or (isinstance(vix, float) and vix != vix):
        vix = vix_proxy_from_tier(tier)
    if vix is None:
        return None
    return standard_take_profit(drop_pct, vix, hold_days=hold_days)
