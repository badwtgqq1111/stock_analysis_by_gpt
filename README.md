# 港股量化研究与回测工具

基于 `Parquet + DuckDB` 数据底座的本地量化研究工具箱。架构设计详见 [QUANT_SYSTEM_OVERALL_DESIGN.md](./QUANT_SYSTEM_OVERALL_DESIGN.md)。

## 环境部署

```bash
cd /home/ccs/code/stock_analysis_by_gpt
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

所有命令使用 `.venv/bin/python`，数据目录默认 `./assets`。

## 数据同步

全港股日线+分钟级增量同步（2014 年起）：

```bash
.venv/bin/python sync_hk_market.py --db-dir ./assets --start-date 2014-01-01 --workers 24
```

仅日线、跳过已入库：

```bash
.venv/bin/python sync_hk_market.py --db-dir ./assets --start-date 2014-01-01 --workers 24 --frequencies daily --skip-existing
```

数据落盘后结构：

- `assets/data/clean/ohlcv` — 日线 Parquet 数据集
- `assets/data/meta/market_data.duckdb` — 元数据
- `assets/data/signal` — 批次扫描信号
- `assets/data/trade` — 回测交易结果

## 使用命令

### 因子验证（独立运行）

先跑验证，产出权重缓存和因子记分卡：

```bash
.venv/bin/python stock_analyzer.py validate_factors \
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
.venv/bin/python stock_analyzer.py select_stocks \
  --top-n 10 \
  --days 365 \
  --initial-capital 100000 \
  --max-workers 8 \
  --show-progress \
  --factor-set qlib_alpha158 \
  --export-csv output/selected_top10
```

写信号层+批次号：

```bash
.venv/bin/python stock_analyzer.py select_stocks \
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

### 因子研究报告（CSV 导出）

系统评估因子质量，导出完整研究报表：

```bash
.venv/bin/python stock_analyzer.py factor_report \
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
.venv/bin/python stock_analyzer.py all_hk \
  --top-n 10 --days 365 \
  --use-recommended-factor-weights \
  --max-workers 8 --show-progress
```

### 单股分析

```bash
.venv/bin/python stock_analyzer.py single 00700 --days 365
```

### 固定股票池多策略对比

```bash
.venv/bin/python stock_analyzer.py suite --days 365 --top-n 3
```

### 批次复盘

```bash
.venv/bin/python stock_analyzer.py review_batch hk_top10_20260516 --export-csv output/review
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
.venv/bin/python stock_analyzer.py validate_factors \
  --days 365 --factor-set qlib_alpha158 \
  --max-workers 8 --show-progress

# 第二步：选股+回测（读缓存，轻量）
.venv/bin/python stock_analyzer.py select_stocks \
  --top-n 10 --days 365 --max-workers 8 --show-progress \
  --export-csv output/top10 --persist-signals --batch-id hk_top10_latest
```

## 依赖

- requests >= 2.31.0
- pandas >= 2.0.0
- matplotlib >= 3.5.0
- duckdb
- pyarrow
- numpy
