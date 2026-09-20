# P1.15 个股资金流与龙虎榜：数据接入、特征工程和信号强化综合方案

> 状态：TODO / 研究与工程设计
> 关联：`docs/runbooks/cn-data-pipeline.md`、`docs/todo/P0_11_data_cleaning_feature_panel_plan.md`
> 目标：把“每日资金流与日 K 同步”纳入 A 股生产链，并用严格 PIT/OOS 实验判断是否提升 LightGBM、Transformer 和现有 signal。

## 1. 结论与推荐路线

### 1.1 不新增独立调度器，新增 `moneyflow` 数据阶段

建议在现有 `daily_bars → features` 之间加入可选但生产默认开启的 `moneyflow` 阶段：

```text
daily_bars
  → moneyflow（moneyflow + moneyflow_dc/ths + top_list/top_inst）
  → features（资金流特征与质量标记）
  → regime → clean_panel
  → model_scores → preselection → pk
```

原因：

- 资金流的主键是 `stock_code + trade_date`，与日 K 天然对齐；不应放入 `fundamental`（披露节奏不同）。
- 先落原始审计层，再由 `features` 生成模型字段，可独立重试、回溯和做版本化。
- `top_list/top_inst` 是稀疏事件数据，与每日连续 `moneyflow` 同阶段采集，但在特征层用事件/缺失标记区分。
- 保留旧 `selection` 兼容入口；新流程继续使用 `preselection → pk`。

推荐命令：

```bash
uv run python scripts/run_cn_pipeline.py --stage moneyflow
```

每日生产脚本 `scripts/run_daily_production.sh` 的阶段顺序增加 `moneyflow`，放在 `daily_bars` 后、`features` 前；首次历史回补可单独运行，不触发训练。

### 1.2 配置草案

在 `config/cn_pipeline.toml` 增加：

```toml
[stages]
moneyflow = true

[moneyflow]
enabled = true
start_date = ""                 # 空值跟随 pipeline.start_date
end_date = ""                   # 空值跟随 pipeline.end_date/最新交易日
lookback_days = 756
providers = ["base", "promax"]
fetch_standard = true            # Tushare moneyflow
fetch_dc = true                  # moneyflow_dc
fetch_ths = true                 # moneyflow_ths
fetch_top_list = true
fetch_top_inst = true
batch_years = 1                  # 单股票请求按年分段，避免 date_range_too_large
page_limit = 5000
max_workers = 4
retry = 3
retry_backoff_seconds = 2
require_daily_bar_match = true
min_match_ratio = 0.98
raw_dir = "assets/data/raw/moneyflow_snapshots"
feature_path = "assets/data/derived/cn_moneyflow_features.parquet"

[moneyflow.features]
windows = [3, 5, 10, 20, 60]
cs_rank = true
industry_neutral = false         # 行业 PIT 映射完善后再开启
winsorize_quantiles = [0.01, 0.99]

[selection.signals.moneyflow_confirmation]
enabled = false                  # OOS 证明增益后再打开
min_score = 0.6
max_weight_boost = 0.15

[model_scores]
# moneyflow 特征先进入 clean_panel；模型是否使用由 feature allowlist 控制

## 2. 数据接口、覆盖和同步契约

### 2.1 接口清单

| 接口 | 粒度/用途 | 关键字段 | 生产策略 |
|---|---|---|---|
| `moneyflow` | 个股每日大/中/小/特大单 | `buy_*_vol/amount`、`sell_*_vol/amount`、`net_mf_vol/amount` | 主表；按股票+年度分段 |
| `moneyflow_dc` | 个股每日东方财富口径 | `net_amount`、`buy_elg/lg/md/sm_amount` 及占比 | 作为独立来源，不与主表静默覆盖 |
| `moneyflow_ths` | 个股每日同花顺口径 | `net_amount`、`net_d5_amount`、大/中/小单 | 独立来源；用于稳健性/共识特征 |
| `top_list` | 龙虎榜每日上榜明细 | `ts_code`、`reason`、`net_amount`、`amount` | 按交易日全市场分页；稀疏事件 |
| `top_inst` | 龙虎榜营业部/机构席位 | `exalter`、`buy`、`sell`、`net_buy`、`side` | 按交易日全市场分页；机构行为事件 |

金额单位、来源、请求时间和原始响应必须保留；不同供应商口径不得直接相加。`moneyflow` 文档显示 2010 年以来个股通常有连续交易日记录，但 2005—2009 年及部分替代接口存在空档；覆盖率必须逐股票、逐年报告。

### 2.2 与日 K 一致的可得性规则

每一条资金流记录必须满足：

```text
market=CN
stock_code=规范化 TS 代码
trade_date=交易日
available_at=当日收盘后数据实际可得时间（默认次日 00:00，实测源可得时间优先）
```

连接日 K 使用内连接候选集 `stock_code + trade_date`，并输出：

- `has_daily_bar`
- `has_moneyflow_standard`
- `has_moneyflow_dc`
- `has_moneyflow_ths`
- `has_top_list`
- `has_top_inst`
- `moneyflow_match_status`：`matched` / `missing_source` / `non_trading_day` / `invalid`

`require_daily_bar_match=true` 时，只有存在合格日 K 的日期才进入特征面板；资金流缺失不能填 0，必须保留 `is_missing` 和来源覆盖率。龙虎榜没有记录代表“未上榜”时可编码为事件 0，但接口失败、未抓取和非交易日必须分开。

### 2.3 拉取与质量门禁

1. 读取 `trade_cal` 或日 K 实际交易日作为日期驱动，不遍历自然日盲猜。
2. 个股历史按 1 年（必要时半年）窗口请求；全市场按 `trade_date + limit/offset` 分页。
3. 基础网关优先，ProMax 回退；遇到 `429` 按 `Retry-After` 退避，`503 upstream_pool_exhausted` 重试并记录。
4. 原始响应按 `source/api/request_params/retrieved_at` 分区保存；写入采用幂等 upsert。
5. 每日检查：主键重复率=0、日期落在日 K、数值非负（净额允许正负）、单位一致、来源交叉相关性、覆盖率和延迟。
6. 质量失败只阻断 `features/model_scores`，不删除已落盘原始数据；报告输出缺失股票/日期示例。

建议门禁：标准 `moneyflow` 与日 K 匹配率 ≥98%；训练窗口内有效股票日数 ≥配置阈值；任何单日全市场缺失超过 20% 则标记 `data_degraded` 并禁止自动开启资金流信号强化。

## 3. 特征工程（先原始、后变换）

### 3.1 单股时间序列特征

对每个 `stock_code`、`trade_date=t`，只使用 `≤t` 的资金流：

- 净额：`net_mf_amount`、`net_mf_vol`；大/中/小/特大单净额及占比。
- 强度：`net_mf_amount / amount`、`(buy_lg+buy_elg - sell_lg-sell_elg) / amount`。
- 趋势：3/5/10/20/60 日滚动和、均值、标准差、连续净流入天数、正流入占比。
- 加速度：`net_t - net_{t-1}`、5 日均值变化、净额相对成交额的 z-score。
- 分层结构：特大/大单占比差、小单逆向指标、大单净流入与收益/成交量的滞后相关。
- 稳定性：滚动符号一致率、最大连续流出、资金流冲击后的 1/5/20 日衰减。

### 3.2 横截面和市场状态特征

- 每个交易日对净流入强度、5/20 日累计流入做 rank、分位数和 winsorize。
- 计算全市场净流入总额、上涨股净流入占比、行业净流入集中度、资金流 breadth、Top-N 集中度。
- 在行业 PIT 映射可用后，增加行业内 rank、行业超额净流入和行业资金扩散速度。
- `moneyflow_dc` 与 `moneyflow_ths` 生成来源间均值、符号一致、差异绝对值和可用来源数；不把口径差异隐藏。

### 3.3 龙虎榜事件特征

以 `stock_code + trade_date` 聚合：

- `top_list_flag`、上榜原因 one-hot/词典编码、上榜次数（1/5/20 日）。
- `top_net_amount`、`top_amount_rate`、净买入/成交额、买卖方向数量。
- `top_inst_flag`、机构/营业部买入卖出额、净买入、买方席位数、卖方席位数。
- 席位重复出现次数、近 20 日席位净买入趋势、龙虎榜后 1/5/20 日收益和最大回撤（仅训练标签构造时使用未来收益，线上特征不得使用）。
- 对 `reason` 建立稳定类别映射并保留原文；新类别映射到 `other`，避免词袋随时间漂移。

龙虎榜特征的缺失语义：`top_list_flag=0` 表示当日确认未上榜；抓取失败使用 `is_missing=1` 且不等于 0。

## 4. 喂给 LightGBM、Transformer 和现有 signal

### 4.1 LightGBM

第一阶段只把资金流特征加入 `clean_feature_panel` 的截面宽表，保留 `*_is_missing`、`*_source_count` 和质量标记。使用训练折内 winsor/标准化参数，特征重要性、SHAP、Permutation importance 和分行业 IC 必须单独报告。

推荐特征组消融：

- `B0`：现有量价/基本面/Alpha 特征；
- `B1`：B0 + 标准 `moneyflow`；
- `B2`：B1 + `moneyflow_dc/ths` 共识；
- `B3`：B2 + 龙虎榜；
- `B4`：B3 + 市场/行业聚合。

只有在滚动 OOS 的 RankIC、分组收益、换手后净收益和最大回撤均改善时才晋升。

### 4.2 Transformer/CNN

把资金流作为与量价同一时间轴的特征通道：

```text
X.shape = [batch, lookback=60, feature]
mask.shape = [batch, lookback, feature]
```

对金额使用 `signed_log1p` 或按成交额归一化；对极端值做训练折 winsorize。龙虎榜事件使用数值特征 + 事件 mask，不把原始营业部名称直接 token 化。保持与现有模型相同的 Purged/Embargo 切分、标签 horizon 和 scaler；不得用未来窗口统计量归一化。

Transformer 先做“特征通道拼接”基线，再比较门控/交叉注意力；若增益只来自事件稀疏性，优先保留 LightGBM 的可解释实现。CNN 仅作为序列局部模式对照。

### 4.3 signal 强化原则

资金流先作为**确认/风险缩放**，而不是硬买入条件：

1. 现有 signal 产生候选；
2. 资金流分数用于 `score_boost`（上限 10%—15%）或 `target_weight` 缩放；
3. 资金流极端流出、数据质量降级、龙虎榜高波动事件可触发减仓/禁入；
4. 信号与资金流冲突时保留原始 signal、资金流解释字段和最终权重，便于回测归因。

候选强化示例：

```text
flow_confirmation = rank(z20_net_flow) + rank(flow_breadth) + 0.5*rank(top_net_amount_rate)
boost = clip(flow_confirmation * 0.05, -0.10, +0.10)
final_score = model_score * (1 + boost)
```

该公式仅作实验起点，必须在 OOS 中学习/校准；不能用同一未来收益调参后回测。

## 5. Jane Street / Jump Trading / 幻方的公开信息边界与借鉴

这些机构的具体生产特征、阈值和代码属于非公开信息，不能声称其使用了某个 Tushare 字段。公开论文、招聘信息和演讲通常支持以下**可迁移方法论**：

- **Jane Street**：强调高质量数据、横截面/时间序列统计、严格回测、交易成本和风险约束；可借鉴“先预测收益，再将流动性/成本作为组合优化约束”，而不是把单一资金流指标当作方向信号。
- **Jump Trading**：公开材料更偏市场微观结构、订单流、盘口和执行；可借鉴把资金流视为交易活动/流动性状态代理，并与成交量、价差、冲击成本联合建模。龙虎榜是日频披露事件，不能等价于实时订单流。
- **幻方量化**：公开访谈强调大规模数据、横截面因子、机器学习和组合风险管理；可借鉴多源特征分层、特征去冗余、滚动 OOS 和容量约束。不能把网络传闻当成其实际字段清单。

研究报告必须把“公开可证据的方法”与“本项目假设”分栏，避免机构归因幻觉。

## 6. 实验矩阵与验收标准

### 6.1 核心实验

| 实验 | 特征 | 模型 | 目的 |
|---|---|---|---|
| E0 | 现有面板 | LGBM/Transformer | 基线复现 |
| E1 | +标准 moneyflow | LGBM/Transformer | 连续资金流增量 |
| E2 | +DC/THS 共识 | LGBM/Transformer | 来源稳健性 |
| E3 | +龙虎榜 | LGBM/Transformer | 稀疏事件增量 |
| E4 | +市场/行业聚合 | LGBM/Transformer | 状态与扩散 |
| E5 | E4 + signal boost | 现有选股/PK | 交易层可实现性 |

每个实验固定股票池、标签、观察期、交易成本、调仓频率和随机种子；使用 expanding/rolling OOS、purge/embargo，按年份和市场状态分层。报告 IC/RankIC/IR、分位数组收益、净收益、换手、容量、最大回撤、MAE/MFE、缺失率和特征漂移。

### 6.2 晋升门槛

- 数据：日 K 对齐率 ≥98%，重复率=0，PIT 检查通过，缺失原因可区分。
- 统计：至少两个独立 OOS 时段 RankIC/净收益改善，且 bootstrap 置信区间不跨 0（或达到预设经济显著性）。
- 交易：扣成本后收益改善，换手/冲击成本不恶化超过阈值，容量约束下仍有效。
- 稳健：去掉任一供应商、去掉龙虎榜、按行业/市值/涨跌停状态分层后结论不反转。
- 生产：每日任务失败可重试；数据降级时自动关闭 boost、保留原模型信号并告警。

## 7. 实施拆分与每日自动化

### 7.0 第一轮落地结果（2026-09-18）

- 已新增 `moneyflow` stage、基础网关/ProMax failover provider、日期分段和 5000 行分页。
- 全市场日期驱动模式已默认开启（`fetch_mode="trade_date"`）：按交易日批量请求，避免 5,349 只股票逐只请求。
- 2026-01-01—2026-09-16 标准资金流回补实测：903,533 行、5,348 只股票、日 K 匹配率 98.6204%，原始快照约 81 MB，滚动特征约 84 MB。
- 使用 `2026-09-16` 截止数据训练 LightGBM 与 Transformer，模型工件分别写入 `output/models/cn/lightgbm/alpha_zoo_hk/` 和 `output/models/cn/transformer/alpha_zoo_hk/`；模型 manifest 已包含 `moneyflow_*` 特征。
- 在 2026-09-16 截面重跑 `preselection → pk`，最终 6 个正权重候选，总目标仓位 46.90%。次日（2026-09-17）收盘相对 9-16 收盘的加权收益约 **+0.1435%**（未扣成本；单日样本不可证明策略有效）。
- `top_list`/`top_inst` 已对 2026-09-16 实测落盘，分别 73/730 行；历史全量龙虎榜仍应单独按交易日批量回补，避免与连续资金流请求混在一个慢任务中。

### 7.1 性能改进

不要用默认的 `stock` 模式对 5,349 只股票逐一调用。生产使用：

```toml
fetch_mode = "trade_date"
fetch_dc = false
fetch_ths = false
```

先以标准 `moneyflow` 完成主链路，再在夜间/周末独立补 DC/THS 与龙虎榜。若服务端返回 `upstream_pool_exhausted`，保留已成功日期并从失败日期断点续跑。

### P1（接入）

- 新增 `moneyflow` stage、Fetcher、原始快照 schema、覆盖率报告和幂等 upsert。
- 在 `run_cn_pipeline.py` 注册命令、依赖和报告项；默认 `all` 在 `daily_bars` 后运行。
- `run_daily_production.sh` 增加阶段及日志；失败时阻断下游特征/打分，保留可重试状态。

### P2（特征）

- 在统一 clean panel 生成资金流/龙虎榜特征与 mask、source_count、available_at。
- 更新 LightGBM/Transformer feature manifest 和训练折 scaler；补单元测试、PIT 测试、日期对齐测试。

### P3（研究）

- 运行 E0—E4 OOS 消融和漂移监控；保存模型、数据、配置版本。
- 仅当 E5 达标后启用 `[selection.signals.moneyflow_confirmation]`，先灰度观察 20/60 个交易日。

### 每日命令矩阵（建议）

```bash
uv run python scripts/run_cn_pipeline.py --stage daily_bars
uv run python scripts/run_cn_pipeline.py --stage moneyflow
uv run python scripts/run_cn_pipeline.py --stage features
uv run python scripts/run_cn_pipeline.py --stage regime
uv run python scripts/run_cn_pipeline.py --stage clean_panel
uv run python scripts/run_cn_pipeline.py --stage model_scores
uv run python scripts/run_cn_pipeline.py --stage preselection --force-rebalance
uv run python scripts/run_cn_pipeline.py --stage pk
uv run python scripts/run_cn_pipeline.py --stage paper_outcomes
```

### 待办验收产物

- `output/pipeline_reports/cn_moneyflow_<timestamp>.json/.md`
- `assets/data/raw/moneyflow_snapshots/` 原始分区
- `assets/data/derived/cn_moneyflow_features.parquet`
- `output/research/moneyflow_ablation_report.{md,json}`
- 更新后的 `clean_feature_panel` manifest、模型 manifest、signal/PK 归因报告
