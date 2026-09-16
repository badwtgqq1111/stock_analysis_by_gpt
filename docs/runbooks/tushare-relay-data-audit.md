# Tushare 中继数据审计

审计日期：2026-09-15。数据中心文档描述两条 Tushare-compatible 网关：基础网关与 ProMax 网关。调用密钥只允许通过环境变量注入，不能写入配置文件、报告或代码库。

## 已验证的行业数据

| 能力 | 基础网关 | ProMax | 返回字段 | 结论 |
| --- | --- | --- | --- | --- |
| `index_classify(src=SW2021, level=L1)` | HTTP 200，5 行样本 | HTTP 200，缓存命中时可用 | `index_code, industry_name, industry_code, level, parent_code, src` | 可取得申万 2021 行业目录 |
| `index_member_all(ts_code=000001.SZ, is_new=Y)` | HTTP 200 | HTTP 200 | `l1/l2/l3_code`, `l1/l2/l3_name`, `ts_code`, `name`, `in_date`, `out_date`, `is_new` | 可取得个股三级行业归属及当前成员起止字段 |
| `index_member_all(l1_code=801780.SI, is_new=Y)` | HTTP 200 | 本次为 503 上游池耗尽 | 同上 | 基础网关可按一级行业批量取得当前成分 |
| `get_industries(level=L1, src=SW2021)` | 不适用 | HTTP 200 | `industry_code, industry_name, level, src` | ProMax 本地聚合目录可用 |
| `get_industry_stocks(industry_code=801010.SI)` | 不适用 | 本次为 502 上游池耗尽 | - | 不能作为唯一生产链路 |

验证样本中，`000001.SZ` 与 `600000.SH` 均返回 `银行 -> 股份制银行Ⅱ -> 股份制银行Ⅲ`；字段含 `in_date`，但 `is_new=N` 的历史查询本次为空。因此目前能证明的是**当前截面**，不能把历史成分变更当成已覆盖数据。

## 已接入的实现

`data.ingest.providers.cn_sw2021_relay.SW2021RelayIndustryFetcher` 使用：

1. `CN_INDUSTRY_RELAY_BASE_KEY` 调用基础网关；
2. 单次失败后以 `TUSHARE_RELAY_KEY` 调用 ProMax；
3. 先请求 `index_classify`，再逐个一级行业请求 `index_member_all`；
4. 输出 `industry_l1/l2/l3`、三级代码、`effective_from/effective_to`、`is_current` 与获取网关；
5. 任一一级行业不完整时拒绝写注册表，避免静默形成残缺全市场映射；
6. 完整结果保存到 `assets/data/raw/industry_snapshots/market=CN/taxonomy=sw2021_relay/`。

默认配置仍为 `taxonomy = "csrc_baostock"`，当前覆盖率为 97.6061%（5,219 / 5,347）。若要同步申万 2021，设置：

```bash
export CN_INDUSTRY_RELAY_BASE_KEY='...'
# 可选：ProMax 作为回退
export TUSHARE_RELAY_KEY='...'

# config/cn_pipeline.toml
# [industry]
# taxonomy = "sw2021_relay"

uv run python scripts/run_cn_pipeline.py --stage fundamental
```

申万和证监会分类不可混用。应将申万快照作为独立 taxonomy，只有在获得历史成员变更的稳定返回、补齐 `effective_to` 后，才可生成 `stock_code + taxonomy + effective_from/effective_to` 的 PIT 映射，并用于行业中性化、行业相对动量和图模型。

## 当前数据缺口与中继可补数据

| 优先级 | 中继接口 | 当前系统状态 | 建议落库与用途 |
| --- | --- | --- | --- |
| P0 | `adj_factor` | **已接入，基础网关实测 HTTP 200** | 原始复权因子时间序列保存在 `adjustment_factor_snapshots`；复权重算与停复牌校验 |
| P0 | `daily_basic` | **已接入，基础网关实测 HTTP 200** | `pe/pb/ps/dv/total_mv/circ_mv/turnover_rate` 写入 `valuation_snapshot`；用非空合并保留其他来源的成交额/成交量 |
| P0 | `trade_cal`, `suspend_d`, `limit_list_d`, `namechange` | `limit_list_d`、`namechange` 已实测 HTTP 200；`suspend_d` 本次为空但契约有效 | 可交易性、幸存者偏差、事件过滤 |
| P1 | `moneyflow`, `top_list`, `hk_hold`, `margin` | 未接入 | 资金流、龙虎榜、北向持仓、两融风险特征；必须使用交易日可得时间 |
| P1 | `income`, `balancesheet`, `cashflow`, `fina_indicator` | 财务源较弱且覆盖有限 | 按 `ann_date` 写 PIT 财务事实表；质量、盈利、成长因子 |
| P1 | `index_weight` | 未接入 PIT 权重 | 沪深 300、中证 500 等历史成分与权重；基准、股票池和中性化 |
| P2 | `a_share_mins`, `stk_mins` | 现有分钟数据源为腾讯 | 成交执行、日内波动和 VWAP 特征；按日期和代码分片 |
| P2 | `cyq_perf`, `cyq_chips`, `stk_factor_pro`, `report_rc` | 未接入 | 筹码、技术指标、分析师预测；先做样本覆盖率与前瞻性审计 |
| P2 | `share_float`, `pledge_detail`, `repurchase`, `block_trade` | 未接入 | 供给冲击、质押、回购、大宗交易事件风险 |

### 已补入 daily_basic

`CNDailyBasicRelayFetcher` 使用基础网关优先、ProMax 回退，读取单股票 `daily_basic` 后写入 `valuation_snapshot`。已验证字段为 `close, turnover_rate, turnover_rate_f, volume_ratio, pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm, total_share, float_share, free_share, total_mv, circ_mv`；其中 `turnover_rate_f` 和 `volume_ratio` 分别保存为 `free_turnover_rate` 与 `volume_ratio`。Tushare 的股本和市值单位为万元，写入前统一换算为元/股。系统保留原始行情的成交量和成交额，不用缺失值覆盖。

在 [cn_pipeline.toml](/Users/ccs/code/quant/stock_analysis_by_gpt/config/cn_pipeline.toml) 设置 `relay_data.enabled = true` 后，`fundamental` 阶段会同步它。先以小股票池和短日期窗运行并核对覆盖率，再开启全量历史。

### 已补入 adj_factor

`CNAdjustmentFactorRelayFetcher` 已验证返回 `ts_code, trade_date, adj_factor`，并写入 `assets/data/raw/adjustment_factor_snapshots/market=CN/source=tushare_relay/`，带 `source` 与 `retrieved_at`。这是原始审计层，不改变当前已入库的 `qfq` OHLCV；只有在逐股票对齐、除权日跳变和缺失率检查通过后，才可用它重建复权价格。设置 `relay_data.adjustment_factors_enabled = true` 启用同步。

真实请求复核（`000001.SZ`，2026-08-01 至 2026-08-31，`limit=5`）：`daily_basic` 返回 5 行，首行日期 2026-08-31，`pe_ttm=5.2334`、`turnover_rate_f=1.1137`、`volume_ratio=0.88`；`adj_factor` 返回 5 行，首行 `adj_factor=139.008`。两项均由基础网关返回 HTTP 200。

接入顺序应是 `daily_basic + adj_factor + suspend_d/limit_list_d`，然后是以公告日为准的财务数据和指数 PIT 数据。每一张新表必须记录 `source`、`retrieved_at`、业务可得时间，并按全市场覆盖率、日期连续性、重复率和修订率设门禁。
