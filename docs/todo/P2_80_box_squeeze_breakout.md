# P2.80 箱体突破回踩：低波收口拐点修正

## 背景

用户 2026-10-22 指出：`box_arm` 的箱体上下价差太大，突破时机应该是「从低波动向高波动
变化的拐点」，低波动阶段的上下价差不能太大；原来的突破看起来都像是「涨完了」。

## 诊断

原口径：60 日振幅 ≤45%、60 日无涨停、首板创 60 日新高、回调 ≥3%、回踩箱体上沿
−3%~+1%、T+1 开盘买/当日收盘卖。

问题不在「突破回踩」四个字，而在「箱体」太宽：

* 45% 的 60 日振幅会把上升中继当成横盘；
* 原口径最新名单的 60 日涨幅 +15%~+19%、距 60 日低点 +21%~+23%，已经是趋势中段；
* 因此「突破」经常发生在趋势末端，而不是低波收口后的第一次扩张。

## 修正

新增独立变体 `box_squeeze_arm`，不改原 `box_arm` 的历史产物：

1. 60 日振幅 ≤25%，60 日无涨停；
2. 突破前 20 日振幅 ≤10%，且 ≤50%×60 日振幅（低波收口）；
3. 首板收盘创 60 日新高；
4. 1~5 日内回调 ≥3%，回踩箱体上沿 −3%~+1% 不破；
5. T+1 开盘买、当日收盘卖，weekly×5、按箱体振幅升序、15.5bp/边。

## 结果

事件层（263 笔）：毛 +0.890%、净 +0.580%、胜率 57%、日度聚类 NW t +3.64。

组合层（252 笔 / 207 个决策日）：累计 +127.5%、年化 +8.8%、最大回撤 −18.7%、
夏普 0.68、NW t +2.22；同期中证500 +5.4%。

相邻口径 weekly×10、daily×5、daily×10 都是 263 笔 / +158.2%，因为信号太稀疏、
持仓上限从未触发；这不能当成参数稳健性的证据。

年度拆解显示收益集中在 2023（+62.5%，61 笔、逐笔 +1.51%）；剔除 2023 后累计 +40.0%、
年化约 +3.4%、NW t +1.12。结论是「形态修正成立，但还不具备稳定 alpha 的证据」。

## 产物

* 名单：`output/results_cn/box_squeeze_arm_selection.csv`
* 历史：`output/results_cn/box_squeeze_arm_history/`
* 曲线：`output/research/box_breakout/box_squeeze_arm/curve/{curve.json,nav_engine.csv}`
* 报告：`output/research/box_breakout/BOX_SQUEEZE.md`
* 登记：`config/strategies.json` → `box_squeeze_arm`（candidate / research，
  `variant_of=box_arm`，每日名单刷新 `mode=run`、`empty_is_ok=true`）

## 验收

* `python scripts/select_box_breakout_arm.py --arm-id box_squeeze_arm ...` 只写
  `box_squeeze_arm_*` 与 `output/research/box_breakout/box_squeeze_arm/`，不覆盖原 `box_arm`；
* `/api/selection?source=cn_box_squeeze_arm` 能读到名单；
* 选股台账包含 `source=box_squeeze_arm`；
* 策略池/K 线自选/投研执行台族树都能看到该变体；
* 全量预检通过。

## 边界

252~263 笔样本、低频、只做一日持有；未建模涨跌停/停牌/整手；涨停用 9.5% 近似。
只作观察候选，不参与生产权重。
