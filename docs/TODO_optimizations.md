# 优化 TODO — 基于论文+统计悖论+LLM报告的诊断

> 数据来源：动量反转论文 (Jegadeesh 2025)、统计五大悖论、2026-05-30 LLM 选股报告
> 生成日期：2026-05-30

---

## 速查矩阵

| # | 优先级 | 优化项 | 改动文件 | 预计耗时 | 预期效果 |
|---|--------|--------|---------|---------|---------|
| 1 | **P0** | 过热平方罚分 | `portfolio.py` | 30min | 过热80+不入Top10 |
| 2 | **P0** | 换手率硬过滤 | `portfolio.py` / `select_stocks.py` | 20min | 自动排除停牌股 |
| 3 | **P1** | win_rate James-Stein 收缩 | `portfolio.py` | 1h | 100%胜率陷阱消除 |
| 4 | **P1** | 凯利动态仓位 | `portfolio.py` / `backtest_ops.py` | 2h | 高风险期自动降仓 |
| 5 | **P1** | 反转污染惩罚项 | `portfolio.py` | 1h | 假突破识别 |
| 6 | **P1** | skip-1m动量因子 | `custom_factors.py` | 30min | 填补正向动量空缺 |
| 7 | **P1** | 差异化持有期 | `current_strategy.py` | 1h | 减少过早/过晚离场 |
| 8 | **P1** | forward_return 加 skip 参数 | `forward_metrics.py` | 1h | 标签质量提升 |
| 9 | **P2** | 噪声交易代理因子 | `custom_factors.py` | 2h | HK市场针对性优化 |
| 10 | **P2** | 用 Expected Shortfall 替代标准差 | `portfolio.py` / 新文件 | 3h | 尾部风险管理 |
| 11 | **P3** | 盈利公告日期管线 | `data/` 新模块 | 1-2d | 财报窗口期规避 |
| 12 | **P3** | GARCH 波动率建模 | 新文件 | 1d | 波动率聚类捕捉 |

---

## P0 — 立即修复（本次）

### 1. 过热罚分改为非线性

**诊断**：LLM 报告 Top10 中 5 只 `overheat>=80`，但线性罚分 `overheat * 0.14` 没拦住。正态假设下 85 是 ~5σ 事件不该发生，实际频繁出现——幂律分布。

**依据**：厚尾风险（`five_statistical_paradoxes.md` §5）

**改动**：[backtest_engine/portfolio.py](stock_analysis_by_gpt/backtest_engine/portfolio.py) ~L584

```python
# 当前
ranking_score = ... - overheat_penalty_score * 0.14

# 优化
if overheat_penalty_score > 50:
    overheat_term = (overheat_penalty_score / 50) ** 2 * overheat_penalty_score * 0.14
else:
    overheat_term = overheat_penalty_score * 0.14
```

**验证**：重跑 Top10 选股，确认 overheat>80 不入前十。

### 2. 换手率硬过滤

**诊断**：LLM 报告 5/10 换手率为 0（停牌/数据缺失），但未被过滤。

**依据**：LLM 报告（`2026-05-30_llm.md`）

**改动**：[cli/select_stocks.py](stock_analysis_by_gpt/cli/select_stocks.py) `main_select_stocks()` 默认值

```python
# 当前默认 None → 不过滤
# 优化：默认过滤日成交额 < 100 万港元
min_daily_turnover = 100 if min_daily_turnover is None else min_daily_turnover
```

---

## P1 — 本周完成

### 3. win_rate 向先验均值收缩

**诊断**：09900 智云科技的 100% 胜率来自小样本（trade_count 极少），LLM 立刻识别出问题但排名公式给了最高权重。

**依据**：James-Stein 悖论（`five_statistical_paradoxes.md` §2）

**改动**：[backtest_engine/portfolio.py](stock_analysis_by_gpt/backtest_engine/portfolio.py) ~L566

```python
# 当前：原始 win_rate 直接进入公式
win_rate_pct = backtest_stats["win_rate"]

# 优化：向先验均值收缩
prior_wr = 55.0  # 全市场平均
credibility = min(trade_count / 20, 1.0)  # 20 笔以上才完全信任
win_rate_pct = prior_wr + credibility * (win_rate_pct - prior_wr)
```

**验证**：检查小样本股票的 shrunk_win_rate 是否合理。

### 4. 凯利动态仓位

**诊断**：LLM 建议"整体仓位控制在 5 成以下"，但系统固定等权。数学上：遍历性破缺 → 算术正期望策略长期乘法归零。

**依据**：遍历性破缺（`five_statistical_paradoxes.md` §4）

**改动**：[backtest_engine/portfolio.py](stock_analysis_by_gpt/backtest_engine/portfolio.py) `backtest_portfolio()` 和排名展示

```python
# 新增：基于凯利公式的动态仓位建议
p = portfolio_result["estimated_portfolio_win_rate"] / 100
b = avg_win_return / abs(avg_loss_return) if avg_loss_return else 2.0
kelly_f = max(0, (p * b - (1 - p)) / b)
half_kelly = kelly_f * 0.5
position_ratio = half_kelly  # 替代等权 1/N
```

**验证**：高 overheat 时期仓位自动下降。

### 5. 反转污染惩罚项

**诊断**：HK 市场散户主导 → 反转效应强 → pre_breakout 可能是噪声交易推高的假突破。论文印证：反转与动量利润 ρ=-0.313。

**依据**：动量反转论文（`momentum_reversal_jegadeesh2025.md` §2/§5）

**改动**：[backtest_engine/portfolio.py](stock_analysis_by_gpt/backtest_engine/portfolio.py) ~L584 ranking_score

```python
# 1 月收益率 → 越高反转风险越大
return_1m = (latest_close - close_20d_ago) / close_20d_ago
reversal_risk = max(return_1m - 0.05, 0)  # >5% 月收益开始惩罚
reversal_penalty = reversal_risk * 100 * REVERSAL_WEIGHT  # ~0.05
```

**验证**：1 月暴涨股排名下降。

### 6. skip-1m 动量因子

**诊断**：当前 5 个手工因子全为负 IC（反转型），因子库缺正向动量因子。

**依据**：动量反转论文（§4）

**改动**：[factor_engine/expressions/custom_factors.py](stock_analysis_by_gpt/factor_engine/expressions/custom_factors.py) `Alpha158HKFactorSet.transform()`

```python
# 新增因子
df['momentum_12m_skip_1m'] = df['close'].shift(21) / df['close'].shift(252) - 1
df['short_term_reversal_1m'] = df['close'] / df['close'].shift(21) - 1
```

**验证**：跑 IC 分析确认动量因子 IC 方向为正。

### 7. 差异化持有期

**诊断**：所有 setup 共用一个 holding_horizon=60。论文证明反转有效期~1月、动量有效期 3-12 月，中间 2 个月是信号空白期。

**依据**：动量反转论文（§3 过渡模式）+ 反正弦定律（随机游走幻觉）

**改动**：[strategy_signals/current_strategy.py](stock_analysis_by_gpt/strategy_signals/current_strategy.py) `identify_buy_signals()`

```python
# 当前：统一 60
# 优化
if setup_type == "bottom_rebound":
    holding_horizon = 40   # 纯反转，短持
elif setup_type == "pre_breakout":
    holding_horizon = 80   # 动量型，长持
elif setup_type == "momentum_continuation":
    holding_horizon = 120  # 强动量，更长
```

### 8. forward_return 加 skip 参数

**诊断**：当前 T+1→T+61 直接计算 forward_return_60，t-1 月反转效应污染标签。论文证明跳过 t-1 月后动量信号更纯。

**依据**：动量反转论文（§4）

**改动**：[core/forward_metrics.py](stock_analysis_by_gpt/core/forward_metrics.py) `_compute_forward_metrics()`

```python
# 新增参数 skip_days=0
def _compute_forward_metrics(self, df, horizons, execution_delay=1, skip_days=0):
    future_close = df["Close"].shift(-(horizon + execution_delay + skip_days))
    entry_close = df["Close"].shift(-(execution_delay + skip_days))
```

---

## P2 — 下周候选

### 9. 噪声交易代理因子

**问题**：论文用零售订单失衡测噪声（φ≈-0.11, t=-4.78），HK 无订单级数据。

**方案**：用 `turnover_vol_20d / ln(mkt_cap)` 作为替代

**改动**：[factor_engine/expressions/custom_factors.py](stock_analysis_by_gpt/factor_engine/expressions/custom_factors.py)

### 10. Expected Shortfall 替代标准差

**问题**：当前 `Risk_Penalty` 基于滚动标准差，隐含正态假设。厚尾分布下方差本身就不稳定。

**方案**：用历史模拟 ES_95 替代

**改动**：新增 [core/tail_risk.py](stock_analysis_by_gpt/core/tail_risk.py)，修改 portfolio.py 的风险计算

---

## P3 — 长期储备

### 11. 盈利公告日期管线

**数据源**：HKEX 上市公司公告 RSS / akshare 财报日期接口
**用途**：公告前后 5 天降低反转因子权重（论文 §3 预测 1：反转在公告后减弱 ~70%）

### 12. GARCH 波动率建模

**问题**：厚尾文档 §5 指出波动率聚类（大波动后更大波动），滚动标准差捕捉不到
**方案**：用 GARCH(1,1) 的条件方差替代滚动 std，作为 Risk_Penalty 输入

---

## 完成标准

- [ ] P0-1: 重跑选股，overheat>80 的股票不再进入 Top10
- [ ] P0-2: 换手率=0 的股票在选股阶段被自动排除
- [ ] P1-3: 小样本股票 win_rate 不再出现 100%
- [ ] P1-4: 排名报告输出凯利仓位建议
- [ ] P1-5: 1 月暴涨股排名下降 3-5 位
- [ ] P1-6: 新因子 IC 分析完成，方向符合预期
- [ ] P1-7: 不同 setup 的 holding_horizon 差异化
- [ ] P1-8: forward_return_60_skip20 标签实现
