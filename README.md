# 港股量化研究与回测工具

基于 `Parquet + DuckDB` 数据底座的本地量化研究工具箱。架构设计详见 [QUANT_SYSTEM_OVERALL_DESIGN.md](./QUANT_SYSTEM_OVERALL_DESIGN.md)。

## 环境部署

Linux 环境推荐直接使用 `uv` 管理工具链和虚拟环境。下面示例使用 Python `3.12.3`。

### 1. 安装 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

安装完成后重新打开终端，或让当前 shell 重新加载环境变量，再确认：

```bash
uv --version
```

### 2. 安装并固定 Python 3.12.3

```bash
uv python install 3.12.3
uv python pin 3.12.3
```

确认当前项目使用的是 `3.12.3`：

```bash
uv run python --version
```

### 3. 安装项目依赖

本项目要求 Python `3.10+`，推荐直接使用 `3.12.3`。在项目根目录执行：

```bash
cd /path/to/stock_analysis_by_gpt
uv sync --dev
```

`uv` 会自动创建并管理 `.venv`。所有命令统一使用 `uv run python`，数据目录默认 `./assets`。

项目通过 `pyproject.toml` 中的 `[tool.uv.sources]` 将 `akshare` 指向同级目录 `../akshare` 并以可编辑模式安装；如果你的本地目录结构不同，先调整这一路径再执行 `uv sync`。

港股历史同步默认优先使用 `akshare_eastmoney`。如果显式指定 `--data-source sina`，本地 `akshare` 会通过解码池复用预热后的 MiniRacer context，macOS 上也不需要默认降低整体并发；`--sina-max-concurrency` 仅作为兼容旧实现或异常环境的手动兜底。

## 数据同步

全港股日线+分钟级增量同步（2014 年起）：

```bash
uv run python sync_hk_market.py --db-dir ./assets --start-date 2014-01-01 --workers 24
```

如果希望实时看到按 `daily/1min/5min/60min` 聚合的进度：

```bash
uv run python sync_hk_market.py --db-dir ./assets --start-date 2014-01-01 --workers 24 --show-progress
```

仅日线、跳过已入库：

```bash
uv run python sync_hk_market.py --db-dir ./assets --start-date 2014-01-01 --workers 24 --frequencies daily --skip-existing
```

如果你要强制优先走 sina：

```bash
uv run python sync_hk_market.py --db-dir ./assets --start-date 2014-01-01 --workers 24 --data-source sina --show-progress
```

如果你在 macOS 上遇到 `libmini_racer.dylib` / `partition_address_space` 崩溃，再显式收紧到：

```bash
uv run python sync_hk_market.py \
  --db-dir ./assets \
  --start-date 2014-01-01 \
  --workers 24 \
  --data-source sina \
  --show-progress \
  --min-daily-rows-for-intraday 5
```

开启 `--show-progress` 后，会在 stderr 持续刷新类似：

- `stocks_done=120/5400`：已完成整只股票抓取的数量
- `tasks_done=360/21600`：已完成的周期任务数
- `daily=120/5400`、`1min=80/5400`：各周期自己的完成进度
- `rate` / `eta`：整体任务吞吐和预计剩余时间

分钟线同步默认做两项加速：

- 先抓 `1min`，再从本地重采样派生 `5min/15min/30min/60min`，减少重复请求；如需强制请求原始周期，加 `--no-derive-intraday`
- 日线有效行数低于 `--min-daily-rows-for-intraday` 时跳过分钟线，默认阈值为 `3`；如需所有股票都尝试分钟线，可设为 `0`

数据落盘后结构：

- `assets/data/clean/ohlcv` — 日线 Parquet 数据集
- `assets/data/meta/market_data.duckdb` — 元数据
- `assets/data/signal` — 批次扫描信号
- `assets/data/trade` — 回测交易结果

## 使用命令

### 因子验证（独立运行）

先跑验证，产出权重缓存和因子记分卡：

```bash
uv run python stock_analyzer.py validate_factors \
  --days 365 \
  --factor-set qlib_alpha158 \
  --max-workers 8 \
  --show-progress \
  --export-csv output/validation_scorecard
```

参数说明：

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--days` | 验证窗口天数 | 365 |
| `--factor-set` | 因子集名称 | `qlib_alpha158` |
| `--max-workers` | 并发线程数 | 0（自动） |
| `--show-progress` | 显示进度 | 关 |
| `--horizons` | 验证 horizon 列表 | `1,5,10,20` |
| `--quantiles` | 分组数 | 5 |
| `--min-observations` | 最小样本数 | 5 |
| `--stock-limit` | 参与验证的股票上限 | 不限 |
| `--validation-factor-scope` | `scoring_only` 或 `all` | `scoring_only` |
| `--refresh-recommended-factor-weights` | 强制重算，跳过缓存 | 关 |
| `--export-csv` | 导出因子记分卡路径 | 不导出 |

验证完成后缓存写入 `assets/data/meta/factor_weight_cache/`。

### 选股+回测（基于验证权重）

读取验证缓存，执行全市场 TopN 选股+回测：

```bash
uv run python stock_analyzer.py select_stocks \
  --top-n 10 \
  --days 365 \
  --initial-capital 100000 \
  --max-workers 8 \
  --show-progress \
  --factor-set qlib_alpha158 \
  --signal-recipes low_price_setup,range_breakout,box_pullback \
  --export-csv output/selected_top10
```

写信号层+批次号：

```bash
uv run python stock_analyzer.py select_stocks \
  --top-n 10 --days 365 \
  --max-workers 8 --show-progress \
  --persist-signals --batch-id hk_top10_20260516
```

参数说明：

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--top-n` | 组合持有数量 | 3 |
| `--days` | 分析天数 | 365 |
| `--initial-capital` | 初始资金 | 100000 |
| `--max-workers` | 并发线程数 | 0（自动） |
| `--show-progress` | 显示进度 | 关 |
| `--fast-mode` | 跳过组合净值回放 | 关 |
| `--analysis-mode` | `factor` 或 `strategy` | `factor` |
| `--factor-set` | 因子集名称 | `qlib_alpha158` |
| `--signal-recipes` | 信号 recipe，逗号分隔；可用 `low_price_setup`,`range_breakout`,`box_pullback` | `low_price_setup` |
| `--export-csv` | 导出结果路径 | 不导出 |
| `--persist-signals` | 写入 signal 层 | 关 |
| `--batch-id` | 批次号 | 自动生成 |
| `--validation-days` | 验证窗口天数 | 同 `--days` |
| `--validation-factor-scope` | 与验证时保持一致 | `scoring_only` |

开启 `--show-progress` 后，`select_stocks` 的因子批量模式会先打印
`phase=batch_factor` 摘要，包含总股票数、批次数、自动批大小、实际 worker 数和当前可用内存；
运行中会持续刷新 `stocks_done`、`batches_done`、`active_batches`、`success`、`rate`、`eta`。

`factor` 模式下的批大小会根据股票总数、`--max-workers` 和当前设备可用内存自动调整，
低内存环境会主动缩小批次，避免单批过大导致长时间无输出或内存压力过高。

导出文件：`{base}_ranking.csv`、`{base}_selected.csv`、`{base}_watchlist.csv`。

#### 信号 recipe 说明

`--signal-recipes` 用于选择形态信号组合。因子层负责给股票打分，recipe 层负责把价量形态翻译成可排序的 `setup_type/setup_score`。

| recipe | 识别形态 | 主要条件 | 适合用途 |
|---|---|---|---|
| `low_price_setup` | 低价股突破前、底部反弹、横盘惩罚 | 低价区间、成交额、接近 20 日高点、60 日低位反弹、量比 | 默认稳健筛选 |
| `range_breakout` | 横盘压缩后的放量突破 | 突破前 20 日高点、波动/区间压缩、量比放大 | 捕捉启动日 |
| `box_pullback` | 箱体突破后的缩量回踩 | 前期箱体、已突破箱体上沿、回踩不破、缩量 | 等待二次确认买点 |

常用组合：

```bash
# 默认：低价股预突破/底部反弹
uv run python stock_analyzer.py select_stocks \
  --top-n 10 --days 365 --max-workers 8 \
  --factor-set qlib_alpha158 \
  --signal-recipes low_price_setup

# 进攻型：默认形态 + 放量突破
uv run python stock_analyzer.py select_stocks \
  --top-n 10 --days 365 --max-workers 8 \
  --factor-set qlib_alpha158 \
  --signal-recipes low_price_setup,range_breakout

# 确认型：突破后等待回踩不破
uv run python stock_analyzer.py select_stocks \
  --top-n 10 --days 365 --max-workers 8 \
  --factor-set qlib_alpha158 \
  --signal-recipes low_price_setup,box_pullback

# 全部形态一起参与排序
uv run python stock_analyzer.py select_stocks \
  --top-n 10 --days 365 --max-workers 8 \
  --factor-set qlib_alpha158 \
  --signal-recipes low_price_setup,range_breakout,box_pullback
```

#### 信号 recipe 验证报告

`signal_report` 用于验证 recipe 触发后的未来收益表现，回答"哪个形态更有效"。它会逐日扫描指定股票池，统计每个 recipe / setup_type 的触发次数、未来收益、胜率和最大回撤。

```bash
uv run python stock_analyzer.py signal_report \
  --days 365 \
  --signal-recipes low_price_setup,range_breakout,box_pullback \
  --horizons 20,40,60 \
  --signal-cooldown-days 20 \
  --signal-event-policy first \
  --max-workers 8 \
  --show-progress \
  --export-csv output/signal_report
```

`--signal-cooldown-days` 会把同一只股票、同一个 `recipe_name/setup_type` 在指定天数内的连续触发合并成一个信号区间，避免每日重复触发高估样本量。`--signal-event-policy` 控制区间内用哪一天作为入场事件：

| policy | 说明 |
|---|---|
| `first` | 使用区间第一次触发，默认，更接近真实首次入场 |
| `latest` | 使用区间最后一次触发 |
| `best_score` | 使用区间内 `setup_score` 最高的一次 |

导出文件：

- `output/signal_report_signal_summary.csv`：按 `recipe_name/setup_type` 汇总触发次数、平均收益、胜率、平均最大回撤
- `output/signal_report_signal_events.csv`：合并后的可交易信号事件，包含 `signal_zone_id`、区间起止日期、合并次数、setup 分、未来收益和回撤
- `output/signal_report_signal_events_raw.csv`：未合并的逐日原始触发事件
- `output/signal_report_metadata.json`：样本股票数、触发事件数、horizon 和 recipe 参数

`signal_summary.csv` 还会包含稳定性诊断字段：

| 字段 | 说明 |
|---|---|
| `unique_stock_count` | 触发该 setup 的股票数 |
| `top5_stock_event_share` | 触发次数最多的 5 只股票占比，用于判断样本是否过度集中 |
| `median_forward_return_*` | 未来收益中位数 |
| `p25_forward_return_*` / `p75_forward_return_*` | 未来收益四分位数 |
| `p95_forward_drawdown_*` | 较差 5% 情况下的最大回撤分位 |
| `return_drawdown_ratio_*` | 平均未来收益 / 平均最大回撤绝对值 |
| `avg_win_*` / `avg_loss_*` | 盈利样本平均收益 / 亏损样本平均收益 |

### 因子研究报告（CSV 导出）

系统评估因子质量，导出完整研究报表：

```bash
uv run python stock_analyzer.py factor_report \
  --days 365 \
  --factor-set qlib_alpha158 \
  --max-workers 8 \
  --show-progress \
  --validation-factor-scope all \
  --export-csv output/factor_report
```

导出文件：`*_factor_scorecard.csv`、`*_ic_summary.csv`、`*_quantile_summary.csv`、`*_long_short_summary.csv`、`*_turnover_summary.csv`、`*_decay_summary.csv`、`*_metadata.json`。

### 兼容旧模式（验证+选股一体）

```bash
uv run python stock_analyzer.py all_hk \
  --top-n 10 --days 365 \
  --use-recommended-factor-weights \
  --max-workers 8 --show-progress
```

### 单股分析

```bash
uv run python stock_analyzer.py single 00700 --days 365
```

### 固定股票池多策略对比

```bash
uv run python stock_analyzer.py suite --days 365 --top-n 3
```

### 批次复盘

```bash
uv run python stock_analyzer.py review_batch hk_top10_20260516 --export-csv output/review
```

### Python API

```python
from analyzer_core import StockAnalyzer

analyzer = StockAnalyzer(db_dir="./assets")

# 因子验证
report = analyzer.build_factor_validation_report(
    stock_codes=analyzer.get_all_stocks(),
    days=365,
    factor_set="qlib_alpha158",
    horizons=(1, 5, 10, 20),
    quantiles=5,
    min_observations=5,
    max_workers=8,
)

# 全市场 TopN
result = analyzer.backtest_hk_market(
    days=365, top_n=10, initial_capital=100000,
    max_workers=8, analysis_mode="factor", factor_set="qlib_alpha158",
)
```

## 推荐工作流

```bash
# 第一步：因子验证（内存密集，单独跑）
uv run python stock_analyzer.py validate_factors \
  --days 365 --factor-set qlib_alpha158 \
  --max-workers 8 --show-progress

# 第二步：选股+回测（读缓存，轻量）
uv run python stock_analyzer.py select_stocks \
  --top-n 10 --days 365 --max-workers 8 --show-progress \
  --export-csv output/top10 --persist-signals --batch-id hk_top10_latest
```

## 依赖

依赖统一由 `pyproject.toml` 管理，安装与更新请使用 `uv sync` / `uv lock`。

- requests >= 2.31.0
- pandas >= 2.0.0
- matplotlib >= 3.5.0
- duckdb
- pyarrow
- numpy
