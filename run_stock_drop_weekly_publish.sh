#!/bin/bash
# Stock Drop Weekly Publish — Friday after close (14:00 local)
# Re-marks the three live books with post-close prices, rebuilds the
# published CSVs (paper_results_filled.csv + paper_results_summary.csv),
# and pushes them to the stock-drop-pnl repo.

cd "$(dirname "$0")"

# Optional user-site packages. $HOME stays local and is not a checked-in path.
USER_PY_BIN="${HOME}/Library/Python/3.9/bin"
USER_PY_SITE="${HOME}/Library/Python/3.9/lib/python/site-packages"
if [ -d "$USER_PY_BIN" ]; then
    export PATH="${USER_PY_BIN}:${PATH}"
fi
if [ -d "$USER_PY_SITE" ]; then
    export PYTHONPATH="${USER_PY_SITE}${PYTHONPATH:+:${PYTHONPATH}}"
fi

echo "$(date): Weekly paper-book publish starting..." >> launchd_stdout.log

# Refresh marks only on a trading day; publish regardless so the repo
# always gets the books as they currently stand.
if /usr/bin/python3 - <<'PY'
import sys, datetime, market_holidays as mh
d = datetime.date.today()
sys.exit(0 if d.weekday() < 5 and not mh.is_market_holiday(d.isoformat()) else 1)
PY
then
    /usr/bin/python3 stock_drop_pnl_tracker.py 2>&1
    /usr/bin/python3 stock_drop_pnl_tracker.py --book 3m10 2>&1
    /usr/bin/python3 stock_drop_pnl_tracker.py --book 3m20 2>&1
else
    echo "$(date): Not a trading day — publishing existing marks" >> launchd_stdout.log
fi

/usr/bin/python3 publish_paper_results.py --commit 2>&1
PUBLISH_EXIT=$?

if [ $PUBLISH_EXIT -eq 0 ]; then
    echo "$(date): Weekly paper-book publish completed" >> launchd_stdout.log
else
    echo "$(date): Weekly paper-book publish FAILED (exit $PUBLISH_EXIT)" >> launchd_stderr.log
fi
exit $PUBLISH_EXIT
