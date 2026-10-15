# P2.61 威科夫：K 线页点图（P&F）+「吸筹突破」候选策略

> 用户（2026-10-14）："看一下威科夫 这个 在 K线图里补充一下 威科夫点图。
> 另外，看看可以做一个新的策略么，注册到现有的策略池里面，和中证 500 对比一下收益曲线。"

## 一、点图（P&F）——K 线页新面板

* 后端 `backend/services/wyckoff_service.py`
  * `point_and_figure()`：经典点数图（箱 = 首日收盘 × `box_pct`，默认 2%；`reversal` 箱反转换列；
    逐日按 high/low 画 X/O），给出支撑/阻力、**横向计数（cause）**与**目标价**；
  * `wyckoff_read()`：规则化速读 —— 交易区间 / spring（下沿假跌破后收回）/ upthrust（上沿假突破后收回）/
    SOS（收盘站上上沿 + 量能 ≥1.5×20 日均量），每条都带**证据行**（哪一天、哪个价位、什么量）。
* 接口 `GET /api/stocks/{code}/wyckoff?days=250&box_pct=0.02&reversal=3&range_days=60`；
* 前端 `frontend/src/components/WyckoffChart.vue` + K 线页「威科夫点图（P&F）」面板：
  X/O 列、区间实线、计数目标点线、spring/upthrust 标注、箱%/反转箱/区间天数三个可调参数 + 证据列表。

### 踩过并修掉的两个"看起来对"的坑

1. **区间的窗口位置**：第一版把"最近 60 日"当区间、又要求"今天收盘 > 区间上沿"——
   今天的最高价本身就在区间里，`last_close > range_high` **恒为假**（实测真实票据从未读出 SOS）。
   现在区间 = 「spring 窗口之前的那 60 日」（`[t-85, t-26]`），与策略口径完全一致。
2. **spring 与 SOS 不能同窗口判定**：两者的窗口重叠时，spring 的最低点会成为区间下沿，
   `low_min < rl*0.997` 永远不成立 ⇒ 全市场 690 万行里 **0 个信号**（详见下）。
   现在 spring 在 `[t-24, t]` 找、区间取它之前，才能形成"假跌破 → 收回 → 放量突破"的时序。

## 二、策略：威科夫吸筹突破（wyckoff_arm）

规则：前 60 日横盘（区间高度 ≤30%、净涨跌 ≤15%）→ 区间内出现过 spring → 当日**放量**站上区间上沿（SOS）
→ 次日开盘买；每日最多 5 只（按区间更窄优先）；跌破入场时区间下沿×0.97 止损，或持有 20 个交易日。
成本双边 15.5bp。

**实测（2021-01-04 ~ 2026-10-12，前复权日线、次日开盘成交）**

| 指标 | 策略 | 中证500 |
|---|---|---|
| 累计 | **+47.81%** | +9.41% |
| 年化 | +7.31% | +1.64% |
| 最大回撤 | −41.79% | −41.81% |
| 卡玛 | 0.175 | 0.039 |
| 夏普（无风险=0） | 0.379 | 0.184 |

269 笔，逐笔 +1.31%、胜率 39.8%；**相对中证500 累计超额 +35.10%**（页面口径：策略净值/指数净值−1）。

**参数敏感性（同一网格里只有这一格为正）**

| 持有 | 区间 | 累计 | 夏普 |
|---|---|---|---|
| 10 日 | 60 | −60.6% | −0.54 |
| 15 日 | 60 | +0.3% | 0.00 |
| **20 日** | **60** | **+47.8%** | **0.22** |
| 30 日 | 60 | −6.3% | −0.03 |
| 40 日 | 60 | −59.5% | −0.42 |
| 20 日 | 90 | −45.6% | −0.38 |

**分年超额**：2021 +16.9pp、2022 +11.5pp、2023 +23.4pp、2024 +37.9pp、**2025 −28.8pp**、**2026 至今 −21.1pp**。

⇒ 结论：**登记为 candidate（观察候选），不进生产权重**。理由写进策略卡：参数敏感 + 近年超额转负。
（策略池的惯例正是"负期望/观察候选默认折叠曲线，只保留结论"，所以这条负信息不会被藏起来。）

## 三、登记与接线（走 P2.57 的抽象层）

`config/strategies.json` 一条声明即完成接线，`python scripts/strategy_registry.py --check` 实测
**11 条策略 / 8 个消费端，gaps = 0**：

| 消费端 | 接线 |
|---|---|
| 选股结果页 | `selection_source=cn_wyckoff_arm`（roles/columns/note 全在声明里） |
| 选股日历 | `ledger_sources=["wyckoff_arm"]` + `build_selection_ledger.read_wyckoff_arm_history()`（899 个决策日 / 5,153 行已进台账） |
| K 线自选 | 池内 `selection_source` → 侧栏分组（自动） |
| 同花顺导出 | `--sources auto` 自动带上（自动） |
| 每日刷新 | `daily_refresh.mode=run` + `empty_is_ok=true`（结构型当日无信号 = 正常，实测状态 `no_signal`，不是缺刷新） |
| 策略池曲线 | `curve.replay` → `output/research/wyckoff/curve/nav_engine.csv`（日历与中证500 逐日一致） |

## 四、顺带修掉的两个真问题

1. **曲线 nav 文件混在 `artifacts` 里会把"最新名单日"标错**：`nav_engine.csv` 带 `trade_date` 列，
   池子扫描 artifacts 时把曲线的最后一天当成名单日期 —— `box_arm` 实际最后名单日 10-09 却显示 10-12、
   `wyckoff_arm` 09-29 却显示 10-12。现已把 nav 类文件从三条策略的 `artifacts` 移除
   （曲线另有 `curve_source` / `curve.replay` 引用），并加了回归锁测试。
2. **`build_strategy_pool_performance.replay()` 的说明文字硬编码** `descriptions[strategy_id]`：
   新增策略直接 KeyError（整条绩效链会挂）。现改为读声明层的 `curve.replay.description`，缺省给通用文案。

## 五、测试与边界

* `test/test_wyckoff.py`（9 条，进每日预检）：点数图结构（X/O 交替、目标价 = 上沿 + 列数×箱）、
  构造行情能读出 spring+SOS、无形态时如实给 `range`、接口字段齐备、**信号几何**（区间必须在 spring 之前 ——
  写成同窗口会得到 0 信号）、登记接线、曲线日历与中证500 逐日一致、台账适配器、曲线文件不得进 artifacts。
* 全量预检 **45 个唯一测试文件 / 361 passed**（新增策略后 `test_agent_api`/`test_strategy_pool`/
  `test_strategy_registry` 三处硬编码期望同步改成"从 config 推导"，守卫不再"加一条策略就要改测试"）。
* 边界：不含 ST/停牌/涨跌停过滤 ⇒ 结果偏乐观；只有 2021 年以来 5.8 年样本；参数敏感与近年衰减已如实标注。

---

## 六、对齐你给的参考资料（`docs/reference/wyckoff/`）

本轮把 `docs/reference/wyckoff/` 里的四份材料读了一遍，并按其中**能完全复现**的部分补了一层：

| 参考材料 | 采用/对照 |
|---|---|
| `wyckoff点云.md`（点数图绘制与应用） | 格值 1%~3%、转向 3 格、X/O 列、横向计数目标价 = 突破位 ±（横向格数 × 格值）→ 与 `point_and_figure()` 口径一致；"买入信号：三重顶突破/上升基底"**未实现**（需要列间形态识别，未编数字） |
| `wyckoff_event_probability_cloud.md`（民生证券 2025-01 概率云） | **新增 `event_cloud()`**：逐根 K 线按研报 sigmoid 公式算事件似然度 → 取最高且 ≥60% 者**坍缩**为确定形态 → 事件映射 ±1 分、半衰期 10 日指数加权 = **WSO 分数**。已实现 14 个公式自足的事件（PS/SC/SPRING/OS/VDB/3H/3L/OKR/VSB/PSY/BC/UT/OB）；需要波段端点结构的 SOT/ASY/AS/AR&ST/TSO/LPS/LPSY/JOC/BOI/OB(Line)/OS(Line) **未实现并如实列出** |
| `wyckoff_doubao.md`（四阶段吸筹/拉升/洗盘/派发 + 方案 A 规则 / 方案 B 因子+LightGBM） | 四阶段标签与 `wyckoff_read().phase` 对应（accumulation / markup / distribution / markdown）；本轮走的是**方案 A（可解释规则）**，方案 B（多分类）留作后续 |
| `威科夫点数图的绘制与应用：股票涨跌空间判断.pdf` | 与 `wyckoff点云.md` 同源（涨跌空间 = 横向计数目标价），已按同一口径实现 |

面板实测（600519，2026-10）：点图 8 列、区间 1151.01~1363.35、计数目标 1502.65；
事件概率云当前 K 线 3H 38.0%（偏多+1）、VDB 31.6%、OS_DOWN 31.5%、3L 29.4%（偏空−1）、PS 27.2%、SC 20.4%、SPRING 3.4%；
**坍缩结论：今日无显著形态（阈值 60%）**，WSO = 0.379（半衰期 10 日 · 23 个坍缩事件）。

> 口径纪律：概率云是研报的**白盒公式**（不训练、不预测价格）；未实现的公式一律列在
> `not_implemented` 里（`test_event_cloud_structure_and_collapse` 锁住"不许悄悄省略"）。
