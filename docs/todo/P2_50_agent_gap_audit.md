# P2.50 P2_42 → P2_49 完成度审计（还差什么）

编写：2026-10-12。触发（用户原话）：**"P2_42 -> p2_49 还有哪些没有完成的么？主要差 agent 功能完善，还有 agent UI 交互？"**

结论先行：**用户猜得对，且缺的就是这两块**。文档里被标成 ✅ 的 11 项都**逐条核过产物**（不是看勾选）；
真正没做的是 **5 项 agent 功能 + 2 项 agent UI 交互 + 2 项研究性实验**，其中本轮补齐了 3 项
（下表标「本轮」），剩下的都是**需要时间/数据累积**或**明确留给后续**的，不是没看见。

## 1. 逐项完成度（✅ 都核过产物）

| 来源 | 项 | 状态 | 证据 / 缺口 |
|---|---|---|---|
| P2.42 §4.1 | 运行轨迹 `agent_run` | ✅ | `output/agent_runs/*/run.json`；`summarize/from-session/list/show` 四条子命令 |
| P2.42 §4.2 | 因子库注册表 | ✅ | `output/factor_library/factors.json` 1,431 条（带分类/描述/性能）；`/api/agent/factors` 可筛 |
| P2.42 §4.3 | 模型卡 | ✅ | `output/models/cn/<model>/card.json`；执行台「模型层」tile 直读 |
| P2.42 §4.6 | 前端「投研执行台」 | ✅ | `/agent` 三标签页；四层 + 两族树 + 轨迹 + 门槛 + 提示词 |
| P2.42 §4.4 | 上线流水线一条命令 | **本轮补齐** | `scripts/promote_candidate.py`（判决 PROMOTE/KEEP/REJECT/MISSING + 人工动作；`--apply` 需 `--confirm`） |
| P2.42 §4.5 | 下单前 checklist | **本轮补齐** | `scripts/pre_order_check.py`（9 项、五类归因、三档状态；实盘 1 block + 2 warn） |
| P2.43 §0.5 | 挖掘↔执行台关联链路 | ✅ | 会话→轨迹双向跳转、门槛跟随 |
| P2.43 §2.1~2.5 | **挖掘工作台界面重排** | **✅ 本轮（p1_83）补齐** | 左栏模板/工具、上下文 chips、常驻 composer、因子卡三操作全部落地；`GET /api/agent/factors/context` 提供口径；实测 31 项 PASS（`output/verification/p1_83_mining_workbench_20261012/`） |
| P2.45 A | 模型臂同口径曲线 | ✅ | `performance.json` 的 `model_arms` + `research`（候选池 259 观测） |
| P2.45 B/C | 标签改超额 / 窗口+早停 | ✅ | 开关 + A/B 已跑，结论：**负结果**（−1.6pp），已归档 |
| P2.45 **D** | PK 拆「约束层/权重层」+ 影子变体 | ⚠️ 半 | 影子变体 `model_topn_equal` ✅ 已有；**拆层没做**；判据要 ≥20 期而 `production` 只有 7 期 ⇒ **等数据** |
| P2.45 **E** | 冗余也筛（准入 ∩ 非冗余，100 → ~60） | ❌ **研究实验** | 未做；`factor_ic_registry.csv` 里 101 条同簇冗余还在给模型吃 |
| P2.46 | 书里 8 条"可抄" | ⚠️ 部分 | N_eff ✅、lambdarank ✅（实测更差，未启用）、单调约束 ✅（未启用）、条件 IC ✅、**alpha-to-go / break-even turnover / minimum edge 成本三件套未做** |
| P2.47 R1 | 每周滚动重训进自动化 | ✅ | `DAILY_RETRAIN=weekly` + ISO 周幂等标记 |
| P2.47 R2 | 训练写模型卡 + `model.prev.txt` | ✅ | `build_model_cards.py` + `promote_model.py` 备份 |
| P2.47 R3 | 新模型先进影子、过闸门才切 | ✅ | `scripts/promote_model.py`（候选→校验→替换→出卡，`--rollback`） |
| P2.47 **R4** | 训练/buffer 口径写进 run trace | **本轮补齐** | `run.json::model_context`（label 20d / feature 252d / train 窗口 / embargo 20d） |
| P2.47 ③ | 漂移触发的条件重训 | ❌ **研究实验** | 需要先有 IC 衰减的日常监控（`factor_state_ic.py` 只有手动跑） |
| P2.48 | 模型臂 A/B 结论 | ✅ | 结论文档 + 达标线判定 |
| P2.48 尾 | **20 期闸门判决** | ⏳ **等数据** | 影子继续累积；`production` 7/20 期 |
| P2.49 §6 | 快照更新入口 / 分红送转 / 30 观测对比 | ❌ **UI+数据** | 页面只提示"快照 N 天未更新"；除权除息未建模；账户曲线要等 30 个观测 |

## 2. 缺口归类（回答用户那句"主要差 agent 功能完善，还有 agent UI 交互？"）

**A. agent 功能（3 项，本轮已补 3/5）**
1. ~~P2.42 §4.4 上线流水线一条命令~~ → 本轮 `promote_candidate.py` + `/api/agent/actions/run`
2. ~~P2.42 §4.5 下单前 checklist~~ → 本轮 `pre_order_check.py`
3. ~~P2.47-R4 训练口径进轨迹~~ → 本轮 `model_context`
4. **P2.45-D**：PK 拆层（**等影子到 20 期**，现在拆了也判不出好坏）
5. **P2.45-E / P2.47-③ / P2.46 成本三件套**：三个**研究性实验**，各自需要一次完整 A/B + 闸门

**B. agent UI 交互（2 项，本轮补了"可点动作"）**
1. ~~执行台**没有可点动作**（只能看，不能跑）~~ → 本轮加「可点动作」面板：
   下单前 checklist（账户可选 paper/live）+ 上线评估（在册策略下拉）→ 结果、归因、下一步命令 + 复制按钮
2. ~~P2.43 挖掘工作台重排~~ → **p1_83 已补齐**（见上表）
3. P2.49 快照更新入口（页面上编辑持仓快照）—— 属"数据录入 UI"，同理未做

## 3. 本轮补的三项：验收与实测

```bash
uv run python scripts/pre_order_check.py --account live --print
# [pre-order] live 2026-10-09 -> 不该下单（有 block 行）（pass 6 / warn 2 / block 1）
#   [block] 单票上限（规则）: 上限 12%；超限 3 只 ['300229.SZ','605499.SH','300896.SZ']
#   [warn ] 资金与整手（资金）: 1 只买不起（一手 > 单票预算）：605499.SH(114.11)
#   [warn ] 逐票建议（卖出）（规则）: 2 只建议卖出：300229.SZ 拓尔思, 605499.SH 东鹏饮料
uv run python scripts/promote_candidate.py box_arm
# [promote] box_arm: 继续观察（不动生产）（KEEP）｜理由：闸门里没有 box_arm 的同名变体 ⇒ 继续观察
uv run python scripts/promote_candidate.py box_arm --apply
# [promote] --apply 必须配 --confirm（人工确认那一步不能自动化）   # 退出码 4
```

* 轨迹口径实测：`2026-10-11-daily/run.json::model_context` = `label_buffer_days=20 / feature_buffer_days=252
  （由特征名推断，显式标注）/ train_window 2025-10-13~2026-05-13（151 交易日）/ embargo_days=20`。
* 页面（执行台 → 可点动作）：点「跑一次」出 9 行检查表（含 ❌ 拦截与 ⚠️ 隐患）、点「评估」出判决 + 下一步命令；
  非法 id / 未知动作一律 400（**白名单**，否则就是命令注入口）。

## 4. 明确不做 / 等条件（不是遗漏）

* **不用日频重训**（P2.47 §1 结论，有效样本不够）；
* **不自动升 stage / 不自动替换生产模型**：都需要人工确认（本轮把"人工那一步"做成了页面上的按钮与命令，而不是自动化）；
* **实盘下单**：按 P2.42 §5 边界，需要"连续 N 个月模拟盘对照 + 闸门 PROMOTE + 下单前 checklist 全绿"三件同时满足。
