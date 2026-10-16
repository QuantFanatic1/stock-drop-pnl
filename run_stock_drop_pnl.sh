#!/bin/bash
# Stock Drop P&L Tracker — Daily 10 AM update
# Fetches entry prices for new positions, updates open positions, checks TP/SL

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

echo "$(date): Running P&L tracker update..." >> launchd_stdout.log
# 1) Main monthly book (~30-45 DTE, 10d hold, 12:45 green gate)
/usr/bin/python3 stock_drop_pnl_tracker.py 2>&1
MAIN_EXIT=$?

# 2) Live 3-month book (~90 DTE, 10d hold, same gate)
/usr/bin/python3 stock_drop_pnl_tracker.py --book 3m10 2>&1
M10_EXIT=$?

# 3) Live 3-month book (~90 DTE, 20d hold, same gate)
/usr/bin/python3 stock_drop_pnl_tracker.py --book 3m20 2>&1
M20_EXIT=$?

# 4) Experiment rebuild book: main-book signals remapped to ~90 DTE, 20d hold
#    (stock_drop_3m_20d_pnl.*) — keep open positions current so max-hold closes fire
/usr/bin/python3 stock_drop_3m_pnl_tracker.py --update --max-hold 20 2>&1
EXP20_EXIT=$?

# Non-zero if any book failed
EXIT_CODE=0
if [ $MAIN_EXIT -ne 0 ] || [ $M10_EXIT -ne 0 ] || [ $M20_EXIT -ne 0 ] || [ $EXP20_EXIT -ne 0 ]; then
    EXIT_CODE=1
fi

if [ $EXIT_CODE -eq 0 ]; then
    echo "$(date): P&L tracker updated successfully" >> launchd_stdout.log
else
    echo "$(date): P&L tracker failed (exit $EXIT_CODE; main=$MAIN_EXIT 3m10=$M10_EXIT 3m20=$M20_EXIT exp20=$EXP20_EXIT)" >> launchd_stderr.log
fi
