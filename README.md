# 港股量化研究与回测工具

基于 `ClickHouse + Parquet` 的本地港股量化研究工具箱，支持数据同步、因子生成、LightGBM 排序选股、行业分层候选池、组合回测和 Web 看板。

架构详见 [QUANT_SYSTEM_OVERALL_DESIGN.md](./QUANT_SYSTEM_OVERALL_DESIGN.md)。

## 环境部署

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12.3
uv python pin 3.12.3
uv sync --dev

# macOS 上 LightGBM 需要 libomp
brew install libomp
```

项目通过 `pyproject.toml` 的 `[tool.uv.sources]` 将 `akshare` 指向同级目录 `../akshare`。如果本地目录结构不同，需要调整该路径。

## 运行顺序

依赖关系如下：

```text
sync
  ├─> backfill-industry ────────────────┐
  ├─> generate-factors ──> select lightgbm
  │        ├─────────────> signal-report
  │        ├─────────────> factor-report
  │        └─> validate-factors ──> select factor
  └─> fetch-alt 可选 ────────────────> select lightgbm
```

推荐主流程：

```bash
# 1. 同步港股历史行情
uv run python run.py sync \
  --start-date 2014-01-01 \
  --frequencies daily \
  --skip-existing \
  --max-workers 24 \
  --show-progress

# 2. 补全行业、标的类型、fund-like 与可交易性元数据
uv run python run.py backfill-industry \
  --force \
  --normalize-existing \
  --max-workers 8 \
  --show-progress

# 3. 生成因子
uv run python run.py generate-factors \
  --days 365 \
  --factor-set alpha158_hk \
  --max-workers 8 \
  --show-progress

# 4. LightGBM 行业分层选股
uv run python run.py select \
  --analysis-mode lightgbm \
  --top-n 10 \
  --days 365 \
  --factor-set alpha158_hk \
  --min-market-cap 30 \
  --min-daily-turnover 500 \
  --max-workers 8 \
  --export-csv output/results \
  --show-progress
```

`select --analysis-mode lightgbm` 会在单次命令内训练和预测，不依赖 `validate-factors`。`select --analysis-mode factor` 才依赖 `validate-factors`。

## 数据与存储

默认写入本地 Parquet：

| 数据 | 路径 |
|---|---|
| OHLCV | `assets/data/clean/ohlcv` |
| 股票元数据 | `assets/data/meta/stock_info_registry` |
| 信号批次 | `assets/data/signal` |
| 回测交易 | `assets/data/trade` |
| ClickHouse 数据卷 | `assets/clickhouse` |

ClickHouse 可选。设置环境变量后，features 与 stock info registry 优先写入 ClickHouse；不可用时自动回退到 Parquet。

```bash
mkdir -p assets/clickhouse

docker run -d --name clickhouse \
  --restart unless-stopped \
  -p 8123:8123 -p 9000:9000 \
  -v "$(pwd)/assets/clickhouse:/var/lib/clickhouse" \
  -e CLICKHOUSE_USER=default \
  -e CLICKHOUSE_PASSWORD=quant2024 \
  -e CLICKHOUSE_DB=quant \
  clickhouse/clickhouse-server

export CLICKHOUSE_HOST=localhost
export CLICKHOUSE_PORT=8123
export CLICKHOUSE_USER=default
export CLICKHOUSE_PASSWORD=quant2024
export CLICKHOUSE_DATABASE=quant
```

`--restart unless-stopped` 会让 Docker 服务启动后自动拉起 ClickHouse 容器；`-v "$(pwd)/assets/clickhouse:/var/lib/clickhouse"` 将 ClickHouse 数据文件保存在项目的 `assets/clickhouse` 下，删除或重建容器时数据不会丢。`/var/lib/clickhouse` 是必须持久化的数据目录，应用侧数据库名使用 `CLICKHOUSE_DATABASE`，默认建议为 `quant`。

## 行业分层选股

`backfill-industry` 依赖 `sync` 产生的港股代码池，不依赖因子生成。它会写入：

| 字段 | 用途 |
|---|---|
| `industry_l1/l2/l3` | 真实行业分类、行业内 TopN、覆盖率检查 |
| `instrument_type` | `common_stock` / `fund_like` / `reit` 等标的类型 |
| `is_fund_like` | 硬排除 ETF、基金、REIT、杠杆反向、结构化产品 |
| `tradable_flag` | 停牌或不可交易标的硬过滤 |

覆盖率优先看普通股口径：

```bash
uv run python run.py industry-coverage --show-missing
```

重点指标是 `ordinary_l1_rate` / `ordinary_l2_rate`。港股 `03/09/28/30/31/34/72/73/75/77` 等代码段大量是 ETF、基金、杠杆反向或结构化产品，会被统计为 fund-like，不应按普通股票要求行业覆盖。

## 验证顺序

改动行业数据、行业内 TopN、硬过滤或组合选择后，按下面顺序验证。

```bash
# 1. 代码级 smoke test：行业候选池、硬过滤、权重解释
uv run pytest test/test_portfolio_builder.py -q

# 2. 小样本行业补全
uv run python run.py backfill-industry \
  --stock-codes 00700 00005 00941 01299 03690 \
  --force \
  --normalize-existing \
  --show-progress

# 3. 小样本覆盖率
uv run python run.py industry-coverage \
  --stock-codes 00700 00005 00941 01299 03690 \
  --show-missing

# 4. 小样本选股，select 当前用 stock-limit 控制规模
uv run python run.py select \
  --analysis-mode lightgbm \
  --stock-limit 50 \
  --top-n 3 \
  --days 365 \
  --factor-set alpha158_hk \
  --export-csv output/industry_smoke \
  --show-progress

# 5. 检查 selected 必须来自行业候选池，且无硬过滤失败标的
python - <<'PY'
import pandas as pd

selected = pd.read_csv("output/industry_smoke_alpha158_hk_selected.csv")
required = [
    "industry_l1", "industry_l2", "industry_rank", "industry_score",
    "industry_cap", "selection_eligible", "eligibility_reasons",
    "valuation_metric_used", "quality_data_coverage",
]
missing = [column for column in required if column not in selected.columns]
assert not missing, f"missing columns: {missing}"
assert selected["selection_eligible"].fillna(False).all()
assert (selected["industry_rank"] <= selected["industry_cap"]).all()
print(selected[["stock_code", "industry_l1", "industry_l2", "industry_rank", "industry_score", "portfolio_weight"]])
PY
```

小样本通过后再跑全市场：

```bash
uv run python run.py backfill-industry \
  --force \
  --normalize-existing \
  --max-workers 8 \
  --show-progress

uv run python run.py industry-coverage --show-missing

uv run python run.py select \
  --analysis-mode lightgbm \
  --top-n 10 \
  --days 365 \
  --factor-set alpha158_hk \
  --min-market-cap 30 \
  --min-daily-turnover 500 \
  --max-workers 8 \
  --export-csv output/results \
  --llm-report \
  --show-progress
```

全市场验证重点：

| 文件 | 检查项 |
|---|---|
| `output/results_alpha158_hk_ranking.csv` | 包含 `selection_eligible`、`eligibility_reasons`、`industry_rank`、`industry_score`、`industry_cap` |
| `output/results_alpha158_hk_selected.csv` | 全部 `selection_eligible=True` 且 `industry_rank <= industry_cap` |
| `output/results_alpha158_hk_industry_weights.csv` | 行业权重和 HHI 是否过度集中 |
| `docs/report/{日期}_llm.md` | 入选/剔除理由是否能解释行业、质量、估值、流动性 |

## 命令速查

| 命令 | 依赖 | 用途 |
|---|---|---|
| `sync` | 无 | 拉取 OHLCV，产生代码池 |
| `backfill-industry` | `sync` | 补行业、标的类型、fund-like、可交易性 |
| `generate-factors` | `sync` | 生成 `alpha158_hk` 等因子 |
| `select --analysis-mode lightgbm` | `sync`、`generate-factors`，建议先 `backfill-industry` | 推荐选股模式，单次命令内训练和预测 |
| `validate-factors` | `generate-factors` | 仅供 factor 模式选股使用 |
| `select --analysis-mode factor` | `generate-factors`、`validate-factors` | 验证权重驱动的选股模式 |
| `signal-report` | `generate-factors` | 验证信号 recipe 触发后的未来收益 |
| `factor-report` | `generate-factors` | 独立评估因子 IC/RankIC |
| `fetch-alt` | 无 | 可选新闻情感特征，LightGBM 有数据则自动加载 |

常用命令模板：

```bash
uv run python run.py sync --start-date 2014-01-01 --frequencies daily --skip-existing --max-workers 24 --show-progress
uv run python run.py backfill-industry --force --normalize-existing --max-workers 8 --show-progress
uv run python run.py generate-factors --days 365 --factor-set alpha158_hk --max-workers 8 --show-progress
uv run python run.py validate-factors --days 365 --factor-set alpha158_hk --export-csv output/validation_scorecard --show-progress
uv run python run.py signal-report --days 365 --signal-recipes low_price_setup,range_breakout,box_pullback --horizons 20,40,60 --export-csv output/signal_report --show-progress
uv run python run.py factor-report --days 365 --factor-set alpha158_hk --export-csv output/factor_report --show-progress
uv run python run.py fetch-alt --stock-limit 100 --persist-signals --show-progress
```

刚补完行业或 instrument 元数据后，不需要重跑 `sync`，但需要重跑 `select`。`alpha158_hk` 在 `qlib_alpha158` 基础上加入港股定制因子，推荐显式指定。

导出文件：

| 文件 | 内容 |
|---|---|
| `{base}_{factor_set}_ranking.csv` | 全市场排名、硬过滤原因、行业排名 |
| `{base}_{factor_set}_selected.csv` | 最终持仓 |
| `{base}_{factor_set}_watchlist.csv` | 观察名单 |
| `{base}_{factor_set}_industry_weights.csv` | 组合行业权重 |

行业分层字段：

| 字段 | 含义 |
|---|---|
| `industry_l1/l2/l3` | 真实行业分类 |
| `selection_eligible` / `eligibility_reasons` | 硬过滤结果与剔除原因 |
| `industry_rank` / `industry_score` / `industry_cap` | 行业内候选排名、分数和候选上限 |
| `industry_concentration_penalty` / `final_score` | 行业集中度惩罚和最终分 |
| `quality_data_coverage` / `quality_missing_fields` | 财务质量数据覆盖 |
| `valuation_metric_used` / `valuation_data_coverage` | 行业内估值指标与覆盖 |
| `portfolio_industry_hhi` | selected 组合行业集中度 |

## 辅助功能

```bash
# 单股分析 / 策略套件 / 批次复盘 / 旧一体化入口
uv run python run.py single 00700 --days 365
uv run python run.py suite --days 365 --top-n 3
uv run python run.py review hk_top10_20260516 --export-csv output/review
uv run python run.py all --top-n 10 --days 365 --max-workers 8 --show-progress

# LLM 报告：设置密钥后，在 select 加 --llm-report
export DEEPSEEK_API_KEY=...
uv run python run.py select --analysis-mode lightgbm --top-n 10 --days 365 --llm-report
```

## Python API

```python
from core import StockAnalyzer

analyzer = StockAnalyzer(db_dir="./assets")

result = analyzer.backtest_hk_market(
    days=365,
    top_n=10,
    initial_capital=100000,
    max_workers=8,
    analysis_mode="lightgbm",
    factor_set="alpha158_hk",
)
```

## Web 界面

```bash
# 后端
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# 前端，需要 Bun
cd frontend
bun install
bun run dev
```

生产部署：`cd frontend && bun run build`。FastAPI 会托管 `frontend/dist/`，访问 `http://localhost:8000`。

| 页面 | 路由 | 功能 |
|---|---|---|
| 选股结果 | `/` | LightGBM TopN、评分、SHAP 特征解释 |
| 因子 IC 分析 | `/factor-ic` | IC/RankIC 时序和因子汇总 |
| K 线图表 | `/kline` | K 线、均线、成交量、信号标记 |
| 组合回测 | `/portfolio` | 净值、回撤、收益指标、当前持仓 |
