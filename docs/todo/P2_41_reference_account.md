# P2-41 把「模拟盘」绑定到参考口径 + 一致性核对（2026-10-09 第三十七轮）

> 触发：用户「先把模拟盘和盈利较好策略绑定，是一致的么？后面我拿模拟盘做参考？」
> 产物：`scripts/run_shadow_variants.py`（落参考账户 + 一致性核对）、`scripts/export_ths_watchlist.py`（+reference 源）、
> `config/cn_pipeline.toml`（`[paper_account].reference_variant`）；证据：`artifacts/reference_account/`。

## 1. 你问的「一致么」：分三层，答案不一样

| 层面 | 一致吗 | 证据 |
|---|---|---|
| **引擎 / 成本 / 成交口径** | ✅ **一致** | 两边都用 `factor_engine/portfolio/paper_account.py` + 同一份 `config/execution_assumptions_cn.json`（T+1 开盘成交、佣金万2.5+滑点万3+印花税千1、涨跌停/停牌/退市都建模） |
| **参考账户 vs 影子组合同名变体** | ✅ **逐位一致** | `consistency.json`：328 天、`max_abs_nav_diff = 0.0`、`identical: true`（同一份名单历史 `shadow_variants/pool_top10_rank.csv`，构造上就不可能有差别） |
| **旧「模拟盘账户」vs 影子 production** | ❌ **原来不一致** | 模拟盘账户读 `paper_selection_history.csv`（10 个决策日、含 3 天**归因回填的等权兜底**）→ 末值 44,998；影子 production 读台账 notify 里**有实际权重**的 6 天 → 末值 46,797。名单交集只 15 只（41 vs 176 只代码） |

**这正是你要绑定的原因**：以前"模拟盘"和"影子组合"是两张各自拼出来的名单，页面上出现的 −0.00% 与 +3.99% 其实是**两套名单**，不是同一件事的两种表示。
现在参考账户与影子变体**共用同一份名单历史**，一致性由脚本每次核对（不再靠人记）。

**参考账户 vs 生产账户**：口径相同、**名单不同**，所以两个净值**不能直接比大小** —— 共同决策日只有 6 天。

## 2. 绑定成了什么
| 项目 | 值 |
|---|---|
| 绑定规则（可改） | `config/cn_pipeline.toml` → `[paper_account].reference_variant = "pool_top10_rank"` |
| 参考账户产物 | `output/paper_trading/account_reference/{nav,positions,fills,orders,paper_account_summary.json,execution_assumptions.json,variant.json,consistency.json}` |
| 当前状态 | 首决策日 2025-09-08、20 个决策日、期末 **63,358.67**（+40.80%）、最大回撤 **−13.37%**、成交 309 笔、当前持仓 8 只 |
| 每天更新 | 每日流程的 `shadow-variants` 步骤（不阻塞生产）；`ths-export` 已**挪到它之后**，保证导出的是当天参考持仓 |
| 生产账户 | **保留**在 `output/paper_trading/account/`，作为对照，不再混用 |

## 3. 怎么"拿模拟盘做参考"
1. **看持仓/成交**：前端「模拟盘」页新增「参考账户」面板（净值曲线 + 当前持仓 + 成交 + 一致性徽标「逐位一致」），
   或直接读 `output/paper_trading/account_reference/positions.csv`。
2. **照着买（同花顺）**：导出新增第 4 个源 **`reference`**：
   ```
   output/results_cn/ths_watchlist/latest_reference_code.txt          ← 6 位代码，导入自选股即可
   output/results_cn/ths_watchlist/<date>_reference_code_gbk.txt      ← 老版本同花顺
   output/results_cn/ths_watchlist/<date>_reference.csv               ← 代码+名称+行业（给人看）
   output/results_cn/ths_watchlist/<date>_sources.csv                 ← 合并视图里多了一列 reference
   ```
   当前实测：`reference 8 只`（301230 301448 688222 002674 300151 300820 301297 600391），
   与生产口径 `holdings 3 只` 明显不同 —— 导入时**不要**把两个源混在一个分组里。
3. **别混淆**：`holdings` = 生产名单（现行 PK 权重）；`reference` = 参考账户（池前 10 等权）。两个源并存是刻意的。

## 4. 必须知道的风险
1. 参考口径（池前 10）在闸门里只是 **KEEP**：19 期配对 12 好 7 差、中位每期 +0.38%、**p=0.36 不显著**
   （见 `docs/todo/P2_40_promotion_gate.md`）。**它不是已验证的优势，是"目前方向最好的一条"。**
2. 参考账户是**模拟**：T+1 开盘价成交、按统一假设计成本、不含市场冲击；也不是实盘。
3. 45,000 元资金下 10 只各约 4,500 元，**部分槽位买不进**（当前实际持仓 8 只；历史上最少 8 只），
   所以"10 只"在账户层面其实是"最多 10 只"。
4. 参考账户的 20 个决策日来自**台账回填**（2025-09 起），间隔不规则（周频为主）；仍在逐日累积。
5. 切换绑定只需改 `reference_variant` 一个值（`production` / `pool_top5_rank` / `pool_top8_rank` / `pool_top10_rank` …），
   改完跑一次 `uv run python scripts/run_shadow_variants.py` 即可重绑；生产链路参数**未动**。

## 5. 变更点
`config/cn_pipeline.toml`（+reference_variant）、`scripts/run_shadow_variants.py`（参考账户落盘 + consistency.json）、
`scripts/run_daily_production.sh`（ths-export 挪到影子组合之后）、`scripts/export_ths_watchlist.py`（+reference 源）、
`backend/api/paper.py`（+reference）、`frontend/src/{types/api.ts,pages/PaperTrading.vue}`（+参考账户面板）。
