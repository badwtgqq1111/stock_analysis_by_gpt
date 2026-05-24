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

如果你在 **macOS** 上准备使用 `LightGBM` 排序模式，先补上系统运行时依赖：

```bash
brew install libomp
```

然后再执行：

```bash
uv sync --dev
```

否则在运行 `--analysis-mode lightgbm` 时，可能会遇到 `Library not loaded: @rpath/libomp.dylib`。

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

使用 LightGBM Ranker 直接训练并排序：

```bash
brew install libomp   # macOS 需要先安装一次
uv sync

# 使用 Alpha158（默认）
uv run python stock_analyzer.py select_stocks \
  --analysis-mode lightgbm \
  --top-n 10 \
  --days 365 \
  --initial-capital 100000 \
  --factor-set qlib_alpha158 \
  --signal-recipes low_price_setup,range_breakout,box_pullback \
  --export-csv output/lightgbm_top10

# 使用 Alpha360（因子更多，覆盖更广）
uv run python stock_analyzer.py select_stocks \
  --analysis-mode lightgbm \
  --top-n 10 \
  --days 365 \
  --initial-capital 100000 \
  --factor-set qlib_alpha360 \
  --signal-recipes low_price_setup,range_breakout,box_pullback \
  --export-csv output/lightgbm_top10
```

> Alpha158 和 Alpha360 **不建议合并使用**。Alpha360 已包含 Alpha158 的大部分因子，两者高度重叠，直接拼在一起会产生多重共线性。建议分别训练后对比 ICIR 和选股结果，选表现更好的一组。如果两组各有优势，可以在预测分数层面做加权集成，而不是在特征层合并。

#### TA-Lib 技术指标因子

Alpha158 现已集成 35 个 TA-Lib 技术指标算子，默认随 `qlib_alpha158` 一起启用。原有 158 个滚动统计因子 + 35 个 TA 指标 = **193 个特征**，LightGBM 会自动做特征选择。

TA 指标分六类：

| 类别 | 个数 | 指标 | 归一化方式 |
|------|------|------|------------|
| 动量/超买超卖 | 11 | `TA_RSI`, `TA_STOCHRSI_K/D`, `TA_STOCH_K/D`, `TA_WILLR`, `TA_CCI`, `TA_CMO`, `TA_ULTOSC`, `TA_MFI`, `TA_BOP` | 原值（天然有界） |
| MACD 族 | 3 | `TA_MACD_DIF`, `TA_MACD_DEA`, `TA_MACD_HIST` | 除以收盘价 |
| 趋势 | 11 | `TA_ADX`, `TA_ADXR`, `TA_PLUS_DI`, `TA_MINUS_DI`, `TA_AROON_UP/DOWN`, `TA_AROONOSC`, `TA_TRIX`, `TA_APO`, `TA_PPO`, `TA_MOM`, `TA_ROC` | 有界原值 / 百分比 |
| 波动 | 5 | `TA_ATR`, `TA_NATR`, `TA_TRANGE`, `TA_BBANDS_PCT_B`, `TA_BBANDS_WIDTH` | 除以收盘价 / 原值 |
| 量价 | 3 | `TA_OBV`, `TA_AD`, `TA_ADOSC` | 除以成交量均值 |
| 特色 | 1 | `TA_KAMA` | 除以收盘价 |

完整列表和算子注册表见 [factor_engine/expressions/ta_operators.py](factor_engine/expressions/ta_operators.py)。

**测试方法：**

```bash
# 直接跑回测——TA 指标默认已启用，无需额外参数
uv run python stock_analyzer.py select_stocks \
  --analysis-mode lightgbm \
  --top-n 10 --days 365 \
  --max-workers 8 --show-progress \
  --export-csv output/lightgbm_ta

# 对比：跑一版不加 TA 指标的作为 baseline
# （通过 Python API 传入 config 关闭 TA，见下方代码示例）
```

或在 Python 中快速验证：

```python
from factor_engine.registry import create_factor_set
import pandas as pd
import numpy as np

# 造一组假数据
dates = pd.date_range("2023-01-01", periods=200, freq="B")
df = pd.DataFrame({
    "open": 100 + np.cumsum(np.random.randn(200) * 0.3),
    "high": 101 + np.cumsum(np.random.randn(200) * 0.3),
    "low": 99 + np.cumsum(np.random.randn(200) * 0.3),
    "close": 100 + np.cumsum(np.random.randn(200) * 0.5),
    "volume": np.abs(np.random.randn(200) * 10000 + 50000),
}, index=dates)

# 默认：193 个特征（158 + 35 TA）
fs = create_factor_set("qlib_alpha158")
result = fs.transform(df)
print(f"总特征: {len(result.columns)}")
print(f"TA 列: {[c for c in result.columns if c.startswith('TA_')]}")

# 只用 5 个 TA 指标
fs2 = create_factor_set("qlib_alpha158", config={
    "ta": {"indicators": ["TA_RSI", "TA_MACD_DIF", "TA_ATR", "TA_OBV", "TA_ADX"]},
})
result2 = fs2.transform(df)
print(f"精简: {len(result2.columns)} 特征")

# 关闭 TA（仅 158 个原始因子）
fs3 = create_factor_set("qlib_alpha158", config={"ta": {"indicators": []}})
result3 = fs3.transform(df)
print(f"无 TA: {len(result3.columns)} 特征")
```

**TA 因子分类**（用于因子评分和验证报告）：

| 分类 | 新增 TA 指标 | 说明 |
|------|-------------|------|
| `trend` | MACD 族、ADX 族、AROON、TRIX、APO、PPO、MOM、ROC、KAMA | 趋势和动量 |
| `quality` | OBV、AD、ADOSC、MFI、BOP、Stoch_K/D、ULTOSC | 量价关系 |
| `risk` | ATR、NATR、TRANGE、BBANDS_PCT_B、BBANDS_WIDTH | 波动和风险 |
| `sentiment` | RSI、StochRSI_K/D、WILLR、CCI、CMO | 超买超卖情绪（**新增分类**） |

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
| `--analysis-mode` | `factor`、`strategy` 或 `lightgbm` | `factor` |
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

`lightgbm` 模式第一版会在一次命令中完成训练和预测，不依赖 `validate_factors` 生成的权重缓存；它直接复用 `factor_set` 生成的日频特征，并按未来收益构造横截面排序标签。

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

## Web 界面

项目提供了基于 **Vue 3 + lightweight-charts + FastAPI** 的 Web 看板，替代原来的 Plotly Dash 页面。包含四个功能页：选股结果、因子 IC 分析、K 线图表、组合回测。

### 启动方式

**1. 启动后端 (FastAPI, port 8000)**

```bash
cd stock_analysis_by_gpt
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

首次启动时会自动生成因子 IC 缓存（约 30 秒），后续请求直接读缓存。

**2. 启动前端 (Vite, port 5173)**

需要 [Bun](https://bun.sh)（或 Node.js）：

```bash
cd stock_analysis_by_gpt/frontend
bun install
bun run dev
```

前端开发服务器会自动将 `/api` 请求代理到 `localhost:8000`。浏览器打开 `http://localhost:5173`。

### 生产部署

```bash
cd stock_analysis_by_gpt/frontend
bun install && bun run build
```

构建产物在 `frontend/dist/`，FastAPI 会自动托管静态文件。直接访问 `http://localhost:8000` 即可，无需单独启动前端。

### 页面说明

| 页面 | 路由 | 功能 |
|------|------|------|
| 选股结果 | `/` | LightGBM Top10 排序表、评分柱状图、SHAP 特征解释 |
| 因子 IC 分析 | `/factor-ic` | IC/RankIC 时序图、Top10 因子柱状图、汇总表，支持切换 Alpha158/Alpha360 和回看周期 |
| K 线图表 | `/kline` | Canvas K 线 + MA 均线 + 成交量 + 信号标记 + 筹码分布，支持十字光标和滚轮缩放 |
| 组合回测 | `/portfolio` | 净值曲线、回撤曲线、收益指标卡、当前持仓表 |

K 线页面支持叠加显示开关：LightGBM 买卖信号（箭头标记）、筹码分布（右侧面板）。颜色遵循 A 股惯例：**红涨绿跌**。
