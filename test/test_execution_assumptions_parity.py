"""统一执行假设（第三十二轮）：模拟盘与日频回放必须读同一份假设、行为一致。

背景：两套引擎此前各写各的成本/滑点/涨跌停/停牌常数，净值不可比。现在唯一来源是
`config/execution_assumptions_cn.json`；这个文件测试两件事：
  1. 两边的**实际生效值**一致（静态 + 行为探针，见 scripts/execution_parity_check.py）；
  2. 换一份假设 JSON，两边都跟着变（防止哪一边偷偷用了局部默认值）。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factor_engine.portfolio.execution_assumptions import cost_model, load_assumptions  # noqa: E402
from factor_engine.portfolio.paper_account import run_paper_account  # noqa: E402


def _replay_module():
    spec = importlib.util.spec_from_file_location("replay_daily_execution", ROOT / "scripts/replay_daily_execution.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_execution_parity_check_reports_no_difference(tmp_path):
    out = tmp_path / "parity.json"
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/execution_parity_check.py"), "--json", str(out)],
                          capture_output=True, text=True, cwd=ROOT, timeout=600)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["differ"] == []
    assert any(r["status"] == "DIFFERS_BY_DESIGN" for r in payload["rows"])       # 成交参考价是已知设计差异


def test_shared_assumptions_file_drives_both_engines():
    cfg = load_assumptions()
    costs = cost_model(cfg)
    replay = _replay_module()
    assert round(replay.COMMISSION * 10_000, 6) == costs["commission_bps"]
    assert round(replay.SLIPPAGE * 10_000, 6) == costs["slippage_bps"]
    assert round(replay.STAMP * 10_000, 6) == costs["stamp_tax_bps_sell"]
    assert replay.LOT == int(cfg["lot_size"])


def test_assumptions_argument_overrides_costs_for_paper_account(tmp_path):
    """显式传一份假设 → 模拟盘按它算（证明成本不是硬编码）。"""
    custom = json.loads((ROOT / "config/execution_assumptions_cn.json").read_text(encoding="utf-8"))
    custom["costs"]["commission_bps"] = 50.0
    custom["costs"]["slippage_bps"] = 0.0
    path = tmp_path / "custom.json"
    path.write_text(json.dumps(custom, ensure_ascii=False), encoding="utf-8")
    dates = pd.date_range("2025-01-01", periods=3, freq="D")
    bars = pd.DataFrame({"stock_code": ["A"] * 3, "trade_date": dates, "open": [10.0, 10.0, 10.0],
                         "close": [10.0, 10.0, 10.0]})
    selection = pd.DataFrame({"stock_code": ["A"], "trade_date": [dates[0]], "target_weight": [1.0]})
    acc = run_paper_account(selection, bars, initial_capital=10_000, assumptions=str(path))
    fill = acc["fills"].iloc[0]
    assert abs(float(fill["commission"]) - float(fill["quantity"]) * float(fill["price"]) * 0.005) < 1e-6
    acc_std = run_paper_account(selection, bars, initial_capital=10_000)
    assert float(acc_std["fills"].iloc[0]["commission"]) < float(fill["commission"])


def test_sell_pays_stamp_tax_but_buy_does_not():
    cfg = load_assumptions()
    costs = cost_model(cfg)
    dates = pd.date_range("2025-01-01", periods=4, freq="D")
    bars = pd.DataFrame({"stock_code": ["A"] * 4, "trade_date": dates,
                         "open": [10.0, 10.0, 10.0, 10.0], "close": [10.0, 10.0, 10.0, 10.0]})
    selection = pd.DataFrame({"stock_code": ["A", "A"], "trade_date": [dates[0], dates[1]],
                              "target_weight": [1.0, 0.0]})
    acc = run_paper_account(selection, bars, initial_capital=10_000)
    fills = acc["fills"].set_index("side")
    buy_value = float(fills.loc["buy", "quantity"]) * float(fills.loc["buy", "price"])
    sell_value = float(fills.loc["sell", "quantity"]) * float(fills.loc["sell", "price"])
    assert abs(float(fills.loc["sell", "commission"]) - sell_value * (costs["commission"] + costs["stamp_tax_sell"])) < 1e-6
    assert abs(float(fills.loc["buy", "commission"]) - buy_value * costs["commission"]) < 1e-6
    assert costs["stamp_tax_sell"] > 0
