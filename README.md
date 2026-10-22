# Stock-drop signal and paper P&L tracker

Paper trading only. These scripts do not send orders to a broker.

A stock falls 5% or more. The next question is whether a call or a put is worth tracking, and when the paper option should come off. This repo answers that with a daily signal and three paper books. It does not include the options backtest that produced the rules.

## How the pipeline works

1. `stock_drop_alert.py` finds stocks down at least 5% and writes an alert file.
2. `stock_drop_watchlist.py` turns that file into a candidate list. Direction comes from the stock's own drop history. The contract is a listed option at least 30 calendar days out.
3. On the next session, near 12:45 PM Pacific, `stock_drop_confirmation_entry.py` keeps calls only when the day is green, scores the survivors, and caps the list at 15.
4. `stock_drop_pnl_tracker.py` records those names as paper positions and marks them until the stop, the target, or the hold limit.
5. `stock_drop_3m_pnl_tracker.py` is a separate experiment. It replays the main book's signals on an option about 90 days out.

Each paper position risks about $2,000 of premium. The stop is −40%. A blank target means the book holds until its time stop. `stock_drop_exit_rules.py` is that table. `Stock_Drop_Alert/stock_drop_v2_rules.json` decides which drop-size and VIX cells are allowed in. It does not set the exit.

| Book | What it tracks | Hold |
|---|---|---|
| Main | Monthly option, about 30–45 DTE | 10 trading days |
| Live 3-month, 10-day | Option about 90 DTE, same names as the 20-day book | 10 trading days |
| Live 3-month, 20-day | Same contracts as the 10-day 3-month book | 20 trading days |
| Experiment | Main-book signals mapped onto an option about 90 DTE | 10 or 20 trading days (`--max-hold`) |

The two live 3-month books share contracts. Do not add their P&L together.

## Setup

```bash
python3 -m pip install -r requirements-stock-drop.txt
cp .env.example .env
```

Put `FMP_API_KEY` and `POLYGON_API_KEY` in `.env`. The scripts read those names from the environment. A missing key stops the run and names the variable.

These files ship with the repo. The signal uses them, and a missing file is reported instead of skipped:

- `vix_history.csv` — midday VIX used when a historical alert date is replayed
- `Stock_Drop_Alert/stock_drop_v2_rules.json` — which cells are tradable, plus the score floor and daily cap
- `Stock_Drop_Alert/Stock_Drop_Strategy_Results_2026-10-05.csv` — 7,330 settled option outcomes from a backtest run, used only as each symbol's prior record in the confirmation score. The backtest code that produced the file is not in this repo.

Generated paper books, alert archives, and logs stay out of git. See `.gitignore`.

## Paper results

Filled closed trades only. A row counts as filled when the option traded enough volume to enter. Open trades and no-fills are excluded. A win is an exit P&L above $0. Average return is the mean option-premium percent.

Main book marked through 2026-10-21. The two 3-month books were last marked on 2026-10-21.

| Book | Filled trades | Win rate | Average return | Average P&L |
|---|---:|---:|---:|---:|
| Main, 10-day hold (entries 2026-03-24 to 2026-10-15) | 751 | 35.2% | +7.9% | +$218 |
| 3-month, 10-day hold (entries 2026-07-14 to 2026-10-15) | 197 | 35.0% | −8.3% | −$176 |
| 3-month, 20-day hold (entries 2026-07-14 to 2026-10-15) | 194 | 28.4% | −11.2% | −$382 |

On the main book the median filled trade is the −40% stop. The average is higher because the winners are larger than the losers. These are paper marks, not broker fills.

The filled trades behind this table are in `paper_results_filled.csv` (1,142 rows: 751 + 197 + 194). Count the rows in a book, the share with `exit_pnl` above 0, and the mean of `exit_pnl_pct`. The averages above are that mean, rounded to one decimal for the percent and to the nearest dollar.

## Daily run

From this directory, on a trading day:

```bash
/usr/bin/python3 stock_drop_alert.py
/usr/bin/python3 stock_drop_watchlist.py
./run_stock_drop_confirmation.sh
./run_stock_drop_pnl.sh
```

`run_stock_drop_confirmation.sh` confirms the day and adds rows to the main book and both live 3-month books. `run_stock_drop_pnl.sh` marks those books and updates the 20-day experiment.

`python3 stock_drop_pnl_tracker.py --summary` reprints a book without fetching new prices.

## Weekly publish

Every Friday at 14:00 local — one hour after the 13:00 Pacific close — `run_stock_drop_weekly_publish.sh` re-marks the three live books with closing prices, rebuilds both published CSVs, and pushes them to this repo.

- `paper_results_filled.csv` — every filled, closed trade (regenerated from the books).
- `paper_results_summary.csv` — one row per book plus a `TOTAL` row: open positions, closed trades, win rate, realized P&L, unrealized P&L, total P&L, money invested in open positions, and total invested to date.

The `TOTAL` row adds all three books, so it double-counts the two 3-month books, which hold the same contracts. Read the per-book rows for the clean view.

`python3 publish_paper_results.py` rebuilds both CSVs without committing; `--commit` stages exactly those two files, commits, and pushes.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

GitHub Actions runs that command on every push and pull request. The tests cover the exit table and the market-holiday calendar.

## License

MIT. See `LICENSE`.
