# A 股数据链路与回测完整性设计

## Goal

补齐 A 股数据接入、元数据刷新、行业/估值/财务覆盖与回测链路检查，让本地仓库中的 CN 数据可以被因子、验证、LightGBM 和 TopN 回测按 `market="CN"` 读取。

## Current Gaps

- A 股已有单股同步入口，但没有批量股票池、批量同步、行业补全、估值/财务刷新和完整性检查命令。
- 分析链路中 `StockAnalyzer.get_all_stocks()`、`load_stock_data_batch()`、因子上下文、验证、LightGBM 和市场过滤多处写死 `HK`。
- BaoStock 已安装但未接入 provider；AkShare 只承担当前 A 股日线/分钟线和 spot 信息。

## Architecture

- 新增 BaoStock provider：统一 login/logout、A 股股票池、日线 OHLCV、基础信息、行业、财务指标。
- 保留 AkShare 作为分钟线和 spot 补充源，CN 日线默认优先 BaoStock，失败后回退 AkShare。
- 在 `MarketDataService` 中新增 CN 批量同步、信息刷新、行业补全、财务刷新和 `cn_backtest_coverage_report()`。
- 将 `StockAnalyzer` 和核心 mixin 的市场参数改为实例级配置，默认仍是 HK，避免破坏现有港股命令。
- 新增 `run.py` CN 子命令：`sync-cn`、`refresh-cn-stock-info`、`backfill-cn-industry`、`refresh-cn-financial-metrics`、`cn-coverage-check`。

## Data Coverage

第一版覆盖：

- OHLCV：daily 使用 BaoStock/AKShare，分钟线沿用 AkShare。
- Universe：BaoStock `query_all_stock`，失败回退 AkShare `stock_info_a_code_name`。
- Stock info：AkShare spot + BaoStock basic/industry 合并。
- Industry：BaoStock `query_stock_industry` 写入 `industry_l1/l2/source/updated_at`。
- Valuation：从 `stock_info_registry` 生成 `valuation_snapshot`。
- Financial metrics：从 BaoStock profit/operation/growth/balance/cashflow 接口尽量填充统一财务字段，缺失字段保持空值并由 coverage report 暴露。

## Backtest Readiness Check

`cn_backtest_coverage_report()` 检查：

- A 股股票池数量。
- daily/qfq OHLCV 股票覆盖、行数、最近交易日。
- `stock_info_registry` 关键字段覆盖。
- 行业字段覆盖。
- `valuation_snapshot` 和 `financial_statement_metrics` 覆盖。
- feature 层是否已有 CN 因子。
- `StockAnalyzer(market="CN")` 能否读到样本 OHLCV。

报告返回 `backtest_ready` 和 `blocking_reasons`，CLI 可 JSON 输出或人类可读输出。

## Testing

- Provider 级测试使用 monkeypatch 的 fake BaoStock 模块，不访问网络。
- Service 级测试使用临时数据目录和 fake provider 数据，验证 CN OHLCV、stock_info、industry、valuation、coverage report。
- Analyzer 测试验证 `market="CN"` 时读取 CN 分区，而不是 HK 分区。

