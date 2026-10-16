"""
Dynamic US stock market (NYSE/NASDAQ) holiday generator.

Computes market holidays for any year using official rules, so scripts
never need hardcoded holiday lists that go stale.

Usage:
    from market_holidays import get_market_holidays, is_market_holiday

    holidays = get_market_holidays(2026)          # set of "YYYY-MM-DD" strings
    is_market_holiday("2026-12-25")               # True
"""

from datetime import date, timedelta


def _observed(d):
    """If a fixed holiday falls on a weekend, return the observed weekday."""
    if d.weekday() == 5:      # Saturday -> preceding Friday
        return d - timedelta(days=1)
    elif d.weekday() == 6:    # Sunday -> following Monday
        return d + timedelta(days=1)
    return d


def _nth_weekday(year, month, weekday, n):
    """Return the nth occurrence of `weekday` (Mon=0) in the given month."""
    first = date(year, month, 1)
    day = 1 + (weekday - first.weekday()) % 7 + (n - 1) * 7
    return date(year, month, day)


def _last_weekday(year, month, weekday):
    """Return the last occurrence of `weekday` (Mon=0) in the given month."""
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    days_back = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=days_back)


def _easter(year):
    """Compute Easter Sunday via the Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month, day = divmod(h + el - 7 * m + 114, 31)
    return date(year, month, day + 1)


def get_market_holidays(year):
    """
    Return NYSE/NASDAQ market holidays for a given year as a set of
    "YYYY-MM-DD" strings.

    Covers: New Year's Day, MLK Day, Presidents Day, Good Friday,
    Memorial Day, Juneteenth, Independence Day, Labor Day,
    Thanksgiving, Christmas.
    """
    holidays = set()

    holidays.add(_observed(date(year, 1, 1)))                   # New Year's Day
    holidays.add(_nth_weekday(year, 1, 0, 3))                   # MLK Day (3rd Mon Jan)
    holidays.add(_nth_weekday(year, 2, 0, 3))                   # Presidents Day (3rd Mon Feb)
    holidays.add(_easter(year) - timedelta(days=2))             # Good Friday
    holidays.add(_last_weekday(year, 5, 0))                     # Memorial Day (last Mon May)
    holidays.add(_observed(date(year, 6, 19)))                  # Juneteenth
    holidays.add(_observed(date(year, 7, 4)))                   # Independence Day
    holidays.add(_nth_weekday(year, 9, 0, 1))                   # Labor Day (1st Mon Sep)
    holidays.add(_nth_weekday(year, 11, 3, 4))                  # Thanksgiving (4th Thu Nov)
    holidays.add(_observed(date(year, 12, 25)))                 # Christmas

    return {d.strftime("%Y-%m-%d") for d in holidays}


def get_market_holidays_range(start_year=None, end_year=None):
    """Generate holidays spanning multiple years.

    Defaults to current year - 2 through current year + 1.
    """
    if start_year is None:
        start_year = date.today().year - 2
    if end_year is None:
        end_year = date.today().year + 1

    all_holidays = set()
    for y in range(start_year, end_year + 1):
        all_holidays |= get_market_holidays(y)
    return all_holidays


def is_market_holiday(date_str):
    """Check if a YYYY-MM-DD string is a US market holiday."""
    year = int(date_str[:4])
    return date_str in get_market_holidays(year)
