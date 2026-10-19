# Runbook：大盘指数与全 A 成交额

页面：`/#/market`（导航「行情 → 大盘行情」）；首页「选股结果」顶部另有 4 个核心指数速览。

## 数据源

| 数据 | 文件 | 生产脚本 |
|---|---|---|
| 8 个指数 OHLCV | `assets/data/meta/cn_benchmark_daily.parquet` | `scripts/backfill_cn_benchmark.py` |
| 全 A 每日成交额 | `assets/data/meta/cn_market_turnover.parquet` | `scripts/build_cn_market_turnover.py` |

指数：上证、深证成指、创业板指、沪深300、中证500、中证全指、科创50、上证50。

- Tushare `index_daily` 的 `amount` 原始单位是**千元**；脚本另存 `amount_yuan = amount * 1000`。
- 全 A 成交额来自 clean 日线 `amount` 汇总，单位**元**；页面换算为亿元。
- 成交额不计入停牌/无成交股票；`n_stocks` 是当日有正成交额的股票数。

## 手工刷新

```bash
uv run python scripts/backfill_cn_benchmark.py
uv run python scripts/build_cn_market_turnover.py
```

每日自动化已在 `scripts/run_daily_production.sh` 中加入：

- `benchmark`：数据面最前面，原有步骤；
- `market_overview`：主阶段 `clean_panel` 之后，非阻塞；开关 `DAILY_MARKET_OVERVIEW=0`。

## 接口

```text
GET /api/market/overview?spark=20
GET /api/market/turnover?days=365
GET /api/market/indices/{code}/intraday
GET /api/market/indices/{code}/ohlcv?days=365
```

「全 A 每日成交额」曲线：X 轴交易日、Y 轴亿元，叠加 20 日均值；页面支持 3M/6M/1Y/3Y/全部。

**已处理的坑**：clean 日线里混有 `000001.SH` 等指数代码；汇总成交额时按
`CN_EQUITY_PREFIXES` 过滤，否则 2026-03 等日期会出现 20 万亿级假尖峰。

## 集合竞价 / 指数分时

- 指数分时走腾讯 `web.ifzq.gtimg.cn/appstock/app/minute/query`，页面默认「分时」tab，30 秒刷新；开盘前只有开盘快照时显示圆点，连续竞价后自动铺线。
- 集合竞价首选东财 `push2.eastmoney.com`；本机代理和直连都可能被拒绝时，先复用当日归档，再用腾讯当前快照降级，不再把 `ProxyError` 原样抛给页面。
- 腾讯快照只代表“当前竞价指示价”，不冒充 09:15–09:24 的逐分钟历史；页面会标注来源/降级状态。

## 口径边界

- 指数 K 线只用于市场温度，不是可交易标的，也不进入个股策略池。
- 指数模式复用 `KlineChart` 的 `simple` 模式：保留 OHLCV/均线/成交量，隐藏入选记号、筹码和威科夫。
- 若指数已更新但成交额文件落后，页面显示 `截至 <turnover_asof>`，不把旧成交额冒充当日值。
