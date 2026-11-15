"""Backtest Strategy B v2.1 (settled-cash, core-only VOO/SMH, $200 daily buy cap) from local CSVs."""
from argparse import ArgumentParser
from pathlib import Path
from unittest.mock import patch
import json

from strategy_b.backtest import bars, metrics, run
from strategy_b.data import load
from strategy_b.paper import Wallet
from strategy_b.planner import CORE_CASH, Costs
from strategy_b.policy import D, load_policy


class SettledCashWallet(Wallet):
    def release(self, session):
        super().release(session)
        for source in self.pockets:
            if source != CORE_CASH:
                self.pockets[CORE_CASH] += self.pockets[source]
                self.pockets[source] = D(0)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True, type=Path)
    parser.add_argument('--start', default='2013-01-02')
    parser.add_argument('--end', default='2026-11-11')
    parser.add_argument('--seed', default='10000', help='initial cash on the start session')
    parser.add_argument('--monthly', default='400', help='deposit on the first session of each month')
    parser.add_argument('--weekly', default=None, help='deposit on the first session of each week (replaces --monthly)')
    parser.add_argument('--daily-limit', default='200', help="combined VOO+SMH buy cap per session; 'none' to disable")
    args = parser.parse_args()
    frames, rates, hashes = load(args.data)
    history = bars(frames, rates)
    if args.start not in history or args.end not in history or args.start >= args.end:
        parser.error('Choose two distinct trading sessions in the input data.')
    with patch('strategy_b.backtest.Wallet', SettledCashWallet):
        rows, wallet = run(history, frames['QQQ']['adjusted'], load_policy(), Costs(),
                           start=args.start, end=args.end, variant='B_CORE', seed=D(args.seed),
                           monthly=D(args.monthly), weekly=D(args.weekly) if args.weekly else None,
                           daily_limit=None if args.daily_limit.lower()=='none' else D(args.daily_limit))
    print(json.dumps(dict(results=metrics(rows, wallet), inputs=hashes), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
