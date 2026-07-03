# 财务因子覆盖 TODO

> 目标：明确日常 5 步流水线中财务数据的下载、落库、使用位置，以及下一步需要补齐的财务因子。

## 结论

当前 5 步流水线里，财务/估值/流动性数据已经开始参与，但覆盖还不完整：

| 步骤 | 是否参与财务数据 | 当前作用 |
|---|---|---|
| `sync` | 间接参与 | 主要同步 OHLCV；单股同步路径可能带基础 `stock_info`，但全量财务快照不应依赖它。 |
| `refresh-stock-info` | 是，负责下载和落库 | 将 PB/PE、市值、成交额、成交量、换手率、股本、股息率等写入 `stock_info_registry`。 |
| `backfill-industry` | 间接参与 | 主要补行业、标的类型、可交易性；会保留已有财务/流动性字段，避免覆盖为空。 |
| `generate-factors --factor-set alpha_zoo_hk` | 少量参与 | 主要生成价量技术因子；若 `total_shares` 存在，会生成 `turnover_rate`；`market_cap/PE/PB` 当前不作为模型特征入库。 |
| `select --analysis-mode lightgbm` | 是，实际消费 | 使用市值/成交额做硬过滤，使用市值做 size 中性化，使用 PE/PB/股息率做行业估值评分；质量分当前仍是中性分。 |

也就是说：目前“财务数据落库”已经有入口；“估值和流动性”已经被选股消费；“盈利质量、成长、杠杆、安全、现金流、TTM/报告期 PIT 财务因子”还没有系统接入。

## 当前已用字段

### 已落库字段

`stock_info_registry` 当前包含：

| 字段 | 当前来源/处理 | 当前用途 |
|---|---|---|
| `market_cap` | AkShare/Tencent 基础行情快照 | 市值硬过滤、LightGBM size 中性化、输出覆盖率 |
| `pe_ratio` | AkShare/Tencent 基础行情快照 | 行业估值评分、输出 PE |
| `pb_ratio` | AkShare/Tencent 基础行情快照 | 行业估值评分、输出 PB |
| `dividend_yield` | AkShare/Tencent 基础行情快照 | 估值 payload 已接入；行业偏好中可用于公用事业/电信等 |
| `volume` | 基础行情快照/OHLCV | 换手率兜底计算 |
| `amount` | 基础行情快照 | `daily_turnover` 兜底 |
| `daily_turnover` | 基础行情快照；缺失时用 `amount` | 成交额硬过滤 |
| `turnover_rate` | 原始字段优先；缺失时 `volume / circulating_shares * 100` | 已落库；过滤报告可读，但尚未作为硬过滤参数 |
| `total_shares` | 基础行情快照 | `alpha158_hk` 的 `turnover_rate` 技术流动性因子 |
| `circulating_shares` | 基础行情快照 | 换手率兜底计算 |

### 已进入 LightGBM/选股逻辑

| 位置 | 使用字段 | 说明 |
|---|---|---|
| 市场硬过滤 | `market_cap`, `daily_turnover` | `--min-market-cap 30`、`--min-daily-turnover 500`。成交额参数单位是万港元，内部转 HKD。 |
| 特征面板附加 | `market_cap`, `log_market_cap`, `total_shares` | `market_cap/log_market_cap` 作为元数据和中性化控制；`total_shares` 进入因子转换前的 OHLCV frame。 |
| `alpha158_hk` | `total_shares` | 生成 `turnover_rate = volume / total_shares * 100`；其他财务字段未进入因子矩阵。 |
| 行业估值评分 | `pe_ratio`, `pb_ratio`, `dividend_yield` | `select` 阶段读取本地 `stock_info_registry`，行业内排序后输出 `valuation_score` 等字段。 |
| 组合/入选硬门禁 | `pe_ratio`, `pb_ratio`, `quality_data_coverage` | portfolio builder 已有极端估值和质量覆盖门禁，但质量覆盖目前多为中性/缺失。 |
| 执行成本/TCA | `daily_turnover`, `market_cap` | 成本模型可用成交额或市值做流动性/冲击代理。 |

## 因子库参考 Alpha Zoo 形态

Vibe-Trading `README_zh.md` 里的 Alpha Zoo 描述只作为因子目录、manifest、横评命令和覆盖口径的参考，不作为运行时依赖。本项目的因子、公式代理、数据落库和 CLI 都必须在 `stock_analysis_by_gpt` 内原生实现；不能 import、读取或要求存在 `Vibe-Trading/` 目录。

它内置 4 组共 452 个预置 alpha，并提供 `alpha list/show/bench/compare/export-manifest` 这类浏览、解释、横评和导出能力。落地财务因子前，建议先把本项目的“因子库底座”补齐，否则后续会出现两个问题：

1. 财务因子加进来了，但和现有价量因子混在一起，缺少统一目录、来源、版本和覆盖率。
2. 模型效果变差时，无法判断是财务数据质量问题、因子公式问题，还是因子冗余/失效问题。

### Alpha Zoo 覆盖对照

| 因子族 | Vibe-Trading 数量 | 本项目现状 | 缺口判断 | 优先级 |
|---|---:|---|---|---|
| `qlib158` | 154 | 已有 `qlib_alpha158`，并通过 `alpha158_hk` 汇入生产默认 `alpha_zoo_hk` | 基本具备；需要补 manifest、公式说明、字段依赖和 IC 状态 | P0 |
| `alpha101` | 101 | 已新增本项目原生 `alpha101` | 20 个 exact-style 公式 + 81 个 compatible proxy；仍需 panel 级横截面 rank 精确化 | P0 |
| `gtja191` | 191 | 已有 `gtja_alpha191`，并入 `alpha158_hk` | 只有 42 个代表公式近似实现，剩余 149 个是确定性 GTJA 风格代理；横截面 `RANK` 当前用单股滚动分位代理 | P0 |
| `academic` | 6 | 已新增 `academic_hk` | 当前是价格/成交量代理，后续可用 PIT 财务字段替换 quality/value/investment 组件 | P1 |
| HK custom | Vibe 未单列 | 已有 9 个港股定制因子 | `pb_ratio_sector_relative` 目前实际是价格区间位置代理，不是真 PB 行业内相对估值 | P0 |
| 财务/基本面 PIT | Vibe Alpha Zoo 主要是价量和学术代理 | 已新增 `valuation_snapshot`、`financial_statement_metrics`、`valuation_hk`、`financial_quality_hk`、`financial_cross_section_hk` | 估值快照和财务质量/行业分位已进入默认 `alpha_zoo_hk`；真实财报下载源仍需继续增强 | P0 |

按现有实现粗算，生产默认 `alpha_zoo_hk` 约等于 `alpha158_hk(393) + alpha101(101) + academic_hk(6) + valuation_hk(12) + financial_quality_hk(17) + financial_cross_section_hk(11)`，表面数量约 540 个。但这里的 540 不能直接等同于“高质量 540 个因子”，因为 GTJA191/Alpha101 中仍有代理公式，且缺少跨股票横截面算子和因子状态评估。

### 当前最缺的因子族

| 缺失项 | 应补内容 | 为什么先补 |
|---|---|---|
| Alpha101 | `alpha101_001` 到 `alpha101_101`；保留原始公式、标准字段依赖、是否需要横截面 rank/industry neutralize | Alpha101 是常见价量公式库，和 Alpha158/GTJA 风格互补；补上后因子 zoo 才接近主流基准 |
| Academic 6 | `market_beta_proxy`, `size`, `value`, `profitability/quality`, `investment/conservative`, `momentum`，或按 FF5 + Carhart 命名 | 这些因子可作为模型解释、风格暴露、组合约束和风险归因的基准层 |
| GTJA191 精确化 | 将当前 `gtja_alpha191` 标记为 `gtja_alpha191_proxy`；新增未来的 `gtja_alpha191_panel` 精确版 | 现在单股物化无法表达真正横截面 `RANK`，需要 panel 级计算才能和研报公式更一致 |
| 真实估值因子 | `pe_ind_pct`, `pb_ind_pct`, `ps_ind_pct`, `ev_ebitda_ind_pct`, `dividend_yield_ind_pct`, `fcf_yield` | 解决 `pb_ratio_sector_relative` 名不副实的问题，并让 valuation score 可训练、可诊断 |
| 真实质量因子 | ROE、ROA、毛利率、净利率、经营利润率、OCF/净利润、资产周转率 | 当前 `quality_score` 基本中性，LightGBM 缺少基本面质量信息 |
| 成长/安全/现金流 | 收入同比、利润同比、EPS 同比、资产负债率、流动比率、利息保障倍数、现金短债比、自由现金流 | 这些是财务因子真正区别于价量 alpha 的部分，必须 PIT 化落库 |

### 因子引擎需要补的通用能力

Alpha101、精确 GTJA191 和部分 academic 因子都不能只靠“单只股票 OHLCV rolling transform”表达，因子引擎需要补 panel 级算子：

| 能力 | 典型用途 | 当前风险 |
|---|---|---|
| 横截面 `RANK(x)` | Alpha101、GTJA191、行业分位估值 | 当前用单股时序分位做代理，会改变因子含义 |
| `SCALE(x)` / 标准化 | Alpha101 权重归一、组合信号 | 缺统一实现会导致不同因子尺度不可比 |
| `INDNEUTRALIZE(x, industry)` | 行业内中性化、行业内比较 | 当前更多依赖 select 阶段处理，因子层不可复用 |
| `DECAYLINEAR` / `TS_RANK` / `SIGNEDPOWER` | Alpha101/GTJA 公式表达 | 部分已有，但需要统一到公式 DSL/manifest |
| rolling corr/cov/beta | Alpha101、academic beta、风险暴露 | 已有部分时序 corr，需要 panel 输出和覆盖率诊断 |
| `advN` / `vwap` / `amount` | Alpha101 常用成交额与均量字段 | 当前 VWAP 多为 OHLC4 代理，amount/advN 需要从 clean layer 标准化 |

### 因子库落地 TODO

#### P0.0 建立因子目录和 manifest

- [x] 新增 `factor-list`：列出全部注册因子集、因子数量、来源、版本、是否生产默认。
- [x] 新增 `factor-show <factor_id>`：展示公式、字段依赖、数据频率、是否 PIT、是否横截面计算、已知假设。
- [x] 新增 `factor-manifest --factor-set ...`：导出 JSON/CSV manifest，包含 `factor_id/family/source/formula/status/exactness/input_fields/lookback/notes`。
- [ ] 给现有 `qlib_alpha158`、`gtja_alpha191`、`alpha158_hk` 补 manifest；明确 GTJA 的 `exact_style_formula_ids` 和 `proxy_formula_count`。
- [ ] 在 README 中说明生产默认因子集和轻量回退因子集，不再只靠 `--factor-set` 名字猜语义。

#### P0.1 补 Alpha101 因子集

- [x] 新增 `factor_engine/expressions/alpha101.py`。
- [x] 注册 `alpha101` 因子集，命名统一为 `ALPHA101_001` 到 `ALPHA101_101`。
- [ ] 先实现可在单股时序层表达的公式；需要横截面的公式标记为 `requires_panel=true`，由 panel 引擎补算。
- [x] 增加单测：字段齐全、缺 VWAP/amount fallback、无未来函数、输出列数 101、manifest 完整。

#### P0.2 梳理 GTJA191 代理和精确版本

- [ ] 将当前 `gtja_alpha191` 的文档语义标为 `gtja_alpha191_proxy`，避免误以为 191 个全是原研报精确公式。
- [ ] 保留向后兼容别名 `gtja_alpha191`，但 metadata 增加 `exactness=proxy_mixed`。
- [ ] 新增 `gtja_alpha191_panel` 设计任务：支持真正横截面 rank、行业中性化、全市场同日计算。
- [ ] `alpha158_hk` metadata 中直接暴露 `gtja_proxy_formula_count`，并在 factor report 输出。

#### P0.3 补 HK 真实估值/流动性因子

- [ ] 把 `pb_ratio_sector_relative` 重命名或废弃为 `price_position_range_52w`，避免和 PB 混淆。
- [x] 新增基于 `valuation_snapshot` 的 `valuation_hk` 因子集：PE/PB/PS/EV_EBITDA/股息率/FCF yield 的行业分位、z-score、缺失标记。
- [x] 新增流动性因子：`turnover_rate`, `amount_ma20`, `amount_ma60`, `free_float_turnover`, `amihud_illiq`, `capacity_score`。
- [x] 所有估值和流动性字段优先来自 ClickHouse/Parquet 落库，不在选股时临时下载。

#### P1.1 补 Academic 6 因子集

- [x] 新增 `academic_hk`：市场 beta、size、value、momentum、profitability/quality、investment/conservative 的价格或财务代理。
- [x] 明确哪些是纯价格代理，哪些依赖财务 PIT 面板。
- [ ] 在组合层输出风格暴露，辅助判断 LightGBM 是否只是买小盘/低 PB/强动量。

#### P1.2 建立 Alpha Zoo bundle 和横评

- [x] 新增 `alpha_zoo_hk` bundle：`qlib_alpha158 + alpha101 + gtja_alpha191_proxy + academic_hk + hk_custom + valuation_hk + financial_quality_hk + financial_cross_section_hk`。
- [ ] 新增 `factor-bench`：按 IC、RankIC、ICIR、覆盖率、换手、行业稳定性、alive/reversed/dead 分类评估。
- [ ] 新增 `factor-compare`：只对指定因子子集横评，避免每次全量跑 400+ 因子。
- [ ] 新增冗余诊断：因子相关矩阵、按 family 聚类、LightGBM importance 稳定性。

### 和财务因子落地的顺序关系

先补因子库不是要推迟财务数据，而是要把财务因子接到正确的位置：

1. 先做 `factor-manifest` 和 `valuation_hk`，修正现有估值字段命名和口径。
2. 同步补 Alpha101/GTJA proxy 标识，让当前 400+ 因子的覆盖状态可解释。
3. 再落 `financial_statement_metrics` 和 `valuation_snapshot`，保证财务字段进入因子库时天然带 PIT、来源、覆盖率和缺失标记。
4. 最后把 `financial_quality_hk`、`financial_growth_hk`、`financial_safety_hk` 合进生产候选 bundle，而不是直接塞进 `select` 阶段临时算。

## 财务数据当前缺口

### P0：必须补齐，直接影响当前选股质量

| 缺口 | 为什么重要 | 建议字段 |
|---|---|---|
| 财务质量原始数据未落库 | 目前 `select` 里质量评分跳过 live 抓取，`quality_score` 基本中性，无法区分高质量公司和财务较差公司。 | ROE、ROA、毛利率、净利率、经营利润率、经营现金流/资产、资产负债率、流动比率、利息保障倍数 |
| TTM / 报告期口径缺失 | PE/PB 是快照，但盈利、成长、杠杆必须有报告期和可得日期，否则容易未来函数。 | `report_date`, `announce_date`, `available_at`, `period_type`, `ttm_flag` |
| `ps_ratio` / `ev_ebitda` 未落库 | 行业估值代码已支持 PS/EV_EBITDA，但 stock info schema 不存这些字段，软件、生物科技、能源等行业估值会退回 PE/PB。 | `ps_ratio`, `ev_ebitda`, `ev`, `ebitda_ttm`, `revenue_ttm` |
| 自由流通股/流通市值缺失 | 换手率和容量应优先用 free float 或 circulating market cap，而不是总股本。 | `free_float_shares`, `free_float_market_cap`, `circulating_market_cap` |
| 财务覆盖率诊断不足 | 现在能输出估值覆盖，但没有专门命令判断全市场财务字段覆盖率。 | coverage report by field/industry/source/date |

### P1：应补齐，提升模型解释和稳定性

| 缺口 | 建议字段/因子 | 用途 |
|---|---|---|
| 成长因子 | 收入同比、净利同比、EPS 同比、毛利率变化、ROE 变化 | 行业内 growth score、质量增长维度 |
| 安全/杠杆因子 | 资产负债率、净负债率、流动比率、现金短债比、利息保障倍数 | 排除高杠杆/现金流紧张标的 |
| 现金流因子 | 经营现金流、自由现金流、OCF/净利润、FCF yield | 避免只看利润不看现金流 |
| 股东回报 | 派息率、股息率、回购、净增发 | payout quality、红利/价值风格 |
| 盈利预期/一致预期 | 预期 PE、预期 EPS 增速、盈利修正 | 如果有可靠数据源，作为增强特征而不是硬依赖 |

### P2：长期增强

| 缺口 | 用途 |
|---|---|
| 分红、拆股、供股、配售等企业行为的完整 PIT 回放 | 准确复权、红利收益、股本变化 |
| 季度/半年/年度财务面板 | 财务因子时序化，支持 point-in-time 回测 |
| 行业专属财务指标 | 银行 NIM/不良率/资本充足率，保险 EV/NBV，地产 NAV/负债结构，能源储量/单位成本等 |
| 审计意见、停牌、监管问询、财报延迟 | 风险事件因子 |

## 建议数据模型

不要继续把所有财务数据都塞进 `stock_info_registry`。建议拆三层：

| 表/数据集 | 粒度 | 作用 |
|---|---|---|
| `stock_info_registry` | 单股票最新快照 | 名称、行业、标的类型、最新市值/PE/PB/成交额等轻量快照 |
| `financial_statement_metrics` | `stock_code + report_date + available_at` | 利润表、资产负债表、现金流和派生质量/成长/安全指标 |
| `valuation_snapshot` | `stock_code + trade_date` | PE/PB/PS/EV/EBITDA、股息率、市值、流通市值等日频估值快照 |

所有用于训练和回测的财务字段必须带 `available_at`，并在生成特征时只使用 `available_at <= trade_date` 的数据。

## 接入 TODO

### P0.1 扩展 schema 和 ClickHouse 表

- [x] 新增 `financial_statement_metrics` schema。
- [x] 新增 `valuation_snapshot` schema。
- [x] ClickHouse DDL 增加对应表，Parquet fallback 同步支持。
- [x] 增加字段覆盖率检查：按字段、行业、数据源、报告期统计。

### P0.2 下载和落库

- [x] 新增 `refresh-financial-metrics` 命令，区别于 `refresh-stock-info` 的最新快照。
- [x] 港股优先接东方财富 F10 / AkShare 可得字段：ROE、ROA、毛利率、净利率、收入同比、利润同比、资产负债率、流动比率、OCF/净利润。
- [ ] 保留原始字段名和标准字段名，避免数据源字段变化时不可追踪。
- [x] 保存 `report_date / announce_date / available_at / source / ingest_time`。

### P0.3 生成财务因子

- [x] 新增 `financial_quality_hk` 因子集或并入 `alpha158_hk` 的财务扩展层。
- [x] 生成行业内标准化财务质量因子：`roe_ind_pct`, `gross_margin_ind_pct`, `debt_ratio_ind_pct`, `revenue_yoy_ind_pct`。
- [x] 生成估值因子：`pe_ind_pct`, `pb_ind_pct`, `ps_ind_pct`, `ev_ebitda_ind_pct`, `dividend_yield_ind_pct`。
- [x] 生成组合特征：`quality_value_score`, `growth_quality_score`, `financial_coverage_score`。

### P0.4 选股阶段使用

- [x] `select` 阶段不再用空 `quality_raw = {}`，改为读取本地 `financial_statement_metrics`。
- [x] `quality_score`、`quality_data_coverage`、`quality_missing_fields` 来自本地 PIT 财务数据。
- [ ] 财务覆盖不足时进入 `watchlist` 或降低 eligibility，而不是默认为高质量。
- [ ] 极端估值门禁使用行业分位 + 原始绝对阈值双规则。

### P1.1 文档和诊断

- [x] README 日常流程加入 `refresh-financial-metrics`。
- [x] 新增 `financial-coverage` 命令，输出全市场财务覆盖。
- [x] `select` 导出的 ranking CSV 增加财务字段覆盖列和原始关键字段。
- [x] 添加单测：无未来函数、报告期对齐、缺字段 fallback、ClickHouse/Parquet 一致性。

## 5 步流水线建议目标形态

```bash
uv run python run.py sync --start-date 2014-01-01 --frequencies daily --skip-existing --max-workers 24 --show-progress
uv run python run.py refresh-stock-info --max-workers 16 --show-progress
uv run python run.py refresh-financial-metrics --max-workers 8 --show-progress
uv run python run.py backfill-industry --force --normalize-existing --max-workers 8 --show-progress
uv run python run.py generate-factors --days 365 --factor-set alpha_zoo_hk --max-workers 8 --show-progress
uv run python run.py select --analysis-mode lightgbm --factor-set alpha_zoo_hk --export-csv output/results --show-progress
```

`refresh-stock-info` 继续负责“最新交易快照”；`refresh-financial-metrics` 负责“报告期财务面板”。二者不要混成一个临时下载步骤。
