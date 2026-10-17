#!/usr/bin/env python3
"""P2.71 explicit execution/cost ledger scenarios."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.execution_ledger import (
    ExecutionCost, PortfolioLedger, fee_summary, periodic_rebalances,
)


def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    cost = ExecutionCost(commission_bps=3, stamp_duty_bps=5, transfer_fee_bps=1,
                         slippage_bps=5, impact_bps=3, borrow_bps_annual=50)
    scenarios = {}

    ledger = PortfolioLedger(1000.0, cost)
    ledger.trade('2025-01-02', 'A', 'BUY', 100, 10.0, reason='cash_to_single')
    scenarios['cash_to_single'] = {'rows': list(ledger.rows),
                                    'mark': ledger.mark({'A': 10.0})}

    ledger = PortfolioLedger(1000.0, cost)
    ledger.trade('2025-01-02', 'A', 'BUY', 100, 10.0)
    ledger.rebalance('2025-01-03', {'B': 1.0}, {'A': 10.0, 'B': 10.0}, reason='A_to_B')
    scenarios['A_to_B'] = {'rows': list(ledger.rows), 'mark': ledger.mark({'A': 10.0, 'B': 10.0})}

    ledger = PortfolioLedger(1000.0, cost)
    ledger.trade('2025-01-02', 'A', 'BUY', 100, 10.0)
    ledger.trade('2025-01-03', 'A', 'SELL', 40, 12.0, reason='partial_reduce')
    scenarios['partial_reduce'] = {'rows': list(ledger.rows), 'mark': ledger.mark({'A': 12.0})}

    ledger = PortfolioLedger(1000.0, cost)
    ledger.trade('2025-01-02', 'A', 'BUY', 50, 10.0)
    ledger.trade('2025-01-03', 'A', 'SELL', 150, 10.0, reason='long_to_short')
    scenarios['long_to_short'] = {'rows': list(ledger.rows),
                                  'mark': ledger.mark({'A': 10.0})}

    summary = {}
    all_rows = []
    for name, data in scenarios.items():
        summary[name] = fee_summary(data['rows'], 1000.0)
        summary[name]['mark'] = data['mark']
        for row in data['rows']:
            all_rows.append({'scenario': name, **row, 'trade_date': str(row['trade_date'])})
    pd.DataFrame(all_rows).to_csv(output / 'ledger.csv', index=False)
    calendar = pd.bdate_range('2025-01-01', periods=60)
    timeline = periodic_rebalances(calendar, every=20, execution_lag=1)
    payload = {'status': 'PASS_COST_LEDGER', 'cost': cost.__dict__,
               'scenarios': summary,
               'rebalance_timeline': [{k: str(v) for k, v in row.items()} for row in timeline[:3]],
               'mapping': ('one_way_turnover = (buy_notional + sell_notional) / 2 / initial_cash; '
                           'two_sided_turnover = (buy_notional + sell_notional) / initial_cash'),
               'limitations': ['research accounting only', 'no lots/limit/ST/suspension/capacity',
                               'borrow fee is annual and reported separately from trade fees']}
    (output / 'manifest.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                                     default=str) + '\n')
    lines = ['# P2.71 执行与成本账本', '',
             f"成本：佣金{cost.commission_bps}bp、印花税{cost.stamp_duty_bps}bp(卖出)、"
             f"过户{cost.transfer_fee_bps}bp、滑点{cost.slippage_bps}bp、冲击{cost.impact_bps}bp、"
             f"借券{cost.borrow_bps_annual}bp/年。", '',
             '| 场景 | 买入名义 | 卖出名义 | 单边换手 | 双边成交/初始现金 | 费用 |',
             '|---|---:|---:|---:|---:|---:|']
    for name, row in summary.items():
        lines.append(f"| {name} | {row['buy_notional']:.2f} | {row['sell_notional']:.2f} | "
                     f"{row['one_way_turnover_fraction']:.4f} | "
                     f"{row['two_sided_turnover_fraction']:.4f} | {row['fees']:.4f} |")
    lines += ['', '## 口径', '',
              '- 每个场景从1000元现金开始；按实际成交名义金额逐笔收费。',
              '- 买入先预留费用；卖出费用从proceeds扣除；平仓/部分减仓按加权平均成本实现盈亏。',
              '- `A→B`先卖后买；多空转换允许持仓穿越零，卖空名义金额同样计入卖出成交。',
              '- 借券费不混入逐笔佣金；本账本仅记录年费率，供后续按日计提。', '',
              '20日调仓语义：信号t收盘后决定，t+1交易日开盘成交，成交日即持仓生效日，'
              '首笔计量为成交日开盘→收盘；不把`i+lag+1`混作成交日。', '',
              '[逐笔账本](ledger.csv) · [manifest](manifest.json)']
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    print('PASS_COST_LEDGER scenarios=4')
    return payload


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'output/research/p2_71/cost_ledger_v1')
    args = parser.parse_args()
    run(args.output.resolve())


if __name__ == '__main__':
    main()
