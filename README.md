# Stock drop P&L

Tracks option positions after a 5%+ stock drop. One confirmation pass writes the day's entries. A later pass marks every open book and closes trades that hit the stop, the take-profit, or the hold limit.

Keys live in `.env` (see `.env.example`). The scripts read `FMP_API_KEY` and `POLYGON_API_KEY` from the environment. Nothing in this set hardcodes a key.

## Books

| Book | File | How it is filled | Hold |
|---|---|---|---|
| Main monthly, about 30–45 DTE | `Stock_Drop_Alert/stock_drop_pnl.json` | `stock_drop_pnl_tracker.py --add-from-confirmed DATE` | 10 trading days |
| Live 3-month, about 90 DTE | `stock_drop_3m10_live_pnl.json` | `--book 3m10 --add-from-3m-confirmed DATE` | 10 trading days |
| Live 3-month, about 90 DTE | `stock_drop_3m20_live_pnl.json` | `--book 3m20 --add-from-3m-confirmed DATE` | 20 trading days |
| Experiment, main signals on a ~90 DTE contract | `stock_drop_3m_pnl.json` | `stock_drop_3m_pnl_tracker.py` | 10 trading days |
| Same experiment, longer hold | `stock_drop_3m_20d_pnl.json` | `stock_drop_3m_pnl_tracker.py --max-hold 20` | 20 trading days |

The two live 3-month books share contracts. Do not add their P&L together.

Generated JSON and text books are gitignored. So are scan output, caches, logs, and `.env`.

## Daily run

From this directory:

```bash
# 1. Find today's 5%+ drops.
/usr/bin/python3 stock_drop_alert.py

# 2. Build the next-session candidate list from the latest alert.
/usr/bin/python3 stock_drop_watchlist.py

# 3. On the next session, near 12:45 local, confirm entries and add them
#    to the main book and both live 3-month books.
./run_stock_drop_confirmation.sh

# 4. Later, mark open positions on every book, including the 20-day experiment.
./run_stock_drop_pnl.sh
```

`run_stock_drop_confirmation.sh` runs `stock_drop_confirmation_entry.py`, then adds confirmed rows with `stock_drop_pnl_tracker.py`. `run_stock_drop_pnl.sh` updates the main book, `--book 3m10`, `--book 3m20`, and `stock_drop_3m_pnl_tracker.py --update --max-hold 20`.

Exit prices come from `stock_drop_exit_rules.py`. The stop is −40%. A blank take-profit holds to that book's time stop. `Stock_Drop_Alert/stock_drop_v2_rules.json` is the confirmation gate (which drop-size and VIX cells are tradable, plus the score floor and daily cap). It does not set the live exit.

## What stays off git

P&L books, alert archives, candidate CSVs, `.pit_cache`, logs, `*.plist` (those contain a machine path), and `.env`.
