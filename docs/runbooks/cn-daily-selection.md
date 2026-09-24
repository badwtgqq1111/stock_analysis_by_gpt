# Runbook：每日股价同步 → 用当前策略选出今日股票

> 适用：本机 macOS（launchd）或手工执行。策略侧当前默认 `[selection.startup_gate] mode = "score"`
> （打分制，见 `docs/todo/P1_21_startup_gate_robustness.md`），`rebalance_stride_days = 1`。

## 0. 一键（自动化入口，已带 --force-rebalance）

```bash
cd /Users/ccs/code/quant/stock_analysis_by_gpt
DAILY_NOTIFY=0 bash scripts/run_daily_production.sh --force        # 想发飞书/企微就把 DAILY_NOTIFY 去掉
```

脚本会按顺序跑：`daily_bars → moneyflow → regime → features → clean_panel → model_scores
→ preselection → pk → exits → paper_account → paper_outcomes`，**每个阶段都追加 `--force-rebalance`**，
随后生成两阶段报告并按需推送。

- 阶段耗时参考（2026-09-22 实测）：daily_bars 44min、moneyflow 13min、regime 30s、features 58min、
  clean_panel 14min、model_scores 68s、preselection 28s、pk 33s、exits/paper 各约 20s。
- 日志：`output/pipeline_reports/daily/<YYYY-MM-DD>/<stage>.log` 与 `run.log`。
- 同一天成功后会写 `production.done`，重复触发直接跳过（`--force` 覆盖）。
- 保留旧的"每 5 个交易日才换股"节奏：`DAILY_FORCE_REBALANCE=0 bash scripts/run_daily_production.sh --force`
  并把配置里的 `rebalance_stride_days` 调回 5。

## 1. 手工分步（需要逐步看结果时用）

```bash
cd /Users/ccs/code/quant/stock_analysis_by_gpt
STAGE() { uv run python scripts/run_cn_pipeline.py --stage "$1" --force-rebalance; }

STAGE daily_bars     # 1) 同步今日日线（qfq），落 assets/data/clean/ohlcv
STAGE moneyflow      # 2) 同步今日资金流 + 重算 rolling 尾部（修复冷启动）
STAGE regime         # 3) 市场状态（影响 gross budget / 权重）
STAGE features       # 4) 因子特征（basis: 全市场日线）→ assets/data/feature/features
STAGE clean_panel    # 5) 干净面板（模型输入）→ assets/data/feature/clean_feature_panel
STAGE model_scores   # 6) 用已保存模型打分 → output/model_scores/cn_*_scores.csv
STAGE preselection   # 7) 预选（打分制门禁 + 信号 sleeve）→ output/results_cn/cn_ensemble_preselected.csv
STAGE pk             # 8) 组合优化 → output/results_cn/cn_ensemble_selected.csv
STAGE exits          # 9) 持仓卖出规则 → output/results_cn/cn_exit_plan.{md,csv}
STAGE paper_account  # 10) 模拟账户下单/净值
uv run python scripts/render_two_stage_report.py --trade-date "$(date +%Y-%m-%d)"
# 需要推送再加：
# uv run python scripts/notify_selection.py --trade-date "$(date +%Y-%m-%d)"
```

只重跑选股（数据已经同步过、只想换门禁或改参数）：

```bash
uv run python scripts/run_cn_pipeline.py --stage preselection --force-rebalance
uv run python scripts/run_cn_pipeline.py --stage pk --force-rebalance
# A/B：命令行临时换门禁形态（不用改配置）
#   --gate-mode eligibility | score | thresholds | both   --gate-strength 0.15
uv run python scripts/run_cn_pipeline.py --stage preselection --force-rebalance --gate-mode eligibility
```

## 2. 为什么必须带 `--force-rebalance`

### 2.1 数据要等收盘后源站发布（否则选的是上一交易日的票）

行情源（tencent 主链）在收盘后**要过一段时间**才给出当日日线：实测 2026-09-23 16:40 拉
`600007.SH`，返回区间仍是 `... 至 2026-09-22`；此时 `daily_bars` 仍会成功，但 9/23 只有
约 200 个标的入库（不到横截面的 5%），打分器按 `min_cross_section_coverage = 0.95`
回退到 9/22 → 选出来的还是上一交易日的股票。所以：

```bash
# 判断"今天的数据到底到没到"（看最后一列是不是今天）
python3 -c "import pandas as pd;print(pd.read_csv('output/model_scores/cn_lightgbm_scores.csv').trade_date.max())"
```

- 到点就（重新）跑；未到点就让调度器在 19:30 / 20:30 / 22:00 自动跑（`DAILY_EARLIEST_HOUR`
  默认 16，但真正可用时间取决于源站，本机 launchd 的 19:30 那一档才是"数据一定齐"的档）。
- 需要立刻拿今日票：等源站发布后 `bash scripts/run_daily_production.sh --force`。

### 2.2 stride 早退逻辑

`MarketDataService.select_persisted_model_scores` 里有一段 stride 早退：

```python
if (as_of is None and stride > 1 and not force_rebalance
        and selection_date is not None and state_path.is_file() and selection_path.is_file()):
    ...
    if 0 <= elapsed < stride:
        return {"status": "carried_forward", ...}   # 直接沿用上一版组合
```

即：**实盘（不带 --trade-date）** + `rebalance_stride_days > 1` + 距上次再平衡不足 N 个交易日
→ 直接返回上一版组合，新选出来的票不会进 book。这就是"新股票几天都不变"的原因。
`--trade-date`（回放）会绕过这段判断，所以历史回放看不出这个问题。

2026-09-23 修复：
1. `scripts/run_daily_production.sh` 的每个阶段都追加 `--force-rebalance`
   （`DAILY_FORCE_REBALANCE=0` 可关）；
2. 配置 `rebalance_stride_days = 1`。

**自检**（3 秒）：

```bash
cat output/results_cn/cn_ensemble_rebalance_state.json
# trade_date 必须是今天；若停在旧日期，说明这次是 carried_forward（没带 --force-rebalance）
```

### 2.3 覆盖度读取不能回退到滞后的 Parquet 镜像（2026-09-23 修复）

`features` 阶段先做"特征覆盖预检"：拿每只股票**最新的 OHLCV 日期**去比对已有特征，已覆盖就跳过。
这个最新日期来自 `MarketDataWarehouse.ohlcv_coverage_by_stock`，它原先**只读 Parquet 镜像**，
而镜像比 ClickHouse 主表晚一个交易日（当日 18:00 镜像停在 09-22、主表已有 09-23）→ 预检认为
"最新就是 09-22、特征齐全"→ 5,198 只全部跳过 → 面板/打分/选股全部停在前一交易日。
这是"今天选出来还是昨天的票"的第二个根因（第一个是 §2.2 的 stride）。

修复：
1. `ohlcv_coverage_by_stock` 同时查询两个存储并按股票合并（行数取大、日期取新）；
2. 该聚合**总是**尝试 ClickHouse（分片、参数 ≤500），不受 `_clickhouse_disabled_reason`
   软禁用标志影响 —— 运行中一次批量查询失败会置位该标志，后续覆盖度查询就会静默退化到镜像；
3. `ClickHouseStore.group_count_and_max` 补齐（原先只有 Parquet 实现），带 `IN` 分片。

**自检**（必须显示今天/最近交易日）：

```bash
python3 - <<'EOF'
import tomllib
from data.ingest.service import MarketDataService
svc = MarketDataService(base_dir="./assets/data")
_, latest = svc.warehouse.ohlcv_coverage_by_stock(
    stock_codes=["600007.SH"], market="CN", asset_type="equity", frequency="daily", adjust="qfq")
print({k: str(v) for k, v in latest.items()})
EOF
```

`features` 阶段正常应打印 `需计算 N 只`（N≈全市场，耗时可到 40-60 分钟）；若显示
`可跳过 5198 只, 需计算 24 只` 而且面板/打分日期没变，就是覆盖度又退化到镜像了。

## 3. 完成校验清单

```bash
python3 - <<'PY'
import json, pandas as pd, datetime as dt
today = dt.date.today().isoformat()
sel = pd.read_csv("output/results_cn/cn_ensemble_selected.csv")
state = json.load(open("output/results_cn/cn_ensemble_rebalance_state.json"))
book = sel[sel.get("target_weight", 0) > 0]
print("state.trade_date =", state.get("trade_date"), "| stride =", state.get("rebalance_stride_days"))
print("book:", book[["stock_code", "target_weight"]].to_dict("records"))
assert state.get("trade_date") == today, "book 不是今天选的：检查 --force-rebalance / stride / 数据日期"
PY
```

- 面板/打分日期：`python3 -c "import pandas as pd;print(pd.read_csv('output/model_scores/cn_lightgbm_scores.csv').trade_date.max())"`
  应等于今天（否则是数据没同步到，或 scoring 回退到上一个完整横截面）。
- 资金流覆盖率（打分制依赖它做加分，不依赖它做剔除）：
  `python3 -c "import pandas as pd;d=pd.read_parquet('assets/data/derived/cn_moneyflow_features.parquet',columns=['trade_date','moneyflow_net_z_5d']);print(d.groupby(d.trade_date.dt.date)['moneyflow_net_z_5d'].apply(lambda s: s.notna().mean()).tail(3))"`

## 4. 常见故障与处置

| 现象 | 原因 | 处置 |
|---|---|---|
| `universe filter removed every scored name` | 资金流覆盖 0 且旧配置 `missing_flow="drop"`；或名称表读空 | 已修：覆盖不足自动降级为"保留"、名称表为空显式报错；修数据后重跑 |
| `startup gate removed every candidate` | 硬门禁 + 极端截面（现已默认 score 模式，不会发生） | `--gate-mode score` |
| book 连续多天不变 | 少了 `--force-rebalance` 或 `rebalance_stride_days > 1` | 见 §2 |
| 打分日期不是今天 | 数据未同步（daily_bars/features/clean_panel 未跑或失败） | 按 §1 顺序补跑，先看 `<stage>.log` 末尾 |
| `features`/`clean_panel` 跑到一半失败 | 上游 provider 限流或代理中断 | 直接重跑该阶段（幂等、skip_existing）；必要时 `HTTPS_PROXY` 检查 |
