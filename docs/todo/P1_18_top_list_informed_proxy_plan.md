# P1.18 龙虎榜知情代理特征与选股级 OOS 复验方案

> 状态：待启动。两项剩余工作：①把 `top_list`/`top_inst`（龙虎榜 + 席位明细）做成"知情交易"代理特征并接入模型；②把已经完成的 169 日**规则级**检验（P1.17 §0.19）扩展到**选股级**（含 PK 权重、sleeve、可买性约束的完整生产链路）。
> 日期：2026-09-21
> 关联：`P1_17_path_labels_meta_labeling_plan.md`（§0.13~§0.20）、`P1_15_moneyflow_plan.md`、`P1_16_three_locks_capital_features_plan.md`
> 目标：用真正的"席位/知情交易"数据替代目前信息量只有成交量 1/4 的资金流代理（P1.17 §0.16 实测 0.13pp vs 0.48pp），并让所有收益结论都在**真实组合口径**（而非规则级近似）上复验。

---

## 1. 现状核查（2026-09-21 实测）

### 1.1 数据现状

| 数据集 | 行数 | 交易日 | 区间 | 关键字段 |
|---|---:|---:|---|---|
| `top_list`（龙虎榜） | 40,318 | **503** | 2024-08-23 ~ 2026-09-18 | `ts_code`、`name`、`close`、`pct_change`、`turnover_rate`、`amount`、`l_buy`、`l_sell`、`net_amount`、`net_rate`、`amount_rate`、`float_values`、`reason` |
| `top_inst`（席位明细） | 432,246 | 503 | 同上 | `exalter`（席位全称）、`buy`/`sell`/`net_buy`、`buy_rate`/`sell_rate`、`side`、`reason` |

席位构成（`top_inst` 全样本）：**机构专用 16.6%**、沪股通 2.8%、深股通 4.0%、营业部（游资）56.9%。→ 可以构造"机构净买入 / 北向净买入 / 游资净买入"三类占比，这正是文献里"知情 vs 流动性"（Llorente et al. 2002）的可得代理。

### 1.2 PIT 可得性（决定性）

按文件内的 `retrieved_at` 统计每个 `trade_date` 的最早抓取时间（北京时间）：

| 交易日 | 最早抓取 | 距当日 15:00 收盘 |
|---|---|---:|
| 2026-09-18 | 09-18 19:45 | **+4.8h（当日盘后可得）** |
| 2026-09-17 | 09-18 17:24 | +26.4h |
| 2026-09-16 | 09-18 09:48 | +42.8h |
| 2026-09-14 及更早 | 09-18 20:02 | +77h ~ +269h（历史快照为一次性回补） |

**结论**：交易所口径的龙虎榜在 **T 日盘后**（约 17:00–20:00）公布，生产链路（`run_daily_production.sh` 在 19:00/20:30/22:00 触发）能当日取到；历史文件因调度不规律而滞后期更长，但这只影响"抓取时间"，不影响"数据本身属于 T 日"。因此特征与 P1.17 的口径一致：**T 日盘后决策 → T+1 开盘执行**（`AVAILABILITY_RULE`），特征只能用 `trade_date ≤ T` 的行构造。

**待核对**：503 个交易日是否覆盖区间内全部交易日（2024-08-23 起约 500 个交易日，需做缺口清单）；个别日期的 `reason` 文本需规范化。

### 1.3 链路落点

| 环节 | 现状 | 需要什么 |
|---|---|---|
| 抓取 | `data/ingest/service.py::refresh_cn_moneyflow_aux(fetch_top_list=True, fetch_top_inst=True)` 已默认开启，落 `assets/data/raw/moneyflow_snapshots/top_{list,inst}_*.parquet` | 无需改动 |
| 派生 | **不存在**（只有 raw） | 新增 `build_top_list_features(...)` → `assets/data/derived/cn_top_list_features.parquet`（格式对齐 `cn_moneyflow_features.parquet`：`stock_code/trade_date/特征列`） |
| 面板 | `materialize_clean_feature_panel(moneyflow_path=...)` 已按同日键合并 | 新增 `top_list_path` 参数并合并；缺失保留 `is_missing` |
| 模型 | Transformer/CNN 有 `protected_features` 白名单（P1.17 W3） | 把关键席位字段加入白名单，白名单从 256 对预算内扣减 |
| 评估 | 169 日**规则级**（P1.17 §0.19） | **选股级**：把每折 OOS 分数写成 per-date 分数文件，跑完整 preselection+pk |

---

## 2. 文献依据

| 结论 | 来源 | 对本方案的含义 |
|---|---|---|
| 订单不平衡预测短期收益 | Chordia, Roll & Subrahmanyam (2002, *JFE*)；Chordia & Subrahmanyam (2004, *JFE*) | 方向成立，但他们用的是逐笔订单；龙虎榜是唯一可得的"机构/游资"分解代理 |
| 量价关系的方向取决于知情 vs 流动性驱动 | Llorente, Michaely, Saar & Wang (2002, *RFS*) | 席位性质（机构/北向 vs 游资）正是判别"知情"的可观测标签 |
| 真实操纵案例呈"放量拉高 → 派发" | Aggarwal & Wu (2006, *JB*) | 上榜净卖出/机构出货的组合应作为**负向**条件 |
| 流动毒性预测波动而非方向 | Easley, López de Prado & O'Hara (2012, *RFS*) | 席位指标若只预测波动，应转做仓位缩放而非选股 |
| 我们的实测 | P1.17 §0.16 | 供应商资金流组间差仅 0.13pp（成交量 0.48pp）→ 需要更接近"知情"的代理 |

---

## 3. 工作项

### 3.1 W1 派生特征（事件层 + 席位层 + 时序层）

落点：`data/ingest/providers/cn_top_list.py`（新），产物 `assets/data/derived/cn_top_list_features.parquet`。

```text
事件层（来自 top_list，按 float_values 标准化以消除市值差异）
  lhb_net_ratio          net_amount / float_values
  lhb_net_rate           net_rate（净买额占成交额）
  lhb_amount_rate        amount_rate（成交额占全天成交）
  lhb_reason_<type>      reason 分类哑变量（涨幅偏离/换手率/振幅/连续三日/ST 等）

席位层（来自 top_inst，按净买入额分组）
  lhb_inst_net_share     机构专用席位净买入 / 全部席位净买入绝对值
  lhb_north_net_share    沪深股通专用净买入占比
  lhb_hot_money_share    营业部（非机构/非北向）净买入占比
  lhb_seat_concentration 买入前 5 席位净额 HHI
  lhb_inst_buy_days_20   近 20 日出现"机构净买入"的天数

时序层（滚动窗口，仅使用 trade_date ≤ T）
  lhb_count_5d / _20d    近 5/20 日上榜次数
  lhb_net_5d_sum         近 5 日 net_amount/float_values 之和
  lhb_days_since_last    距最近一次上榜天数
```

**稀疏特征处理**：每日上榜约 60–100 只（`top_list`），未上榜的股票取 0 但必须保留 `is_missing` 语义（"未触发上榜" ≠ "抓取失败"），并在文档中写明。

### 3.2 W2 面板与模型接入

1. `materialize_clean_feature_panel(..., top_list_path=...)`：同日键合并 + `is_missing`；
2. Transformer/CNN `protected_features` 加入 `lhb_inst_net_share`、`lhb_net_ratio`、`lhb_count_5d`、`lhb_days_since_last`；
3. 审计字段：`top_list_feature_count`、`missing`、覆盖率报告（与 `quality_report` 一并落盘）。

### 3.3 W3 事件研究（**先做，成本最低，决定后续是否值得投**）

对 2024-08-23~2026-09-18 全部上榜样本做分组事件研究：

| 组 | 定义 |
|---|---|
| A 机构净买入 | `lhb_inst_net_share > 0.5` |
| B 机构净卖出 | `lhb_inst_net_share < -0.5` |
| C 游资主导 | `lhb_hot_money_share > 0.5` |
| D 对照组 | 同日同行业、未上榜、同市值分位的股票 |

指标：未来 1/5/20 日**超额收益**（vs 全市场等权）均值、中位数、命中率、按日期聚类的 t 值；并检查是否只是"动量"的代理（加入 20 日涨幅分层后是否仍显著）。

### 3.4 W4 模型接入与 OOS 评估

同一 13 折 / 169 个决策日口径（`config/cn_pipeline_p1_17_extended.toml` 家族），做三组对照：

```text
① 无龙虎榜特征（当前基线）
② 加全部龙虎榜特征
③ 只加机构/北向类（剔除游资类）
```

评估：RankIC（5/10/20 日）、Top-20 链式净收益、最差单周、以及 W3 的条件超额是否被模型利用。

### 3.5 W5 选股级 169 日复验（P1.17 §0.19 的升级版）

现状：169 日的检验是**规则级**（离线对 OOS 分数套规则、等权 Top-10），未含 PK 权重、sleeve 覆盖、可买性与整手约束。

做法：
1. 用现有 13 折 OOS 产物，把每折对测试日的 `model_score` 按日期写成 `output/model_scores_oos_<date>/cn_{lightgbm,transformer}_scores.csv`（**严格使用该折的模型**，无前视）；
2. 对每个决策日跑 `preselection` + `pk`（`--trade-date <date>`，配置指向对应分数目录）；
3. 评估**真实组合**的 5/10/20 日净收益（扣 25bps）与尾部，与"旧规则"组合同日配对检验。

算力：169 日 × 2 臂 × 2 阶段 ≈ 3 小时；先做**每 5 个交易日的 34 日子集**（≈35 分钟）出结论，再决定是否铺满 169 日。

---

## 4. 验收标准

| 项 | 验收 |
|---|---|
| W1 | 派生表行数与 raw 对账一致；503 个交易日缺口清单为空或已解释；`is_missing` 与 0 值语义分离 |
| W2 | 面板与 manifest 记录 `top_list_feature_count` 与覆盖率；protected 白名单从 256 对预算内扣减 |
| W3 | **机构净买入组相对对照组的 5 日超额 ≥ +0.3pp 且按日聚类 p < 0.05**；若否证则记录负结论并停止 W4/W5 中的模型接入 |
| W4 | 加入特征后 5/10 日链式净收益不劣化，且 RankIC 不下降；机构组条件超额改善 |
| W5 | 34 日选股级复验中，新规则组合相对基线的配对差 ≥ +1pp 且 p < 0.10，最差单周不劣化 |

失败也是结论：W3 若显示席位指标只预测波动（与 Easley et al. 2012 一致），则该特征族转为**仓位缩放**输入，不进入选股排序。

---

## 5. 阻断项与待确认

| 项 | 类型 | 影响 |
|---|---|---|
| `top_list` 是否有交易日缺口 | 阻断 W1 | 覆盖率不足会让时序类特征（5/20 日累计）失真 |
| `exalter` 席位名称规范化（全称/简称混用） | 阻断 W1 | 机构/北向/游资三类划分依赖关键词匹配，需人工抽查 |
| 是否需要"知名游资"名单 | 不阻断 | 先按机构/北向/营业部三分，名单留作 W4 增强 |
| 选股级复验的算力预算 | 阻断 W5 铺满 169 日 | 先做 34 日子集 |
| 龙虎榜是否与"第二次资金流"重叠 | 不阻断 | 需在 W4 消融中同时移除两者，避免归因混叠 |

---

## 6. 实施顺序

```text
0. W3 事件研究（1 小时内出结论）      ← 决定后面是否值得做
1. W1 派生特征 + 缺口/席位规范化核对
2. W2 面板接入 + protected 白名单 + 测试
3. W4 13 折 OOS 对照（无/全部/仅机构三组）
4. W5 34 日选股级复验 → 视结果扩到 169 日
```

---

## 7. 回滚

按 `artifacts/` 既有约定，每个工作项生成 `DIFF_FILE.diff`、`MODIFIED_FILE.*`、`ROLLBACK.sh`、`VERIFICATION.txt`，目录 `artifacts/top_list_informed_proxy/`。

特征侧回滚是配置级的：派生表路径为空（`top_list_path = ""`）即完全不接入；`[model_features] exclude_families = ["top_list"]` 可在画像层整族关闭；面板与模型 manifest 都会记录该族的准入/缺失状态，便于审计。
