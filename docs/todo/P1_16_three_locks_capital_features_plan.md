# P1.16 三把锁、主力资金与敢死队资金特征落地方案

> 状态：RAW DATA BACKFILL IN PROGRESS / `daily_basic`、`cyq_perf`、`hm_detail` 已回补；截至 2026-09-19，`cyq_chips` 已完成 2,303 只股票（约 44.1% 有效股票池），仍在三路并行续传。原始表尚未转换为 `capital_flow_locks.v1` 宽特征面板；P1.16 消融训练未开始。
> 日期：2026-09-18
> 关联：`P1_15_moneyflow_plan.md`、`P1_15_moneyflow_evaluation_20260918.md`、`docs/runbooks/cn-data-pipeline.md`
> 目标：用公开可审计数据重建截图中的“三把锁”买点、CYC 成本线、主力资金和敢死队资金代理特征，并接入 clean panel、LightGBM、Transformer、signal 与 PK。

> **数据完整性结论（2026-09-18）**：仅下载标准 `moneyflow` 不足以落地本方案。标准资金流只能覆盖“大/中/小/特大单”主力代理；CYC 成本分布、流通市值归一化、来源共识和龙虎榜席位行为仍缺数据。正式实现必须按本文第 2 节的必需数据包分层下载；缺少任一必需包时只能生成降级的 `moneyflow_only` 影子特征，不能宣称三把锁已复刻。

## 1. 范围与精度边界

### 1.1 可实现范围

当前数据可以重建以下可解释代理：

- `CYC_5/13/34`：基于成交额/成交量的滚动成本/VWAP 代理；
- 主力资金：大单 + 特大单净买入及其成交额/流通市值归一化；
- 敢死队资金：特大单、短期爆发、量比、换手率、涨停/龙虎榜事件的复合代理；
- 三把锁：趋势锁、成本锁、资金锁三个独立状态及总分；
- 三日累计：所有资金流字段的 `3d/5d/10d/20d/60d` 聚合；
- 资金流与价格的背离、持续性、加速度、过热和失败状态。

### 1.2 不宣称精确复刻

截图来自具体行情 App；其“三把锁”“主力资金”“敢死队资金”可能含有未公开的成交单切分、筹码算法、分钟数据或专有阈值。没有原始公式和标签时，本项目只称为：

```text
three_locks_proxy
main_capital_proxy
daredevil_capital_proxy
cyc_cost_proxy
```

验收重点是预测增益、稳定性和可审计性，不是逐点复制 App 图形。

## 2. 数据源与时间可得性

### 2.1 必需数据

| 数据 | 接口/来源 | 用途 |
|---|---|---|
| 日 K | 本地 `ohlcv` | 价格、成交量、成交额、波动率、量比 |
| 标准资金流 | `moneyflow` | 小/中/大/特大单金额和净额 |
| 替代资金流 | `moneyflow_dc`、`moneyflow_ths` | 来源共识、差异和稳健性 |
| 龙虎榜 | `top_list`、`top_inst` | 事件、席位净买入、机构行为 |
| 市值/流通股本 | `daily_basic` 或估值快照 | 流通市值归一化 |
| 筹码（可选） | `cyq_perf`、`cyq_chips` | 进一步逼近 CYC 与成本分布 |

### 2.2 PIT 规则

- 特征行主键：`market + stock_code + trade_date`。
- 只使用 `trade_date` 收盘后已经可获得的数据。
- 资金流数据必须与日 K 交易日内连接；缺失保留 `is_missing`，禁止填 0。
- 龙虎榜“当天未上榜”编码为 `event_flag=0`；抓取失败编码为 `is_missing=1`，两者不可混淆。
- 滚动窗口只能使用当前日及以前数据；不得使用未来分位数、未来行业映射或未来成交统计。
- 训练折内拟合 winsor、rank、z-score 和阈值；验证/测试/线上冻结同一 manifest。

### 2.3 数据包分层与当前缺口

三把锁不是一个接口的字段映射，而是五个数据包的组合。下载任务按“必需/增强/研究”分层，避免只落一张基础资金流表却误开启信号。

| 数据包 | 接口 | 最低字段/用途 | 当前仓库状态 | 缺失时行为 |
|---|---|---|---|---|
| A 日 K 与交易日 | 本地 `ohlcv`、交易日历 | `open/high/low/close/vol/amount/trade_date`；CYC 代理、量比、收益、波动 | 已有，约 1,189 万行 | 阻断全部资金特征 |
| B 标准资金流 | `moneyflow` | 20 个买卖量/金额字段；主力/敢死队订单分层 | 503 个交易日、2,981,212 个去重股票日 | 可生成主力/敢死队订单代理 |
| C 来源交叉资金流 | `moneyflow_dc`、`moneyflow_ths` | `net_amount`、大/特大单、占比、5 日净额 | DC 503 日完整；THS 426 日，起于 2024-12-19 | THS 早期缺失必须保留 mask |
| D 流动性与规模 | `daily_basic`（优先）或 PIT 估值快照 | `circ_mv/free_share/turnover_rate/volume_ratio` | 官方回补 503 日、2,731,534 股票日 | 扩展证券池覆盖 90.4%；训练时保留缺失标记/支持股票池 |
| E 龙虎榜事件 | `top_list`、`top_inst`、`hm_detail` | 上榜原因、机构席位、游资营业部买卖净额 | 三源均覆盖 503 日；`hm_detail` 135,972 条 | 可构造事件与敢死队资金特征 |
| F 成本分布增强 | `cyq_perf`、`cyq_chips` | `cost_5/15/50/85/95`、`weight_avg`、`winner_rate`、价格分布 | `cyq_perf` 503 日、2,575,555 股票日；`cyq_chips` 已完成 2,303/5,220 有效股票，仍在续传 | `cyq_perf` 已可用于全市场成本线/获利盘；价格分布浓度仅对已完成股票可用 |

已对中继接口做字段级 smoke check（`2026-09-16`）：A/B/C/D/E/F 中的 `moneyflow`、`moneyflow_dc`、`moneyflow_ths`、`top_list`、`top_inst`、`daily_basic`、`cyq_perf` 均可返回；`cyq_chips` 必须提供 `ts_code`，不能只传交易日。该检查只证明接口可用，不代表历史数据已经落盘。

正式数据目录应保持源表分离：

```text
assets/data/raw/moneyflow_snapshots/moneyflow_*.parquet
assets/data/raw/moneyflow_snapshots/moneyflow_dc_*.parquet
assets/data/raw/moneyflow_snapshots/moneyflow_ths_*.parquet
assets/data/raw/moneyflow_snapshots/top_list_*.parquet
assets/data/raw/moneyflow_snapshots/top_inst_*.parquet
assets/data/raw/moneyflow_snapshots/daily_basic_*.parquet
assets/data/raw/moneyflow_snapshots/cyq_perf_*.parquet
assets/data/raw/moneyflow_snapshots/cyq_chips_*.parquet
```

每个源表都必须保留 `source/api/request_params/retrieved_at/available_at`，并以 `stock_code + trade_date`（`cyq_chips` 另含 `price`）去重。不同供应商金额不得直接相加；先生成 `source_count`、符号一致率和差异率，再生成共识特征。

### 2.4 推荐下载顺序

1. **每日增量（必做）**：A+B+C+D，先完成最近交易日；D 使用 `daily_basic` 原始字段，不能只依赖当前估值快照。
2. **每日/夜间事件（必做）**：E，按交易日全市场分页；当天无记录编码 `event_flag=0`，请求失败编码 `is_missing=1`。
3. **历史增强（建议）**：F，先回补与模型训练窗口相同的 756 个自然日，再评估 CYC 增量；`cyq_chips` 数据量大，独立任务、按股票分批落盘。
4. **训练前门禁**：A/B 匹配率≥98%，C/D 覆盖率分别≥90%/95%，E 有明确抓取覆盖报告；否则只跑 `moneyflow_only` shadow，不进入 signal boost。

### 2.5 `cyq_chips` 一次性回补

官方 `cyq_chips`（文档 294）要求 `ts_code`，单次最多 6,000 条，10,000 积分档为每日 200,000 次、每分钟 200 次。智能体数据中心免费版也提供同名兼容接口：Base Relay 为上游账号池/缓存，ProMax 为共享 Key 2,000/min、IP 200/min（以响应头为准）。全市场 503 个交易日的价格档位明细不能按交易日一次拉全；独立阶段将股票稳定分片到官方、Base、ProMax 三路，每路单独限流、遇 `429` 遵循 `Retry-After` 后重试一次，完成范围断点续传：

```bash
source ~/.bashrc
nohup uv run python scripts/run_cn_pipeline.py --stage cyq_chips \
  > output/logs/cyq_chips_backfill.log 2>&1 &
```

默认参数为每源 8 并发：官方 `195/min`，Base `180/min`，ProMax `180/min`。ProMax 文档明确有 IP 限额，Base 文档说明上游 Tushare 频率规则仍生效，因此三个来源都不可以无上限并发。实际首次全量耗时取决于每只股票的分页数与缓存命中；每日 `moneyflow` 不再触发该历史回补，后续仅请求每只股票的未覆盖后缀。

```bash
tail -f output/logs/cyq_chips_backfill.log
uv run python - <<'PY'
from pathlib import Path
import json

manifest = Path('assets/data/raw/moneyflow_snapshots/aux_request_manifest.jsonl')
codes = {
    (record.get('request') or {}).get('ts_code')
    for record in map(json.loads, manifest.read_text().splitlines())
    if record.get('api') == 'cyq_chips' and record.get('status') == 'ok'
}
print(f'completed_codes={len(codes)}')
PY
```

## 3. 特征定义

### 3.1 CYC 成本线代理

```python
cyc_5  = rolling_sum(amount, 5)  / rolling_sum(volume, 5)
cyc_13 = rolling_sum(amount, 13) / rolling_sum(volume, 13)
cyc_34 = rolling_sum(amount, 34) / rolling_sum(volume, 34)

close_to_cyc5  = close / cyc_5 - 1
close_to_cyc13 = close / cyc_13 - 1
close_to_cyc34 = close / cyc_34 - 1
cyc5_slope_3d  = cyc_5 / cyc_5.shift(3) - 1
cyc5_slope_5d  = cyc_5 / cyc_5.shift(5) - 1
```

金额和成交量单位必须在 provider 层确认后再相除；异常成交量、停牌和零成交日输出缺失标记。若接入筹码数据，额外生成成本中位数、获利盘比例、成本集中度和成本上移速度，并与 `cyc_*` 分开命名。

### 3.2 主力资金代理

```python
main_buy  = buy_lg_amount + buy_elg_amount
main_sell = sell_lg_amount + sell_elg_amount
main_net  = main_buy - main_sell

main_net_3d = rolling_sum(main_net, 3)
main_net_5d = rolling_sum(main_net, 5)
main_net_20d = rolling_sum(main_net, 20)
main_strength_3d = main_net_3d / rolling_sum(amount, 3)
main_float_cap_3d = main_net_3d / circulating_market_cap
main_positive_ratio_10d = mean(main_net > 0, 10)
main_acceleration_3d = main_net_3d - main_net_3d.shift(3)
```

同时保存 `main_net_rank`、`main_strength_rank`、`main_source_count` 和来源差异，不将 DC/THS 与标准口径直接相加。

### 3.3 敢死队资金代理

```python
dare_buy = buy_elg_amount
dare_sell = sell_elg_amount
dare_net = dare_buy - dare_sell
dare_net_3d = rolling_sum(dare_net, 3)
dare_strength_3d = dare_net_3d / rolling_sum(amount, 3)

daredevil_score = (
    rank(dare_strength_3d)
    + rank(volume_ratio_20)
    + rank(return_3d)
    + rank(turnover_rate)
    + 0.5 * top_list_flag
    + 0.5 * top_inst_net_buy_rate
)
```

把“强流入但过热”单独建模：特大单强流入 + 短期涨幅过大 + 波动率极高时，输出 `daredevil_overheat_flag`，用于减仓/禁止增强，而不是简单当成买入信号。

### 3.4 三把锁

三把锁拆成三个可解释状态：

```python
trend_lock = (
    (close > cyc_5) &
    (cyc_5 > cyc_5.shift(1)) &
    (close > rolling_mean(close, 20))
)

cost_lock = (
    (cyc_5 > cyc_5.shift(3)) &
    ((close / cyc_5 - 1).between(-0.02, 0.12))
)

flow_lock = (
    (main_strength_3d_rank > 0.70) &
    (dare_strength_3d_rank > 0.60) &
    (volume_ratio_20 > 1.20)
)

three_lock_score = (trend_lock + cost_lock + flow_lock) / 3
three_lock_entry = three_lock_score == 1
```

还需生成：

```text
trend_lock / cost_lock / flow_lock
three_lock_score
three_lock_entry
three_lock_days_since_entry
three_lock_failure
three_lock_overheat
```

固定阈值只是初始规则；正式版本应在训练折内按市值/行业分位数校准。

## 4. 进入统一面板和模型

### 4.1 clean panel

特征命名空间统一使用：

```text
cyc_*
main_*
daredevil_*
three_lock_*
```

所有连续字段配套：

```text
*_is_missing
*_is_imputed
*_is_outlier
*_source_count
```

质量清单记录原始字段、单位、窗口、阈值、来源和 `feature_version`。

### 4.2 LightGBM

分组进入模型，禁止一次性无消融混入：

- `G1_cyc`：CYC 价格/斜率/偏离；
- `G2_main`：主力净额/强度/持续性；
- `G3_daredevil`：特大单/龙虎榜/过热；
- `G4_locks`：三把锁状态和失败状态；
- `G5_consensus`：DC/THS 来源共识和差异。

先使用连续代理和缺失标记；`three_lock_entry` 只能作为可解释特征，不能替代模型排序。

### 4.3 Transformer

输入形状保持：

```text
X: [batch, lookback=60, features]
mask: [batch, lookback, features]
```

资金流、CYC 和锁状态作为时间通道；龙虎榜使用事件数值 + mask，不把营业部名称直接 token 化。比较普通拼接、资金流门控和 cross-attention 三种结构，模型复杂度增加必须有 OOS 增益支撑。

## 5. Signal 与 PK 集成

初始只做确认和风险缩放：

```python
capital_confirmation = (
    0.35 * rank(main_strength_3d)
    + 0.25 * rank(dare_strength_3d)
    + 0.25 * three_lock_score
    + 0.15 * rank(cyc5_slope_3d)
)

boost = clip(capital_confirmation, -1, 1) * 0.10
final_score = model_score * (1 + boost)
```

约束：

- 增强上限 `+10%`；减弱下限 `-10%`；
- `daredevil_overheat_flag=1` 时禁止正向增强；
- 数据质量降级时 boost=0，保留原模型分数；
- PK 仍负责单票、行业、波动、容量和换手约束；
- 每次选择同时输出原始模型分数、资金确认分、boost、最终分数和禁用原因。

## 6. 实验矩阵

| 实验 | 增加内容 | 模型/层 | 目的 |
|---|---|---|---|
| E0 | 当前量价/基本面/Alpha | LightGBM + Transformer | 基线 |
| E1 | + `moneyflow` | 两模型 | 连续资金流增益 |
| E2 | + CYC 代理 | 两模型 | 成本线增益 |
| E3 | + 主力资金 | 两模型 | 大单行为增益 |
| E4 | + 敢死队/龙虎榜 | 两模型 | 短期资金增益与过热风险 |
| E5 | + 三把锁状态 | 两模型 | 复合确认增益 |
| E6 | + signal boost + PK | 交易层 | 可实现收益增益 |

每个实验固定股票池、标签、预测窗口、调仓、成本、随机种子和训练切分，采用 expanding OOS + purge/embargo。报告：IC/RankIC/IR、分位数组收益、净收益、换手、容量、最大回撤、MAE/MFE、覆盖率、漂移和过热误判率。

## 7. 验收门槛

### 数据门槛

- 与日 K 日期匹配率 ≥98%；
- 主键重复率为 0；
- 单位检查和 PIT 检查通过；
- 缺失、未上榜和请求失败可区分；
- 每个特征均有版本和来源。

### 模型门槛

- 至少两个独立 OOS 时段 RankIC 或扣成本收益改善；
- 增益不能只来自单一股票、行业或涨停样本；
- 去除龙虎榜、去除 DC/THS 后结论不应完全反转；
- 特征重要性、SHAP、Permutation 和分层 IC 可解释。

### 交易门槛

- 扣手续费、滑点和冲击成本后仍改善；
- 换手和容量不超过配置上限；
- 过热过滤后最大回撤不恶化；
- 数据降级时能自动退回原模型 signal。

## 8. 实施步骤

### P1：特征引擎

- 新增 `factor_engine/expressions/capital_flow_locks.py`；
- 对 `ohlcv + moneyflow + daily_basic + top_events` 生成宽表；
- 增加单位、PIT、缺失和零成交测试；
- 版本：`capital_flow_locks.v1`。

> 训练就绪检查（2026-09-19）：现有 `clean_feature_panel` 为 2025-09-16 至 2026-09-16 的旧快照，含基础 `moneyflow_*` 列；已发布的 LightGBM/Transformer manifest 分别使用 37/14 个基础资金流列。它不含 `cyc_*`、`main_*`、`daredevil_*`、`three_lock_*`、龙虎榜聚合、`daily_basic` 或 `cyq_perf` 列，不能作为 E0--E5 的新旧特征消融依据。当前 `cn_moneyflow_features.parquet` 只有 2026-09-17、2026-09-18 两日，必须先从完整 raw snapshot 重建完整历史资金特征，再物化新的 clean panel 和模型 feature allowlist。

### P2：面板与模型

- 在 `materialize_clean_feature_panel` 合并特征；
- 更新 LightGBM/Transformer manifest；
- 加入分组 allowlist，支持 E0—E5 消融；
- 记录特征重要性和漂移。

### P3：Signal/PK

- 增加 `selection.signals.capital_flow_confirmation`；
- 默认关闭，先生成 shadow score；
- 经过两个 OOS 区间和 20/60 日纸面账户验证后再灰度开启。

### P4：数据增强

- 独立 `top_events` 阶段回补龙虎榜；
- 增加 DC/THS 夜间回补；
- 有稳定授权后接入 `cyq_perf/cyq_chips`，与 CYC 代理做增量验证。

## 9. 预期产物

```text
assets/data/derived/cn_capital_flow_locks_features.parquet
output/research/capital_flow_locks_ablation_report.{json,md}
output/research/capital_flow_locks_feature_manifest.json
output/model_scores/cn_capital_flow_shadow_scores.csv
output/results_cn/capital_flow_confirmation_attribution.csv
```

## 10. 风险说明

- 不把 App 的视觉图标反向解释为真实机构身份；“主力/敢死队”是订单规模和行为代理。
- 不将龙虎榜营业部名称当作稳定、可泛化的机构标签；名称需聚合为事件统计。
- 不把单日资金流强度当作因果买点；需与收益、成本、流动性和市场状态共同评估。
- 任何正式 signal 开关都必须保留 shadow 版本、回滚版本和 OOS 证据。
