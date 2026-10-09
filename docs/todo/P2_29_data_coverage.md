# P2-29 每日自动化的数据面覆盖审计（2026-10-08）

> 触发：用户要求「每天的自动化需要把所有依赖的数据都要跑完，包括每日分时/财务/财报等等」。
> 证据：`artifacts/data_coverage_audit/`（VERIFICATION.txt / evidence/*）。

---

## 1. 审计方法

不看"任务有没有 exit 0"，而是把三份清单摊平对比，再逐项查**产物本身的新鲜度**：

1. `config/cn_pipeline.toml [stages]` —— 配置认为该开哪些阶段
2. `scripts/run_daily_production.sh` 的 `STAGES` —— 每天**真正**跑哪些
3. `scripts/run_cn_pipeline.py` 注册的阶段 —— 系统**能**跑哪些

再加一个产物层体检：每份数据的最新日期 vs 应该到的日期。

---

## 2. 审计结果（改动前）

| 数据 | 生产方 | 在每日流程里？ | 最新 | 结论 |
|---|---|---|---|---|
| 日线 OHLCV | `--stage daily_bars` | ✅ | 2026-10-08 | ok |
| 资金流 | `--stage moneyflow` | ✅ | 2026-10-08 | ok |
| 分时 5min | `sync_cn_intraday_full.py` | ⚠️ 第二轮才接 | 停在 2026-09-30 | 见 P2_28 |
| **财务 / 财报** | `backfill_cn_financials.py` | ❌ **不在** | 2026-10-06（且判据是"文件存在"，永不增量） | **缺口** |
| **股票基础信息 / 估值历史** | `--stage fundamental` | ❌ **不在** | — | **缺口** |
| **行业分类** | `--stage fundamental` 内 | ❌ **不在** | 2026-09-24 | **缺口（半个月没更新）** |
| **复权因子** | `--stage fundamental` 内 | ❌ 未启用 | 目录不存在 | **缺口** |
| 筹码 cyq_chips | `--stage cyq_chips` | ❌ 不在（config 里也关着） | — | 已知未启用 |
| 公司行动 | 无独立阶段 | ❌ | 2026-09-23 | 事件驱动，见 §4 |

**关键点**：`config [stages]` 里 `fundamental = true`，但 `run_daily_production.sh`
**根本不在 config 的列表上** —— 它有自己的硬编码 `STAGES`（11 个阶段），两套清单从来没有对齐过。
`arm_selection` 也是同一个病因（P2_28 已修）。

### 2.1 财务/财报的缺口有两层

* **层 1：不在每日流程**（`com.quant.financial-backfill` LaunchAgent 未加载）
* **层 2：即使跑了也不会更新** —— 判据是"逐股检查点文件存在 = 完成"，
  实测 `--status` 报 `done=5225/5224`，也就是**新披露的季报/年报永远不会被拉进来**

第二层和分时是同一个 bug 模式（见 P2_28），所以修法也一样：把判据换成
"这只股票最后报告期 < 应该已经披露的最新报告期"。

### 2.2 顺带查了财务数据的实际覆盖（不是缺口，但要说明）

`clean_feature_panel` 里 44 个财务列：

```
financial_roe_clean            19.48%     ← 台账 B8 记的 19.6%
financial_net_margin_clean     19.66%
financial_revenue_yoy_clean     0.00%
valuation_fcf_yield_clean       0.00%
financial_quality_score_clean   0.00%
```

这**不是**因为数据没同步：`cn_financial_indicators.parquet` 有 236,136 行 / 5,225 只、
公告日覆盖到 2026-09-30，按披露截止日推算"应有报告期 = 2026-06-30"，
全市场只有 **1 只**没补上。覆盖率低是 **PIT 连接语义**造成的（财务因子只对公告日之后的
面板行有效，早期年份必然为空），属于研究口径问题（B8），不是自动化问题。

---

## 3. 修法

### 3.1 财务/财报：加 `--refresh` 增量（`scripts/backfill_cn_financials.py`）

判据从"文件存在"改成**按 A 股定期报告披露截止日推算应有的报告期**：

```python
_PERIOD_DEADLINES = (
    ((3, 31), (4, 30)),    # 一季报
    ((6, 30), (8, 31)),    # 半年报
    ((9, 30), (10, 31)),   # 三季报
    ((12, 31), (4, 30)),   # 年报（次年）
)
expected_period("2026-10-08") → 2026-06-30     # 半年报 8/31 已过，三季报 10/31 未到
```

**第一版用"全市场最新 ann_date"当基准，实测直接踩坑**：2026-10-08 时全市场最新公告日是
09-30（少数公司提前披露），而绝大多数公司停在 08-2x ⇒ **5,224 只里 5,221 只被判成陈旧**，
每天要重抓 5,000+ 只。改成按法定截止日推算后，当前只剩 **1 只**真正需要补。

另外：
* 加 `--limit`（限时调试）与 `--active-window-days`（最后公告日早于 400 天的视为已停止披露，跳过）
* 批次后**用与挑选时相同的判据**复核（第一版用 `ann_date` 复核、用 `end_date` 挑选，
  两者不一致 ⇒ `002731.SZ` 被无限重抓，实测死循环，已修）
* 单实例锁（与分时同款）

### 3.2 每日流程：补上"数据补齐"块（`scripts/run_daily_production.sh`）

放在**主阶段之前**：

```
[数据补齐] fundamental → financials(--refresh) → financials(--merge)
   ↓
[主阶段] daily_bars → moneyflow → regime → features → clean_panel → model_scores
        → preselection → pk → exits → paper_account → paper_outcomes
   ↓
[收尾]   arm_selection → intraday-refresh → 数据完整性报表
```

* 放在主阶段**之前**的原因：这些是 `features` / `clean_panel` 的输入。
  放后面的话，当天新披露的财报要**第二天**才进特征面板。
* 每个数据步骤**非致命**：数据源会抽风（2026-10-08 baostock 就挂过），
  不该因此让整条选股链停摆。缺什么由收尾的完整性报表统一暴露。
* 开关：`DAILY_DATA_REFRESH=0` 整体关；`DAILY_FUNDAMENTAL=0` / `DAILY_FINANCIALS=0` 单独关；
  `DAILY_FINANCIALS_SECONDS`（默认 1800）限时。
* `--dry-run` 也会把它们列出来。

### 3.3 数据完整性报表（新增 `scripts/data_completeness_report.py`）

独立的体检器：**只看产物，不看任务退出码**。每份数据按自己的更新频率判达标：

| 频率 | 判据 |
|---|---|
| `daily` | 最新日期 == 基准交易日 |
| `report` | 没有"该披露却没披露"的股票 |
| `monthly` | 允许滞后 1~2 个月（当月月末还没到） |
| `event` | 距上次快照 ≤ 30 天 |

产出 `output/data_quality/data_completeness.{md,json}`，并在每日 `run.log` 里打一行结论。

---

## 4. 修完之后的实测状态

```
| 数据 | 频率 | 最新 | 状态 | 说明 |
|---|---|---|---|---|
| 日线 OHLCV | 每日 | 2026-10-08 | ✅ | 最新交易日 5208 只 |
| 复权因子 | 每日 | — | ❌ | 目录不存在（[relay_data].adjustment_factors_enabled=false） |
| 资金流 | 每日 | 2026-10-08 | ✅ | 7,881,007 行 |
| 分时 5min | 每日 | 482/4533 | ⚠️ | 已到 2026-10-08: 482 只 / 落后 4051 只（补齐进行中） |
| 财务 / 财报（PIT） | 按披露截止日 | 2026-06-30 | ⚠️ | 应有报告期 2026-06-30｜待补 1 只｜已合并 236,136 行 / 5225 只 |
| 估值快照（daily_basic） | 每月 | 2026-09-30 | ✅ | 105 个快照文件 |
| 行业分类 | 事件驱动 | 2026-09-24 | ✅ | 2 个文件 |
| 公司行动 | 事件驱动 | 2026-09-23 | ✅ | 6 个文件 |

**未达标 3/8**：复权因子、分时 5min、财务 / 财报（PIT）
```

财务那一项的 ⚠️ 是 `002731.SZ` 一只补不到（中继没有它更新的报告期），
已被 quarantine（3 次失败后跳过），不会拖累每日流程。

---

## 5. 边界与遗留

1. **复权因子仍未启用**：`[relay_data].adjustment_factors_enabled = false`，
   `assets/data/raw/adjustment_factor_snapshots/` 目录都不存在。当前 qfq OHLCV 是已算好的，
   所以这是"审计层缺口"而非选股硬依赖 —— 但**没有这层就无法校验复权是否算错**，建议单独一轮决定要不要开。
2. **筹码 cyq_chips 仍未进每日流程**（config 里 `fetch_cyq_chips=false`，设计上是分批回补）。
   本轮把它列为已知未启用，没有盲开（`cyq_chips_stock_limit=0` 意味着全市场，成本未评估）。
3. **`fundamental` 阶段的耗时未实测**：它含股票信息 + 百度估值历史 + 行业分类回补，
   本轮是在每日生产已经在跑的情况下接入的，没有单独压测。若它过慢，
   用 `DAILY_FUNDAMENTAL=0` 关掉、改由专门的 LaunchAgent 跑。
4. **公司行动没有独立生产方**（只有 `corporate_actions_snapshots` 6 个快照文件，
   最近 2026-09-23）。本轮只把它纳入体检，没有新建拉取任务。
5. 财务覆盖率的低值（19.5%）是 PIT 语义问题，不是同步问题；要提高到 B8 说的 80% 需要
   单独做 PIT 连接口径的研究。
6. **本轮踩的一个自伤**：我在每日生产**正在运行**时编辑了 `run_daily_production.sh`。
   bash 是边读边执行的，插入文本会让后续字节偏移错位（当前这轮侥幸没坏 —— 
   因为 `for` 循环这种复合命令在执行前已被完整解析）。**教训：不要在运行中的 bash 脚本上改文件**，
   要么等它跑完，要么改副本。
