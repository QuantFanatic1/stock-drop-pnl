#!/usr/bin/env python3
"""Regenerate the published paper-trade CSVs from the live P&L books.

Outputs (repo root):
  paper_results_filled.csv  — one row per filled, closed trade across the
                              three live books (matches the README table)
  paper_results_summary.csv — per-book + TOTAL snapshot: realized P&L,
                              unrealized P&L, total P&L, money invested

A trade counts as filled only when entry_price was recorded (low-volume /
no-fill candidates never invested money and are excluded). Open positions
are excluded from the filled CSV but drive the unrealized and
money-invested-open columns of the summary.

With --commit, stages exactly these two files, commits, and pushes to
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

FILLED_CSV = os.path.join(REPO, 'paper_results_filled.csv')
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


def write_filled_csv():
    rows = []
    for label, path in BOOKS:
        closed, _, _ = load_book(path)
        for p in closed:
            rows.append({
                'book': label,
                'symbol': p.get('symbol'),
                'entry_date': p.get('entry_date'),
                'exit_date': p.get('exit_date'),
                'status': p.get('status'),
                'exit_pnl': p.get('exit_pnl'),
                'exit_pnl_pct': p.get('exit_pnl_pct'),
            })
    rows.sort(key=lambda r: (r['book'], r['entry_date'] or '', r['symbol']))
    cols = ['book', 'symbol', 'entry_date', 'exit_date', 'status',
            'exit_pnl', 'exit_pnl_pct']
    with open(FILLED_CSV, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return rows


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
    staged = git('add', '--', os.path.basename(FILLED_CSV),
                 os.path.basename(SUMMARY_CSV))
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
                        help='Commit the two CSVs and push to origin/main.')
    args = parser.parse_args()

    filled = write_filled_csv()
    print(f'paper_results_filled.csv: {len(filled)} closed trades')

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
