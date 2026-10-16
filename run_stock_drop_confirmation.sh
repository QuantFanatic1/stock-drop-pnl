#!/bin/bash
# Stock Drop v2 Confirmation Entry — 12:45 PM PST
# Applies the Day-1 confirmation gate + score/floor/cap to the previous
# session's candidate list and outputs the final entries.

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

echo "$(date): Running stock drop v2 confirmation entry..." >> launchd_stdout.log
/usr/bin/python3 stock_drop_confirmation_entry.py 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "$(date): Confirmation entry completed" >> launchd_stdout.log

    TODAY=$(date +%Y-%m-%d)

    # Main book: confirmed entries only (the real v2 monthly book)
    echo "$(date): Adding confirmed entries to main P&L book..." >> launchd_stdout.log
    /usr/bin/python3 stock_drop_pnl_tracker.py --add-from-confirmed "$TODAY" 2>&1

    # Shadow book: every candidate with its v2 decision tag (research)
    echo "$(date): Adding all candidates to shadow P&L book..." >> launchd_stdout.log
    /usr/bin/python3 stock_drop_pnl_tracker.py --book shadow --add-from-decisions "$TODAY" 2>&1

    # 3-month parallel books (same contracts, same 12:45 green gate)
    # Reminders only for the 20d trade path ("3 Month Options"); both books get PnL.
    if [ -f "Stock_Drop_Alert/Stock_Drop_3m_Confirmed_Entries_${TODAY}.csv" ]; then
        echo "$(date): Adding 3m confirmed entries to 3m10 live P&L book..." >> launchd_stdout.log
        /usr/bin/python3 stock_drop_pnl_tracker.py --book 3m10 --add-from-3m-confirmed "$TODAY" 2>&1

        echo "$(date): Adding 3m confirmed entries to 3m20 live P&L book..." >> launchd_stdout.log
        /usr/bin/python3 stock_drop_pnl_tracker.py --book 3m20 --add-from-3m-confirmed "$TODAY" 2>&1
    else
        echo "$(date): No 3m confirmed file for ${TODAY} (none passed gate or no contracts)" >> launchd_stdout.log
    fi
else
    echo "$(date): Confirmation entry failed (exit $EXIT_CODE)" >> launchd_stderr.log
fi
