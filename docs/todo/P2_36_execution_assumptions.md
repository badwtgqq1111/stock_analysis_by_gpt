# P2-36 统一执行假设（模拟盘 ↔ 日频回放）+ 长样本前瞻收益记录（2026-10-09，第三十二轮）

> 触发：上一轮的四个遗留中剩下的两条 ——「模拟盘与回放引擎不是同一套执行假设」「样本只有 7（现 10）个决策日，不构成统计结论」。
> 证据：`artifacts/execution_assumptions_unified/`（`VERIFICATION.txt` + `baseline/` + `diffs/` + `evidence/` + `ROLLBACK.sh`）。

## 1. 两套引擎此前的口径差（这就是"净值不可比"的原因）

| 假设 | 模拟盘（`paper_account.py`） | 日频回放（`replay_daily_execution.py`） |
|---|---|---|
| 佣金 | 5bp | 2.5bp |
| 滑点 | 5bp | 3bp |
| 印花税（卖出） | **无** | 10bp |
| 整手 | 100 股 | 100 股 |
| 涨跌停 | **不建模** | 涨停不买、涨停不卖（顺延） |
| 停牌 | 挂 pending（持仓当天**从净值里消失**） | 不可交易、持仓按最后价 |
| 退市 | **不建模** | 连续 20 日无行情 → 强制清算 |
| 成交参考价 | 次日开盘 | 决策日收盘 |

## 2. 改动
| 位置 | 改动 |
|---|---|
| `config/execution_assumptions_cn.json`（新） | **唯一来源**：成本/整手/涨跌停规则与容差/停牌/退市阈值/成交参考价；未建模项（ST 5%、分红对前收的影响）写在 `_note` 里 |
| `factor_engine/portfolio/execution_assumptions.py`（新） | 共享读接口：`load_assumptions / cost_model / price_limit_ratio / is_limit_up / is_limit_down / curse_month` |
| `factor_engine/portfolio/paper_account.py` | 假设改由共享文件解析；新增建模：涨停不买、跌停卖不出（**顺延不丢单**）、涨停不卖（策略规则）、停牌沿用最后价盯市、连续 20 日无行情强制清算、卖出计印花税；新增 `reason` 列记录被挡原因；`enforce_frictions=False` 可退回旧行为做 A/B |
| `scripts/replay_daily_execution.py` | 常数改读共享文件（`--assumptions` 可换一份）；**补上"跌停卖不出"闸门**（注释里一直写着、代码里没有，`skip_sell_limit` 恒为 0） |
| `scripts/execution_parity_check.py`（新） | 静态（两边实际生效值并排）+ **行为探针**（各跑一段小行情：涨停当天买、跌停当天卖必须都不成交）；`DIFFER` 非 0 即退出码 1；快照写 `output/verification/execution_parity.json` |
| `scripts/run_daily_production.sh` | 新增 `forward-validation` / `execution-parity` 两步（非致命，`DAILY_*` 可关）⇒ 每日流程 23 → **25 步** |
| `backend/api/paper.py` / `backend/api/forward.py`（新） | `/api/paper` 增加 `assumptions` + `parity`；新增 `/api/forward-validation[/{source}]` |
| `frontend/src/pages/PaperTrading.vue` | 新增两块面板：「执行假设（与日频回放共用同一份）」+「长样本：候选池前瞻记录」 |
| `test/test_panel_training.py`、`test/test_cn_pipeline_runner.py` | 契约随实现更新（前者要注意旧用例 `10 → 11` 正好是涨停，已另加专门用例；后者见下文"抓到的真回归"） |

## 3. 顺带修掉的两个真 bug
1. **回放的"跌停卖不出"只写在注释里**：`stats["skip_sell_limit"]` 恒为 0。
   base_v1 复跑影响：`trades` 503 → 492、`final_nav` 244,407.39 → 244,528.62（+0.05%）、
   `max_drawdown` -17.221% → -17.251%。方向明确、幅度很小，已记录。
2. **模拟盘停牌持仓会凭空蒸发**：旧代码只在 `code in day.index` 时才把市值加进净值。
   统一为"沿用最后可得价盯市"。

## 4. 长样本（遗留第 1 条）
模拟盘的账户口径**无法变长**（最终名单 2026-09 才开始归档，更早的永久丢失，10 个决策日就是上限）。
但选股台账 `selection_ledger.parquet` 里 `preselected` 口径从 **2025-09-08** 起就带着**已实现的前瞻收益**
（r1/r5/r10/r20 + 基准 + 超额）⇒ 新增 `scripts/build_forward_validation.py`：

| 口径 | 决策日 | 区间 | 每期等权(r5) | 胜率 | 累计(不扣成本) | 基准累计 | 最大回撤 |
|---|---|---|---|---|---|---|---|
| preselected | 15 | 2025-09-08 ~ 2026-09-23 | +1.30% | 67% | +20.2% | +13.6% | -5.4% |
| notify | 4 | 2026-09-17 ~ 2026-09-23 | -3.33% | 0% | -12.8% | -7.9% | -11.4% |
| survey_gate | 15 | 2026-06-03 ~ 2026-09-10 | +0.06% | 27% | -0.4% | -5.2% | -16.2% |
| survey_raw | 15 | 2026-06-03 ~ 2026-09-10 | -0.39% | 33% | -10.2% | -5.2% | -25.3% |

**口径必须一起记住**：候选池等权、直接用已实现收益串联、**不扣成本、不建模成交**、决策日间隔不固定（周频为主）
⇒ 它是"决策质量记录"，**不是账户净值**。页面上与账户口径并排展示，就是防止拿长样本的漂亮数字当业绩。
交叉验证：本脚本独立聚合的 `preselected` 每期均值与台账自带的 `selection_ledger_by_date.csv` 差 `3e-7`。

## 5. 验收（细节见 `artifacts/execution_assumptions_unified/VERIFICATION.txt`）
- 一致性：`MATCH=17 DIFFER=0 DIFFERS_BY_DESIGN=1`（唯一设计差异 = 成交参考价），**exit 0**；
- 单测：`test_panel_training.py -k paper_account` 4 passed、`test_execution_assumptions_parity.py` 4 passed、
  `test_cn_pipeline_runner.py` 20 passed；baseline 版模块 + baseline 版测试在 /tmp 跑 2 passed（不碰 live 树）；
- **被测试抓到的真回归**：`test_cn_pipeline_runner.py` 里 `lot_size == 100` 的旧断言随契约变化失效，
  已改成验更强的新契约（默认 `None` = 共享假设；TOML 显式写值仍然优先）。整文件 `test_panel_training.py -q`
  **没跑完**（约 55 分钟只累计 6 秒 CPU，判定卡在外部依赖上，已 kill 改跑定向子集）—— 这条按"未完成"记；
- 回放重构等价性：默认假设下 baseline 与重构后 `final_nav` **逐位相同**（244,407.3898）；
- 真实历史 A/B：期末净值 44,994.71（旧口径）→ 44,998.15（统一口径），卖出佣金 61.56 → **153.94**（印花税真的开始计）；
- 回滚复验：临时树 **OK=19 DIFF=0**（13 个改动文件逐一 == baseline、6 个新增文件被移走）。

## 6. 仍未做
1. **成交参考价没有统一**（模拟盘=次日开盘 / 回放=决策日收盘）。真统一要给回放加"次开成交"
   （`price_matrix` 增加 open + 决策/执行时点分离），属研究级改动；本轮只把它显式化。
2. **ST 的 5% 涨跌停**未建模（需要 PIT 名称变更史）；分红除权对"前收"的影响用复权价近似。
3. 模拟盘账户口径仍是 **10 个决策日**，不足以做任何统计判断；`notify` 4 期全负只是这个短窗口的事实。
