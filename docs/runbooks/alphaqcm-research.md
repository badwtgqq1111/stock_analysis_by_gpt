# Runbook：P2.71 AlphaQCM 因子挖掘

关联：`docs/todo/P2_71_alphaqcm_factor_mining_research.md`、`backend/services/alphaqcm_adapter.py`、`scripts/p2_71_alphaqcm_*.py`。

本页只讲**怎么跑、产物在哪、怎么读、怎么续跑**。研究结论和预注册边界见 P2.71 计划文档及各 run 的 `REPORT.md`。
本文档按 2026-10-17 的脚本状态编写；后台任务（如 equal-evals v2 / PIT fetch）的实时进度以日志和 `ps` 为准。

## 1. 这套东西是什么

AlphaQCM 是独立研究链路，不是旧 LightGBM / preselect / PK / 条件风控的补丁：

```text
train + valid 面板
        │
        ▼
AlphaQCM worker（子进程，只有 train/valid）
        │  产出 factors.json + artifact hash
        ▼
frozen artifact
        │
        ▼
独立 evaluator（子进程，才读取 test）
        │
        ▼
test_eval/metrics.json
```

关键约束：

- 训练进程**看不到 test**；test 只在 artifact hash 冻结后由独立进程读取。
- 选择只用 valid IC；官方 `test_best_table` 路径已禁用。
- 5k / 20k / 60k / 2M 步是预算实验；只有状态为 `COMPLETED` 或预注册的 `STOPPED_*_BUDGET`，且 counters 达到预算，才算完成对应预算。
- partial / stopped run 只按 partial 归档，不外推成 2M 结果。

## 2. 快速开始

在项目根目录执行。当前工作区优先使用已有 `.venv`，避免在后台任务运行时改环境：

```bash
cd /Users/ccs/code/quant/stock_analysis_by_gpt
source .venv/bin/activate
python -c "import torch, pandas, gymnasium, tensorboard; print('alphaqcm deps ok')"
```

下文 `python` 均指 `.venv/bin/python`；若没有激活虚拟环境，把命令里的 `python` 换成 `.venv/bin/python`。

若缺依赖：

```bash
uv pip install --python .venv/bin/python gymnasium tensorboard
```

后台 worker 正在跑时，不要同时 `uv sync`、重建 `.venv` 或升级 torch；worker 进程会继续持有旧解释器，但新进程会读到不同环境。

最小冒烟（20只股票、2000步、CPU，数十秒）：

```bash
python scripts/p2_71_alphaqcm_pilot.py \
  --steps 2000 --seeds 0 --assets 20 \
  --output output/research/p2_71/alphaqcm_smoke
```

看结果：

```bash
cat output/research/p2_71/alphaqcm_smoke/pilot_runs.csv
cat output/research/p2_71/alphaqcm_smoke/manifest.json
```

标准 20k pilot（60只股票、seed 0/1/2）：

```bash
python scripts/p2_71_alphaqcm_pilot.py \
  --steps 20000 --seeds 0,1,2 --std-lams 1.0 \
  --output output/research/p2_71/alphaqcm_pilot_v1

python scripts/p2_71_alphaqcm_pilot.py \
  --steps 20000 --seeds 0,1,2 --std-lams 0 \
  --output output/research/p2_71/alphaqcm_pilot_v1

python scripts/p2_71_alphaqcm_pilot.py \
  --steps 20000 --seeds 0,1,2 --random \
  --output output/research/p2_71/alphaqcm_pilot_v1

python scripts/p2_71_alphaqcm_summary.py \
  output/research/p2_71/alphaqcm_pilot_v1
```

`--std-lams 1.0` 是 QCM 主组，`--std-lams 0` 是探索奖励消融，`--random` 是几乎全程随机动作的搜索基线。三组必须固定同一份数据切分和同一评测器。

带 PIT 财务/行业相对估值特征的 pilot（当前脚本默认已启用；`--no-fundamentals` 关闭）：

```bash
python scripts/p2_71_alphaqcm_pilot.py \
  --steps 10000 --seeds 0 --assets 60 --fundamentals \
  --output output/research/p2_71/alphaqcm_fin_v1
```

默认会给面板追加：

```text
roe, roa, grossprofit_margin, netprofit_margin, debt_to_assets,
netprofit_yoy, or_yoy, ocfps,
pe_ind_pct, pb_ind_pct, size_ind_pct
```

财务字段按 `ann_date` 做 PIT as-of join（不是按 `end_date`）；估值分位按当日行业相对面板合并。缺失保留 NaN，不填 0。

冻结 run 的中证500组合回测：

```bash
python scripts/p2_71_alphaqcm_backtest.py \
  --run output/research/p2_71/alphaqcm_fin_v1/iqn_lam1_s0_10000 \
  --output output/research/p2_71/alphaqcm_portfolio_v1 \
  --fundamentals --start 2023-01-01 --end 2026-10-15
```

结果写入 `nav.csv`、`trades.csv`、`metrics.json`、`REPORT.md`，前端 `/factors` 页面的“AlphaQCM 冻结组合 · 中证500 对比”会读取最新 `alphaqcm_portfolio_v1`。

## 3. 单次 run 的目录与字段

每次 `run_miner()` 写入 `<output>/<run_id>/`：

| 文件/目录 | 用途 |
|---|---|
| `train.parquet`, `valid.parquet` | 训练子进程唯一可见的数据 |
| `request.json` | worker 请求、数据窗口、预算、resume 标记 |
| `factors.json` | 冻结表达式、权重、valid IC、`artifact_sha256` |
| `manifest.json` | split、预算、数据/源码 hash、`test_visible_to_worker` |
| `status.json` | 状态、停止原因、步数、评价数、checkpoint 路径 |
| `valid_best_table.csv` | valid-best 池；没有则以最终 pool 导出 |
| `test_eval/metrics.json` | 独立 evaluator 的测试段 IC/RankIC/ICIR/NW t |
| `checkpoints/` | 完整训练状态快照，2M run 才有 |
| `summary/` | TensorBoard 事件 |

先读 `manifest.json` 和 `status.json`，再看 `factors.json` 与 `test_eval/metrics.json`。不要用一个“最佳 IC”字段同时代表训练、验证和测试。

`status.json` 的常见状态：

- `COMPLETED`：达到预注册步数或评价数预算。
- `STOPPED_STEP_BUDGET`：达到 `stop_steps`。
- `STOPPED_EVAL_BUDGET`：达到 `stop_evals`（等评价次数实验用）。
- `STOPPED_WALL_CLOCK`：达到 `max_seconds`。
- `STOPPED_SIGNAL`：收到 `SIGTERM` / `SIGINT`，下一 step 边界保存 checkpoint 后停止。
- `FAILED`：异常；查看 worker stderr / `status.json` / `checkpoints/latest.pt`。

## 4. 等表达式评价次数对照（equal-evals）

步数相等不等于评价次数相等，因为不同策略生成的表达式长度不同。等评价实验分两相：

1. **参考相**：固定步数上限，先测量 6 个 run 实际完成的表达式评价次数。
2. **对照相**：写入 `T = min(reference evaluations)`，所有 arm 用 `stop_evals=T` 重跑。

命令：

```bash
python scripts/p2_71_alphaqcm_equal_evals.py \
  --output output/research/p2_71/alphaqcm_equal_evals_v2 \
  --steps 60000 --seeds 0,1,2 --assets 60 \
  --ref-max-seconds 2700 --cmp-max-seconds 2700
```

财务/估值特征默认启用；`--no-fundamentals` 可关闭。该开关改变特征空间（6→17），不同特征集的run不能互相续跑。

产物：

| 文件 | 用途 |
|---|---|
| `equal_evals_config.json` | 预注册配置；`target_evals` 在对照相前写盘 |
| `equal_evals_runs.csv` | 参考相 + 对照相逐 run |
| `manifest.json` | 汇总、限制、预算 |
| `REPORT.md` | 人读报告 |
| `ref/`, `cmp/` | 各 arm 的完整 run 目录 |

当前已有 v1：`output/research/p2_71/alphaqcm_equal_evals_v1/`，T=11；QCM 平均 test RankIC `+0.0463`，random `+0.0558`。v2（60k 步上限）已完成，T=62；QCM `+0.0134`，random `+0.1507`，仍仅作描述性对照，不构成晋级。

## 5. PIT 数据层

### 5.1 抓取

PIT 缓存默认写到：

```text
assets/data/raw/pit/market=CN/
```

先做小范围冒烟（只抓几个代码、跳过全市章数据）：

```bash
python scripts/p2_71_pit_fetch.py \
  --codes 000001.SZ,600000.SH --skip-bulk --n-panel 0
```

抓全市章数据（退市、指数权重、停牌）：

```bash
python scripts/p2_71_pit_fetch.py --bulk-only
```

抓默认研究 universe（panel top + delisted + 已有复权快照）：

```bash
python scripts/p2_71_pit_fetch.py --universe panel_top+delisted --n-panel 300
```

抓取是**可续跑**的：已有非空 unit 会跳过；失败写入 `coverage.json` 的 `failures`，进程退出码为 1。网络错误（`400/429/503`）直接重跑同一命令即可继续。

常用参数：

| 参数 | 含义 |
|---|---|
| `--bulk-only` | 只抓退市/指数权重/停牌，不抓代码级 |
| `--skip-bulk` | 只抓代码级，适合冒烟/补抓 |
| `--codes a,b,c` | 显式代码清单 |
| `--refresh` | 强制重抓已有 unit |
| `--n-panel 300` | panel 模式取多少只流动性股票 |
| `--sleep 0.05` | 请求间隔 |

### 5.2 审计

```bash
python scripts/p2_71_pit_audit.py \
  --output output/research/p2_71/pit_v1 \
  --assets 60 --index 000905.SH
```

产物：

- `REPORT.md`：覆盖、涨跌停交叉验证、qfq 基期发现。
- `manifest.json`：覆盖统计、限制、源码 hash。
- `panel_pit.parquet`：带 PIT 标志的面板。
- `delist_crosscheck.csv`、`index_members_by_snapshot.csv`、`qfq_vintage.csv`。

### 5.3 在 Python 里使用

```python
from backend.services import pit_data

annotated = pit_data.annotate_bars(
    bars,                       # columns: stock_code, trade_date, open, high, low, close, ...
    index_code="000905.SH",
)
# annotated 增加：
# is_listed, is_st, is_delist_risk, suspended_official,
# raw_close, raw_open, up_limit, down_limit,
# close_at_up, close_at_down, open_at_up, open_at_down,
# in_index, index_weight
```

`annotate_bars` 是按 `(stock_code, trade_date)` 的 as-of join；不要假设全市场统一日历。ST/退市/停牌/涨跌停标志目前是**上下文与审计字段**，还没有自动进入 evaluator 的交易掩码。

### 5.4 财务/行业相对估值特征

`backend/services/fundamental_data.py` 把两类特征接到价量面板：

```text
财务：roe, roa, grossprofit_margin, netprofit_margin,
      debt_to_assets, netprofit_yoy, or_yoy, ocfps
估值：pe_ind_pct, pb_ind_pct, size_ind_pct
```

财务按公告日 `ann_date` 做 as-of join：t日只能看到`ann_date<=t`的最新公告，禁止用`end_date`预知未来。缺失保留NaN。

```python
from backend.services.fundamental_data import attach_fundamentals
panel = attach_fundamentals(panel)
```

AlphaQCM 的 FeatureType 已扩展到17维；缺失财务列会变成全NaN tensor列，action space仍与tensor宽度一致。历史6特征checkpoint不能直接在17特征空间续跑。

## 6. Walk-forward

默认窗口是 `2021H1` 到 `2025H2`，每窗口半年度 test；面板只使用 test 开始前 4 年的数据选样：

```text
selection: [test_start - 4y, test_start)
train:     [test_start - 4y, test_start - 6m)
valid:     [test_start - 6m, test_start)
test:      [test_start, test_end]  # evaluator only
```

框架 demo（每窗口 5000 步）：

```bash
python scripts/p2_71_walk_forward.py \
  --output output/research/p2_71/walk_forward_v1 \
  --steps 5000 --seeds 0 --assets 60
```

只跑某几个窗口：

```bash
python scripts/p2_71_walk_forward.py \
  --output output/research/p2_71/walk_forward_v1 \
  --steps 5000 --seeds 0 --windows 2024H1,2024H2
```

全量预算：

```bash
python scripts/p2_71_walk_forward.py \
  --output output/research/p2_71/walk_forward_2m_v1 \
  --steps 2000000 --seeds 0 --assets 60
```

产物：

- `walk_forward_plan.json`：运行前写盘的窗口与切分。
- `window_runs.csv`：逐窗口/seed 的 valid/test 指标、PIT 上下文计数。
- `REPORT.md`、`manifest.json`。
- 每个窗口的 `test_panel_*.parquet` 与 run 目录。

注意：walk-forward 故意不使用 pilot 的 valid→test 两个月 padding；它是严格 PIT 版本，结果不能和 pilot 直接拼接。

## 7. 2M 全量 run

### 7.1 先写计划

```bash
python scripts/p2_71_alphaqcm_2m.py \
  --output output/research/p2_71/alphaqcm_2m_v1 \
  --steps 2000000
```

这会写 `run_plan.json`（预算、官方常量、CPU 适配、resume 协议、源码 hash）。不要在跑完之后改这个文件来“对齐”结果。

### 7.2 资源 probe

```bash
python scripts/p2_71_alphaqcm_2m.py \
  --output output/research/p2_71/alphaqcm_2m_v1 \
  --probe --probe-steps 1500
```

`budget_estimate.json` 记录 `seconds_per_1k_steps`、worker peak RSS、投影小时数。先看这个，再决定 CPU 还是 CUDA/隔离机器。

### 7.3 后台启动

```bash
python scripts/p2_71_alphaqcm_2m.py \
  --output output/research/p2_71/alphaqcm_2m_v1 \
  --launch --steps 2000000 --seed 0
```

全量版本默认带财务特征；如需复现旧6特征运行，显式加 `--no-fundamentals`，并与 `--probe` / `--run` / `--launch` 使用同一个输出目录。

`launch.json` 记录 pid、命令、日志路径；`run_s0/` 是训练目录，`runs.jsonl` 是完成记录。

第一次全量启动可加 `--max-seconds 3600` 做一段有界运行，确认 checkpoint/日志/CI 环境正常后再去掉；被 wall-clock 截断时会写 `STOPPED_WALL_CLOCK` 和完整 checkpoint。

### 7.4 监控

```bash
cat output/research/p2_71/alphaqcm_2m_v1/launch.json
tail -f output/research/p2_71/alphaqcm_2m_v1/logs/run_*.log
cat output/research/p2_71/alphaqcm_2m_v1/run_s0/status.json
ls -lh output/research/p2_71/alphaqcm_2m_v1/run_s0/checkpoints/
```

checkpoint 默认每 25,000 步一次，保留最近 2 个。`checkpoints/latest.pt` 是续跑入口。

### 7.5 续跑

前台续跑：

```bash
python scripts/p2_71_alphaqcm_2m.py \
  --output output/research/p2_71/alphaqcm_2m_v1 \
  --run --resume --steps 2000000 --seed 0
```

后台续跑：

```bash
python scripts/p2_71_alphaqcm_2m.py \
  --output output/research/p2_71/alphaqcm_2m_v1 \
  --launch --resume --steps 2000000 --seed 0
```

续跑时会校验 run_id、model、seed、pool_capacity、std_lam、official_commit、train/valid hash 和 features 列表；不一致会直接拒绝加载 checkpoint。

### 7.6 停止

优先让 worker 自己收信号：

```bash
ps -ax | grep '[a]lphaqcm_worker.py'
kill -TERM <worker_pid>
```

`launch.json` 的 pid 是外层 `p2_71_alphaqcm_2m.py --run` 进程；真正训练的是它启动的 `alphaqcm_worker.py` 子进程，所以停止时对 worker pid 发信号。worker 会在下一 step 边界写 checkpoint，`status.json` 变为 `STOPPED_SIGNAL`。不要直接 `kill -9`，除非只想要上一个周期 checkpoint。

## 8. 结果怎么读

当前产物索引：

| 产物 | 路径 |
|---|---|
| 20k pilot（QCM / std=0 / random） | [`alphaqcm_pilot_v1/REPORT.md`](../../output/research/p2_71/alphaqcm_pilot_v1/REPORT.md) |
| 等评价 v1（T=11） | [`alphaqcm_equal_evals_v1/REPORT.md`](../../output/research/p2_71/alphaqcm_equal_evals_v1/REPORT.md) |
| 等评价 v2（T=62，已完成） | [`alphaqcm_equal_evals_v2/REPORT.md`](../../output/research/p2_71/alphaqcm_equal_evals_v2/REPORT.md) |
| 财务特征 pilot（10k） | [`alphaqcm_fin_v1/pilot_runs.csv`](../../output/research/p2_71/alphaqcm_fin_v1/pilot_runs.csv) |
| 冻结组合 vs 中证500 | [`alphaqcm_portfolio_v1/REPORT.md`](../../output/research/p2_71/alphaqcm_portfolio_v1/REPORT.md) |
| 集成验收记录 | [`alphaqcm_integration/REPORT.md`](../../output/research/p2_71/alphaqcm_integration/REPORT.md) |
| 宇通客车 2026-03/06 头脑风暴 | [`600066_2026_03_06/REPORT.md`](../../output/research/p2_71/stock_brainstorm/600066_2026_03_06/REPORT.md) |
| 头脑风暴策略回测 | [`brainstorm_strategies/`](../../output/research/p2_71/brainstorm_strategies) |

头脑风暴的两个候选已登记为策略池：

- `brain_quality_lowpe_momentum`：2023-01-03至2026-10-15累计 **+27.63%**、年化 **+6.97%**、最大回撤 **−23.90%**；中证500累计 **+13.66%**、年化 **+3.60%**，相对累计超额 **+12.30%**。
- `brain_panic_reversal_quality`：40个触发，累计 **+1.36%**、年化 **+0.37%**、最大回撤 **−2.20%**；相对中证500累计超额 **−10.82%**。

两者均为research阶段，不接生产调度。`$netprofit_yoy`/`Min($pe_ind_pct,40)` 是另一轮AlphaQCM财务实验，不属于这两个头脑风暴候选。
| 精确 Alpha101 | [`alpha101_exact_v1/REPORT.md`](../../output/research/p2_71/alpha101_exact_v1/REPORT.md) |
| 数据契约 | [`data_contract_v1/REPORT.md`](../../output/research/p2_71/data_contract_v1/REPORT.md) |

历史 `alphaqcm_pilot_v1` / `alphaqcm_equal_evals_v1` 是6特征版本；当前脚本默认17特征（6价量+11财务/估值）。复现旧结果必须显式加 `--no-fundamentals`，不同特征集的曲线不能直接拼接。

单次 run：

```bash
cat output/research/p2_71/<run>/status.json
cat output/research/p2_71/<run>/manifest.json
cat output/research/p2_71/<run>/factors.json
cat output/research/p2_71/<run>/test_eval/metrics.json
```

重点看：

- `status` / `stop_reason` / `steps_completed` / `evaluations`：预算是否真的完成。
- `manifest.test_visible_to_worker`：必须是 `false`。
- `manifest.train_sha256` / `valid_sha256`：续跑和复现要一致。
- `factors.json` 的表达式和权重：这是冻结候选，不是生产配置。
- `test_eval/metrics.json` 的 `dates`、`mean`、`ci95`、`nw_t`：日期太少或区间跨 0 时只列观察。

多 run 汇总：

```bash
python scripts/p2_71_alphaqcm_summary.py output/research/p2_71/alphaqcm_pilot_v1
```

等评价实验直接看 `equal_evals_runs.csv` 和 `REPORT.md`。

### 8.1 导入前端因子池

```bash
python scripts/p2_71_import_alphaqcm_factors.py \
  --run output/research/p2_71/alphaqcm_fin_v1/iqn_lam1_s0_10000
```

导入到与前端同一个 `output/factor_pool_v2/factors.sqlite3`：

- 表达式以 `expression_language='alphagen_v1'` 保存；
- 前端显示“AlphaGen 原生表达式（正窗口）”；
- 支持校验、预览、编辑、版本历史、恢复和父子关系；
- 新版本仍标为 `unmeasured`，不会自动改变策略引用；
- 已有同名候选默认跳过，`--update` 才追加新版本。

也可以在前端 `/factors` 的“AlphaQCM 冻结组合”面板点击“把该 run 的候选导入因子池”；后端只允许导入 `output/research/p2_71/` 下的 run。

原生表达式编辑限制：

- 只允许正滚动窗口；
- 不接受未来 `Ref`；
- 只允许 AlphaGen 白名单算子/特征；
- 最大公式长度 4000 字符；
- 预览是 pandas 研究实现，和训练 tensor 的极端数值边界可能有细微差异。

### 8.2 个股头脑风暴

```bash
python scripts/p2_71_stock_brainstorm.py \
  --code 600066.SH --months 2026-03,2026-06 \
  --output output/research/p2_71/stock_brainstorm/600066_2026_03_06
```

输出 `REPORT.md`、`brainstorm.json`、`monthly_metrics.csv`、`events.csv`：

- 月度价量/资金流/行业中位/中证500对照；
- 财报、恐慌日、关键日前瞻收益；
- 可测试的因子/事件假设；
- 可导入因子池的候选公式。

它是研究假设生成器，不是自动交易信号；小样本事件只标为 `hypothesis` 或 `too_few_events`。

两个头脑风暴候选已用 `scripts/p2_71_brainstorm_strategies.py` 做300只流动性面板回测并登记策略池：

- `brain_quality_lowpe_momentum`：累计+27.63%、年化+6.97%、回撤−23.90%，相对中证500超额+12.30%。
- `brain_panic_reversal_quality`：40个触发、累计+1.36%、年化+0.37%、回撤−2.20%，相对超额−10.82%。

## 9. 编程接口

```python
from pathlib import Path
from backend.services.alphaqcm_adapter import (
    AlphaQCMMiner, MinerBudget, MiningRequest,
)

request = MiningRequest(
    run_id="my_run_s0",
    train_panel=train,          # columns: trade_date, stock_code, OHLCV, vwap
    valid_panel=valid,
    split={"train": ("2018-01-01", "2022-12-31"),
           "validation": ("2023-01-01", "2024-12-31"),
           "test": ("2025-01-01", "2025-10-31")},
    output_dir=Path("output/research/p2_71/my_run_s0"),
    budget=MinerBudget(seed=0, num_steps=20000, std_lam=1.0),
)

miner = AlphaQCMMiner()
artifact = miner.propose(request)
print(artifact.status, artifact.steps_completed, artifact.artifact_sha256)
print(miner.export(artifact))
```

需要 GPU 时给 `MinerBudget` 显式传 `device="cuda"`（并确认本机 `torch.cuda.is_available()`）；默认是 `cpu`。当前 `p2_71_alphaqcm_2m.py` 的 CLI 没有单独的 `--device` 参数，2M 计划默认 CPU 适配。

test 评测必须单独调用：

```python
metrics = miner.evaluate(
    artifact, test_panel,
    split=request.split,
    output=artifact.output_dir / "test_eval",
    label="paper",
)
```

表达式解析：

```python
from backend.services.alphaqcm_data import prepare_vendor_path
from backend.services.alphagen_expression import parse_expression

prepare_vendor_path()
expr = parse_expression("Mean($close,10)")
```

解析器只接受正窗口、已知特征/算子、无属性/下标/任意调用；因子表达式不能包含未来 `Ref`。

## 10. 目录速查

| 路径 | 内容 |
|---|---|
| `backend/services/alphaqcm_adapter.py` | propose / evaluate / export，run_miner |
| `backend/services/alphaqcm_checkpoint.py` | 完整状态 checkpoint/restore、stop hook |
| `backend/services/alphaqcm_data.py` | Qlib-free 面板 tensor |
| `backend/services/fundamental_data.py` | PIT财务/估值特征与ann_date as-of join |
| `backend/services/alphagen_native.py` | 原生AlphaGen表达式校验/pandas评测 |
| `backend/services/alphaqcm_portfolio.py` | 冻结因子组合回测与中证500对比 |
| `backend/services/alphagen_expression.py` | 导出表达式白名单解析 |
| `scripts/alphaqcm_worker.py` | 隔离训练子进程 |
| `scripts/alphaqcm_evaluate.py` | 冻结 artifact 的独立 test 评测 |
| `scripts/p2_71_alphaqcm_pilot.py` | 小面板 pilot / 消融 / 随机基线 |
| `scripts/p2_71_alphaqcm_equal_evals.py` | 等表达式评价次数对照 |
| `scripts/p2_71_alphaqcm_2m.py` | 2M 计划、probe、launch、resume |
| `scripts/p2_71_alphaqcm_backtest.py` | 冻结run → 组合净值/中证500曲线 |
| `scripts/p2_71_import_alphaqcm_factors.py` | 导入AlphaQCM候选到FactorStore |
| `scripts/p2_71_walk_forward.py` | 半年度 PIT walk-forward |
| `scripts/p2_71_pit_fetch.py` | PIT 抓取（可续跑） |
| `scripts/p2_71_pit_audit.py` | PIT 覆盖/一致性审计 |
| `backend/services/pit_data.py` | PIT as-of join 与标注 |
| `config/alphaqcm_research.yaml` | 固定commit、切分、pilot/2M 预算 |
| `config/alpha101_exact71.json` | 精确Alpha101来源与口径 |

## 11. 排障

| 现象 | 原因 / 处理 |
|---|---|
| `No module named gymnasium/tensorboard` | `uv pip install --python .venv/bin/python gymnasium tensorboard` |
| `start_steps=num_steps` 导致 PER `beta_steps=0` | 随机基线要让 `start_steps < num_steps`；脚本已用 `steps-100` |
| `start_time 前缺少 NN 个交易日` | train/valid panel 没有足够 warmup；用 `_slice` 多带 100 个交易日 |
| `end_time 后缺少 25 个交易日` | 标签需要未来 buffer；panel 要覆盖 end 之后约 25 个交易日 |
| `没有可评测因子` | 小预算随机探索可能没有完整表达式；增加步数或看 `status.evaluations` |
| `ensemble valid_ic = -1` / 因子为空 | pool 从未完成有效评价；不是“模型失败”，是预算/语法/mask 问题 |
| `checkpoint 不兼容` | seed/model/pool/std_lam/数据 hash 改变；换匹配 run 或从头跑 |
| 旧run续跑报特征维度/tensor越界 | 新代码特征空间从6扩到17；旧6特征checkpoint不能在新特征集上续跑，重新训练或保留旧代码快照 |
| `resume 需要 checkpoint` | 该 run 没有 `checkpoints/latest.pt`；不能凭空续跑 |
| PIT `daily_raw` 400/429/503 | relay 暂时失败；重跑同一命令，已成功 unit 会跳过 |
| PIT `units_failed > 0` | 看 `coverage.json.failures`；网络恢复后重跑，不要手改 coverage |
| PIT audit 覆盖率接近 0 | 先完成 `p2_71_pit_fetch.py` 的 bulk + code-level 抓取；`pit_v1` 不会凭空补数据 |
| equal-evals 重复启动 | 同一个 output 只能有一个 writer；先 `ps`/`launch.json` 确认 |
| 2M run 太慢 | 先看 `budget_estimate.json`；CPU 上按投影小时数决定是否换 CUDA/隔离机器 |

## 12. 后台任务与日志（opencode / shell）

推荐把所有长任务显式 `tee` 到 run 目录，而不是只依赖终端临时日志：

```bash
mkdir -p output/research/p2_71/alphaqcm_equal_evals_v2/logs
python scripts/p2_71_alphaqcm_equal_evals.py \
  --output output/research/p2_71/alphaqcm_equal_evals_v2 \
  --steps 60000 --seeds 0,1,2 --assets 60 \
  2>&1 | tee output/research/p2_71/alphaqcm_equal_evals_v2/logs/equal_evals.log
```

查看当前后台任务：

```bash
ps -ax | grep -E '[p]2_71_(alphaqcm|pit|walk_forward)' 
```

opencode 的临时日志通常在：

```text
/private/var/folders/*/*/T/opencode/*.log
```

找不到时：

```bash
find /private/var/folders -name 'equal_evals_v2.log' -o -name 'pit_fetch.log' 2>/dev/null
```

一个任务只允许一个 writer。重复启动 equal-evals / 2M 前先看 `ps`、`launch.json` 和 `status.json`，否则会争用同一个 output 或占用额外 CPU。

## 13. 使用顺序建议

```text
PIT fetch（先冒烟）
    ↓
PIT audit（覆盖率/涨跌停/复权基期）
    ↓
equal-evals v2（等评价次数）
    ↓
walk-forward demo（严格PIT切分）
    ↓
财务特征pilot（加 --fundamentals）
    ↓
冻结run → 组合回测（中证500曲线） → 导入前端因子池
    ↓
2M probe → 2M launch/resume（确认配置与预算后）
    ↓
冻结候选 → 成本后组合回放 → 前向影子
```

任何时候都不要用 test 指标回头改 seed、表达式、pool 或超参数。当前 AlphaQCM 的 v1/v2 结果都只是研究对照，不是生产选股或收益承诺。
