#!/usr/bin/env python3
"""Backtest frozen AlphaQCM factors against CSI500 on the research panel."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.alphaqcm_portfolio import PortfolioConfig, run_from_run
from scripts.p2_71_alphaqcm_pilot import select_panel


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, required=True,
                        help='AlphaQCM run directory containing factors.json')
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'output/research/p2_71/alphaqcm_portfolio_v1')
    parser.add_argument('--start', default='2023-01-01')
    parser.add_argument('--end', default='2025-10-31')
    parser.add_argument('--assets', type=int, default=60)
    parser.add_argument('--top-n', type=int, default=20)
    parser.add_argument('--rebalance', type=int, default=20)
    parser.add_argument('--cost-bps', type=float, default=15.5)
    parser.add_argument('--fundamentals', action='store_true')
    args = parser.parse_args()
    panel = select_panel(args.assets, with_fundamentals=args.fundamentals)
    result = run_from_run(
        args.run.resolve(), panel, start=args.start, end=args.end,
        output=args.output.resolve(),
        config=PortfolioConfig(top_n=args.top_n, rebalance_every=args.rebalance,
                               cost_one_way=args.cost_bps / 1e4))
    print(result)


if __name__ == '__main__':
    main()
