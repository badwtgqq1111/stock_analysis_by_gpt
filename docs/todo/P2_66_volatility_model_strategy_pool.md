# P2.66 波动率模型与策略池优化计划

依据 `docs/reference/波动率模型演变.pdf`，先校准波动率和时点口径，再把预测风险用于独立影子策略；收益改善必须通过统一执行回放验证。

## P0 基础口径（本轮实施）

- [x] 修正 `vol_yang_zhang_20`：隔夜方差 + k × 日内方差 + (1−k) × Rogers–Satchell 区间方差；20 日窗口、至少 10 个有效样本、年化口径不变。
- [x] 修正市场状态 hysteresis：只在第 N 个连续确认日切换；此前日期保持当时已知状态，`insufficient` 不提前变为候选状态。
- [x] 增加解析式基准、盘中振幅非零、前缀不变性和确认日回归测试；记录基线失败及修复后结果。

## P1 条件波动率预测与影子策略

- [x] 建立仅用截至 t 日数据的 EWMA 基线，输出未来 1/5/20 日年化预测与模型版本；已通过解析式、前缀不变性、缺测重置测试，并用 `600000.SH` 2026 年日线验证。
- [x] 使用滚动未来已实现波动率校准 EWMA；严格区分预测时点与事后评价窗口。
- [x] 在同一数据窗口比较 GARCH-t、GJR-GARCH-t 与 EWMA：QLIKE/MAE、覆盖率、稳定性、计算成本；数据不足时保留 EWMA。
- [x] 新增独立候选 `cn_ensemble_volcap`：选股及排序不变，只用预测协方差约束组合整体风险；缩放协方差时保持相关结构一致。
- [x] 新增 `arm_select_volrisk`：等权、历史波动权重、预测波动权重；分别记录净值、成本、换手和回撤，不覆盖现行策略。

## P2 策略池评价

- [x] 共同起止日、相同费用与执行规则，比较累计收益、Sharpe、Sortino、Calmar、最大回撤、CVaR 与换手（短样本比率留空并标注原因）。
- [x] 增加策略相关性、边际风险贡献及风险预算视图；实现样本量和稳定性晋级门槛（当前积累中，不晋级）。
  - [x] 研究明细落盘：`strategy_correlation.csv`、`risk_contributions.csv`、`risk_audit.csv`。
  - [x] 前端专门风险预算/贡献视图、前向留档机制和晋级门槛检验；实际前向样本继续逐日积累。

## P3 形态策略研究

- [x] 为 Donchian、箱体、威科夫研究“波动收缩→突破”的状态交互；使用次日可成交价格和统一成本开展事件研究。

## 验收与边界

P0 测试必须先红后绿，重算历史特征应标记版本变更。P1/P2 比较只使用决策时可得数据；所有实验先独立落盘，生产策略保持原配置。

## 本轮落地（2026-10-16）

- `VolumeVolatilityFactorSet.version = 0.2.0`，输出列名不变；Yang–Zhang 三个分量使用相同有效样本，暖窗 k 按实际样本数计算。
- 市场状态增加 `hysteresis_version = causal.v2`，候选被打断时重新计数；缺测当日输出 `insufficient` 并清空候选，不沿用旧状态。
- 新增 `factor_engine/portfolio/volatility.py::forecast_ewma_volatility`：收盘 t 的预测用于 t+1 以后，默认 decay=0.94、至少20个连续有效收益；缺测重置。EWMA 无均值回复，所以 1/5/20 日年化预测相同，区间累计波动按 √h 缩放。
- 定向回归 32 项通过；600000.SH 本地前复权日线 187 根（截至 2026-10-15），167 个有效预测，最新年化波动率 21.817%。这只是运行验证，不是收益改善证据。
- 未重算已有因子缓存、未训练新生产模型、未修改生产策略配置；下轮先做时点一致的预测校准与影子接线。
- 复核与回滚材料：`output/verification/volatility_foundation_20261016/`。全量 panel 训练测试曾停滞并终止（143）；本轮通过的是定向回归，不宣称全量测试通过。

复用命令（项目根目录）：

```bash
uv run --no-sync python -m pytest -q test/test_volatility_foundation.py test/test_volatility_forecast.py test/test_volume_volatility_features.py test/test_panel_training.py::test_market_regime_builds_point_in_time_labels_and_weights
```

## 第二轮执行（2026-10-16）

### P1 模型校准已运行

- `factor_engine/portfolio/volatility_calibration.py`：SciPy 约束极大似然实现零均值标准化 Student-t GARCH(1,1)/GJR(1,1)，不新增 `arch` 依赖；参数正性、平稳性约束、失败状态及参数范围均保留。
- 中证500当前本地价格版本，2024年起输入、252日训练、每20日重拟合。收盘 t 的预测评价 t+1…t+h；所有模型/1、5、20日统一在 2025-01-16 至 2026-09-09 的400个决策日比较，最长事后评价到2026-10-15。
- 1日 QLIKE：EWMA −7.303216，GARCH-t −7.386662，GJR-t −7.384284（越低越好）；95%一日区间覆盖率依次94.25%、94.75%、94.75%。EWMA波动率MAE反而更低，暂不更换影子基线。
- 分年稳定性及参数范围：`output/research/volatility_models/stability_by_year.csv`；计算时间及拟合失败数：`summary.csv`。仅跑基准指数，未证明全股票泛化或统计显著改善；重叠窗口不视为独立样本。

### 独立影子接线及回放已运行

- `scripts/run_volatility_shadow.py` 独立保存五条变体，不覆盖既有账户、名单或生产策略项。`config/strategies.json` 只追加 `cn_ensemble_volcap`、`arm_select_volrisk` 两个 research 项；手动刷新，策略池/选股源从声明派生，未接生产自动调度。
- 模型按原 rank 取前10（不足10保留原数量）；volcap 只把等权整体缩放到15%年化预测风险以内，现金不再归一化。使用60日共同样本相关矩阵配合每票 EWMA 预测对角线，保留相关结构。风险不足不删票而将当期实验权重设为现金并留审计；此次66条变体决策审计无缺数。
- ARM只取台账正权重名单，等权/历史波动倒数/EWMA预测波动倒数同名单对照。仅6个决策日、6个日收益；与模型的共有决策日另行冷启动回放，不以不同起点曲线直接排名。
- 模型族长窗（2025-09-08 至 2026-10-15，263个日收益）：等权累计 +27.12%、最大回撤 −14.99%；限仓累计 +16.58%、最大回撤 −5.60%，双向换手30.47→11.93。风险下降同时收益下降，这只是当前历史模拟观察，不是策略晋级结论。
- 三个ARM变体当前净收益分别 −6.69%、−6.53%、−6.05%。共同短窗所有变体 Sharpe/Sortino/Calmar 留空（至少252个日收益），CVaR留空（至少5个尾部观测）。
- 详细报告：`output/research/volatility_shadow/REPORT.md`。五变体的成交、订单、持仓、净值、费用、滑点估计、换手、尾部风险均落盘；注册表曲线带独立方法说明，策略池构建已实测读取到263/6个收益观测。

### 额外修正及未完成项

- 发现共享模拟引擎旧路径在 t 日循环提前执行 t+1 成交后再记 t 日净值；增加 **opt-in** `run_paper_account(..., causal_execution=True)`：t收盘定仓，t+1开盘实际执行再记账。本轮五条影子全部使用新分支；旧默认保持不变，生产历史产物未重算。新增时序、净值前缀、延期卖出和旧默认回归。
- 当前QFQ日线不是归档的逐日复权版本，台账也是历史重建；晋级前仍要做PIT数据版本与前向验证。现有引擎的停牌挂单/买卖排序等语义继续沿用，不是订单簿级成交保证。
- P2专门前端风险视图与P3形态交互事件研究保持未勾选；下一步先补前向样本和风险视图，再运行P3事件研究。未重训生产模型。
- 本轮命令、原样输出、哈希、源码快照、补丁和副本回滚验证：`output/verification/volatility_research_20261016/VERIFICATION.txt`。
- 定向回归67项通过（基础波动率、模型拟合/多步解析式/前缀不变性、影子选股/缺数、因果成交/延期卖出、注册表、费用一致性）；未运行全量训练测试。两条选股源分别实测最新8/5只，无跨日期重复名单。

复用命令（项目根目录）：

```bash
uv run --no-sync python scripts/compare_volatility_models.py --start 2024-01-01
uv run --no-sync python scripts/run_volatility_shadow.py
uv run --no-sync python scripts/strategy_registry.py --check
uv run --no-sync python scripts/build_strategy_pool_performance.py
```

## 第三轮收尾（2026-10-16）

### P2 风险视图与前向闸门已实施

- 策略池 `/#/strategies` 新增独立风险面板，`GET /api/strategies/volatility-risk` 只读产物，不触发重算。支持研究变体和历史决策日、敞口/现金/15%预算、边际/分量风险贡献、等风险份额参照与偏离。ARM未设置风险上限，显示空值，不伪造15%预算。
- 相关性按所选日期重新截断共同净值，历史日不会读取未来收益；短样本标识、空产物、接口错误和快速切换过期响应均有处理。浏览器实测最新模型8票、2025-09-08历史10票、ARM5票，历史首日共同收益0个，错误/空态可见。
- `scripts/capture_volatility_forward.py --refresh`：独立重算后按真实捕获时刻追加不可覆盖快照，保存冻结权重、当日价格、输入和执行配置哈希；SHA校验、同日收盘后且价格齐备才计有效决策。历史回填、过期名单均不记前向；同日采用最早有效快照。
- 每日流程在台账后以 `DAILY_VOLATILITY_FORWARD=1` 默认启用独立研究步骤，刷新240秒/外层300秒上限，失败仅记日志；关闭用 `DAILY_VOLATILITY_FORWARD=0`。策略注册的名单刷新仍为skip，研究自动留档与生产下单完全分离。
- 晋级只到人工复核资格：252日收益、63个配对决策、至少3个有效季度且2/3季度风险改善、波动/回撤不劣、累计收益落后≤2个百分点、逐日留档完整、冻结模型/执行协议一致、独立PIT来源审计。无自动升格或生产权重修改。协议变化不得跨版本混合，须独立审计和重新积累。
- 已实际运行首批留档；最新可用名单/行情仍为2026-10-15，捕获日2026-10-16，故有效前向决策/收益均为0，状态 `accumulating`。这是正确闸门结果，不用历史263日回放冒充前向。
- 扩展测试发现前轮新增研究策略未进入旧名单刷新状态快照；补 `refresh_strategy_lists.py --reconcile`，仅增补skip项并保留旧运行状态，不合成可运行策略的成功状态。16项注册覆盖验收通过。

### P3 三类形态事件研究已实跑

- 独立脚本 `scripts/breakout_volatility_study.py`；输入2023暖窗，信号2024-01-01至2026-10-15，2026年预设为回顾测试段；当前元数据股票池5224只，读取4609064根日线，200252条“形态事件×5/20日持有期”记录（不是200252个独立样本）。
- 收缩阈值固定：突破前一日5日收益波动/20日波动≤0.7；箱体使用首板突破之前的波动状态，回踩后才决策。Donchian/箱体/威科夫定义见 `methodology.json`，未使用全样本分位选择阈值。
- 次日市场开盘买，t+5/t+20收盘卖；统一执行假设，买5.5bp/卖15.5bp、100股整手，每事件1万元含现金尾数。缺行情/涨停跳过买入，受限退出最多顺延20个市场交易日，未退出censored与失败事件都保留。
- `summary.csv` 分年/回顾开发和测试/收缩状态，`paired_date_interaction.csv` 为同一决策日收缩组减未收缩组的日期等权净收益及市场日距HAC（lag=h+20）；不足20日不出t值。`interaction.csv`另列非配对描述值，不据此声称因果。
- 2026年同日配对5/20日净收益差：Donchian −0.296/−0.472个百分点；箱体 −1.026/−1.984个百分点；威科夫 −0.045/+1.185个百分点。所有这些|HAC t|<1，未见稳定优势，不把收缩筛选推入生产。
- 原始事件/执行失败审计/输入文件哈希/逐日覆盖/方法局限与报告位于 `output/research/breakout_volatility/`。当前股票池幸存偏差、QFQ历史版本、ST/IPO限价例外、事件重叠和截尾偏差均明确保留；这些为回顾研究，不是组合业绩或真实前向。

### 验收

- 基线67项通过；收尾扩展回归141项通过，覆盖新增时点/不可覆盖留档/哈希/冻结协议/门槛/历史API/执行约束、旧注册/名单契约/策略池/构建等。
- 前端 `vue-tsc -b && vite build` 通过；实际8000服务重启后新API HTTP200，浏览器验收已运行。手机风险表格可内部横向滚动；既有全页布局仍存在横向溢出，未将本轮构建验收等同于全站移动端重构。
- 工程与本轮研究项已完成；真实前向积累和独立PIT审计属于持续运行条件，当前不晋级。未重训生产模型、未改变生产策略权重或默认执行分支。
- 本轮基于进入本轮时的未提交工作区保存原始哈希，不回退前两轮改动；补丁、源码包、命令原样输出及副本回滚结果见 `output/verification/volatility_completion_20261016/VERIFICATION.txt`。

复用：

```bash
uv run --no-sync python scripts/capture_volatility_forward.py --refresh
uv run --no-sync python scripts/breakout_volatility_study.py --start 2024-01-01 --end 2026-10-15 --test-start 2026-01-01
uv run --no-sync python -m pytest -q test/test_volatility_completion.py
```
