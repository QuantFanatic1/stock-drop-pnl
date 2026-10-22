#!/usr/bin/env python3
"""Regenerate the published paper-trade CSVs from the live P&L books.

Outputs (repo root):
  paper_results_filled_<book>.csv — the filled, closed trades of each live
                              book in its OWN file (main_10d, 3m_10d,
                              3m_20d); the books are never combined into
                              one table
  paper_results_summary.csv — per-book + TOTAL snapshot: realized P&L,
                              unrealized P&L, total P&L, money invested

A trade counts as filled only when entry_price was recorded (low-volume /
no-fill candidates never invested money and are excluded). Open positions
are excluded from the filled CSVs but drive the unrealized and
money-invested-open columns of the summary.

With --commit, stages exactly these output files, commits, and pushes to
origin/main. It never runs `git add .` — the rest of this directory is
not for git (see .gitignore).
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime

REPO = os.path.dirname(os.path.abspath(__file__))
ALERT_DIR = os.path.join(REPO, 'Stock_Drop_Alert')

# (book label in the CSVs, live book JSON) — the experiment rebuild book is
# not part of the published set.
BOOKS = [
    ('main_10d', os.path.join(ALERT_DIR, 'stock_drop_pnl.json')),
    ('3m_10d', os.path.join(ALERT_DIR, 'stock_drop_3m10_live_pnl.json')),
    ('3m_20d', os.path.join(ALERT_DIR, 'stock_drop_3m20_live_pnl.json')),
]

FILLED_FILES = {
    'main_10d': 'paper_results_filled_main_10d.csv',
    '3m_10d': 'paper_results_filled_3m_10d.csv',
    '3m_20d': 'paper_results_filled_3m_20d.csv',
}
# The old all-books combined file; removed in favor of the per-book files.
RETIRED_FILLED = 'paper_results_filled.csv'
SUMMARY_CSV = os.path.join(REPO, 'paper_results_summary.csv')


def load_book(path):
    with open(path) as f:
        data = json.load(f)
    positions = data.get('positions', [])
    # A filled trade has a recorded entry price; low-volume / no-fill
    # candidates never invested money. Some legacy d10_close rows have an
    # empty exit_date, so classify by status, not by exit_date presence.
    filled = [p for p in positions if p.get('entry_price') is not None]
    open_filled = [p for p in filled if p.get('status') == 'open']
    closed = [p for p in filled if p.get('status') != 'open']
    return closed, open_filled, data.get('last_update', '')


def write_filled_csvs():
    """One CSV per book — the books are never combined into one table."""
    cols = ['symbol', 'entry_date', 'exit_date', 'status',
            'exit_pnl', 'exit_pnl_pct']
    counts = {}
    for label, path in BOOKS:
        closed, _, _ = load_book(path)
        rows = sorted(closed, key=lambda p: (p.get('entry_date') or '',
                                             p.get('symbol') or ''))
        out = os.path.join(REPO, FILLED_FILES[label])
        with open(out, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows([{c: p.get(c) for c in cols} for p in rows])
        counts[label] = len(rows)
    return counts


def summarize_book(closed, open_pos):
    realized = sum(p.get('exit_pnl') or 0 for p in closed)
    unrealized = sum(p.get('current_pnl') or 0 for p in open_pos)
    wins = sum(1 for p in closed if (p.get('exit_pnl') or 0) > 0)
    return {
        'open_positions': len(open_pos),
        'closed_trades': len(closed),
        'win_rate_pct': round(100.0 * wins / len(closed), 1) if closed else 0.0,
        'realized_pnl': round(realized, 2),
        'unrealized_pnl': round(unrealized, 2),
        'total_pnl': round(realized + unrealized, 2),
        # Capital currently at risk in open positions
        'money_invested_open': round(sum(p.get('entry_amount') or 0
                                         for p in open_pos), 2),
        # Cumulative premium deployed across every filled entry (closed + open)
        'total_invested_to_date': round(sum(p.get('entry_amount') or 0
                                            for p in closed + open_pos), 2),
    }


def write_summary_csv():
    as_of = ''
    out_rows = []
    totals = {k: 0.0 for k in ('open_positions', 'closed_trades',
                               'realized_pnl', 'unrealized_pnl',
                               'total_pnl', 'money_invested_open',
                               'total_invested_to_date')}
    wins_total = 0
    for label, path in BOOKS:
        closed, open_pos, last_update = load_book(path)
        as_of = max(as_of, str(last_update))
        s = summarize_book(closed, open_pos)
        out_rows.append(dict(as_of=str(last_update), book=label, **s))
        for k, v in s.items():
            if k in totals:
                totals[k] += v
        wins_total += sum(1 for p in closed if (p.get('exit_pnl') or 0) > 0)
    totals['win_rate_pct'] = (round(100.0 * wins_total / totals['closed_trades'], 1)
                              if totals['closed_trades'] else 0.0)
    out_rows.append(dict(as_of=as_of, book='TOTAL', **{
        k: (int(v) if k in ('open_positions', 'closed_trades')
            else round(v, 2))
        for k, v in totals.items()}))

    cols = ['as_of', 'book', 'open_positions', 'closed_trades', 'win_rate_pct',
            'realized_pnl', 'unrealized_pnl', 'total_pnl',
            'money_invested_open', 'total_invested_to_date']
    with open(SUMMARY_CSV, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows)
    return out_rows


def git(*args):
    return subprocess.run(['git', '-C', REPO, *args], capture_output=True,
                          text=True)


def commit_and_push():
    staged_names = [FILLED_FILES[label] for label, _ in BOOKS]
    staged_names.append(os.path.basename(SUMMARY_CSV))
    # One-time transition: remove the old combined file from the repo.
    retired_path = os.path.join(REPO, RETIRED_FILLED)
    if os.path.exists(retired_path):
        os.remove(retired_path)
        staged_names.append(RETIRED_FILLED)
    staged = git('add', '--', *staged_names)
    if staged.returncode != 0:
        print('git add failed:', staged.stderr.strip())
        return 1
    if git('diff', '--cached', '--quiet').returncode == 0:
        print('No changes to publish — nothing to commit.')
        return 0
    msg = 'Weekly paper book publish: ' + datetime.now().strftime('%Y-%m-%d')
    committed = git('commit', '-m', msg)
    if committed.returncode != 0:
        print('git commit failed:', committed.stderr.strip())
        return 1
    pushed = git('push', 'origin', 'main')
    if pushed.returncode != 0:
        print('git push failed:', pushed.stderr.strip())
        return 1
    print('Committed and pushed:', msg)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='Rebuild the published paper-trade CSVs from the live books.')
    parser.add_argument('--commit', action='store_true',
                        help='Commit the output CSVs and push to origin/main.')
    args = parser.parse_args()

    counts = write_filled_csvs()
    for label, _ in BOOKS:
        print(f'{FILLED_FILES[label]}: {counts[label]} closed trades')

    summary = write_summary_csv()
    for row in summary:
        print('{book:>9}  closed={closed_trades:>4}  open={open_positions:>3}  '
              'realized={realized_pnl:>12,.2f}  unrealized={unrealized_pnl:>10,.2f}  '
              'total={total_pnl:>12,.2f}  invested_open={money_invested_open:>10,.2f}'
              .format(**row))

    if args.commit:
        sys.exit(commit_and_push())


if __name__ == '__main__':
    main()
