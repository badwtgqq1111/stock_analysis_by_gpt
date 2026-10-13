# P2.53 适应度 / 值快照 / 多因子对比（FactorMiner 清单的第 1~3 项）

编写：2026-10-12。来源：`docs/todo/P2_52_factor_miner_review.md` §3「值得抄但要排期的」，
按 1 → 2 → 3 顺序落地（用户："好的，继续"）。

## 1. 可配置的适应度（Fitness Hook）

`config/factor_fitness.json`（声明式）+ `scripts/factor_fitness.py`：

```
score = Σ w_i × min(|x_i| / target_i, cap)  −  Σ w_j × max(0, (x_j − target_j) / target_j)
```

* 三个 profile：`default`（IC/ICIR/t + 冗余·换手·衰减惩罚）、`low_turnover`（换手惩罚加倍）、
  `long_horizon`（偏 h20 与低衰减）。**加 profile / 改权重只动配置**，脚本不写死。
* 数据源：因子库 metrics（IC/ICIR/NW t…）+ inspector 档案（冗余 max|ρ|、Q5 换手、衰减比、多空、滚动 IC）。
* **纪律（写在产物里）**：fitness 只用于**排序候选**；准入仍由 `factor_ic_protocol`（|IC|/BH-FDR/HLZ/簇冗余）判，
  上线仍由 `promotion_check`（同窗口逐期配对 + 集中度）判。
* **缺证据不假装**：没有 inspector 档案的因子**不给"推荐"**，只标 `pending_review`
  —— 实测：107 条候选里 8 条有档案，`推荐 1 / 待审 92`（若不区分，会得到 96 条"推荐"的假象）。
* `--review-top N`：先把 |ICIR| 最强且未审的 N 条送进 inspector，再评分（一条 ~5 秒）。

实测（default profile）：`CORR5_clean` 2.304 分且 max|ρ|=0.613 ⇒ **推荐**；
`volume_price_divergence_10d` 0.749、`GTJA081` 0.754、`vr_volatility_20` 0.812、`GTJA097` 0.888 ⇒ 都因冗余被扣分/不推荐。

## 2. 因子值快照（可对账）

`scripts/snapshot_factor_values.py` → `output/factor_library/snapshots/<factor_id>.{parquet,json}`：

* parquet：`trade_date / stock_code / value / fwd_return_{1,5,20}d`（标签口径与 inspector **同一份代码**，`importlib` 复用，避免两套口径）；
* json：血缘（面板窗口、清洗版本、行/日/股、覆盖率、**parquet sha256**、命令、复现说明）；
* `--verify`：重算一遍并与快照对账（`max|Δ|`、覆盖率差、`identical/close/drift` 三档判定）。
  实测 `CORR5_clean`：1,905,327 行 / 380 日 / 5,224 股 / 覆盖 100% → 对账 `max|Δ| = 0 ⇒ identical`。

## 3. 多因子对比（2–5 条）

* 接口：`GET /api/agent/factors/inspector/compare?ids=a,b,c` —— 滚动 IC（h20）按**共同日期**对齐，
  **不插值、不补造**；缺档案的列进 `missing` 并给命令；单条 → 400。
* 前端：因子库表加勾选列（最多 5 条）→「对比选中」→ 对比面板（滚动 IC 叠加 + 10 列指标表 + 缺档案提示）；
  表里新增 **fitness 列**（悬停显示 rank / 未审惩罚项 / 是否推荐）。
* 实测：`moneyflow_amount_scale_clean` + `GTJA097_clean` 共同日期 **342 天**（2025-04-10 ~ 2026-09-03），
  2 条曲线叠加 + 指标表；未审过的 id 会被明确列出来。

## 4. 验收

`output/verification/p1_86_fitness_snapshot_compare_20261012/verify_fitness.py`（配置/打分/推荐语义/快照对账/对比接口 + 页面交互）
+ `test/test_factor_fitness.py`（6 条，含"缺证据不给推荐"与"快照对账 identical"）。

## 5. 下一步

* 把 `--review-top` 接进每日自动化（每晚审 3~5 条未审因子，约 30 秒），让 `with_inspector` 覆盖率逐日爬升；
* 值快照升级为"入池即自动存快照"（`registerStrategyCandidate` 触发，或每日扫候选）；
* 多因子对比扩展到"因子 vs 模型分"（把 B2 的 model score 也当一条曲线叠上去）。

## 6. 续作（2026-10-12，本轮）：把三者接进每日流程 + 补一条口径对照

1. **每日审查覆盖**（§5 第 1 条）：`run_daily_production.sh` 新增非阻塞阶段
   `DAILY_FACTOR_INSPECTOR`（默认 3）→ `factor_fitness.py --review-top N`：
   每晚把 |ICIR| 最强且**还没有审查档案**的 N 条送进 Inspector，再按 profile 打分。
   为什么必须这么做：适应度里的冗余/换手/衰减惩罚**只有审过才算得出来**，不审就永远是「待审」，
   推荐名单也永远不可信（实测：107 条候选一开始只有 4 条有档案 → 96 条「推荐」假象）。
2. **入池即存快照 + 每日补扫**（§5 第 2 条）：新增 `scripts/snapshot_candidates.py`（每日非阻塞阶段
   `DAILY_CANDIDATE_SNAPSHOTS`）：扫 `output/strategy_pool/candidates/`，三态如实记录
   `ok / skipped_existing / not_a_panel_column`（后者给命令 + 说明「表达式还没落成面板列」）。
   **踩坑并修掉**：面板加载器会把请求的 `*_clean` 名字 materialize 成全 NaN 列，
   于是「列存在」不等于「因子存在」—— 第一版给 4 个候选各生成了 **46MB 全 NaN 假快照**还标 ok。
   现在 `snapshot_factor_values.py` 与 `factor_inspector.py` 都加了**覆盖率 0 = 缺列**的守卫（有测试锁），
   并已清理垃圾快照。
3. **因子 vs 模型分**（§5 第 3 条）：对比接口新增 `include_model=1` → 叠加一条
   **`model_score@ledger`**：来自选股台账的**决策日**截面秩 IC(model_score, 已实现 r20)。
   口径写清在返回 note 里：**稀疏决策日 + 已实现收益**，与面板口径的逐日自建前向收益 IC **不可直接比**。
   实测：**55 个决策日（2020-07-16 ~ 2026-09-03），均值 IC +0.0112、ICIR 0.030、正号占比 54.5%**
   —— 模型分在决策日的截面排序能力：弱但为正。页面上勾「叠加模型分（台账·决策日）」即出现。

## 7. 下一步

* 把「模型分」从**决策日台账**升级为**逐日**：`--stage model_scores` 目前只写当天一版
  （`output/model_scores/cn_lightgbm_scores.csv` 会被覆盖）→ 建议按日归档 `output/model_scores/<date>.parquet`，
  这样模型分也能进 Inspector 的日频口径，与因子同台比较；
* 候选快照在「因子真正落成面板列」后自动转 ok（现在靠每日补扫，已经是这个行为）；
* 审查覆盖率做成页面指标（`with_inspector / factors` 直接显示在因子库标题上）。
