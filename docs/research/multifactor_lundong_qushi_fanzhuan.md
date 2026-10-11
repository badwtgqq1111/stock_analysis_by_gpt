# 克隆自聚宽文章：https://www.joinquant.com/post/81316
# 标题：多因子ETF轮动 + 趋势/震荡自适应切换策略
# 作者：Amber218

# ============================================================
# 聚宽平台 - 多因子ETF轮动 + 趋势/震荡自适应切换策略
# 策略逻辑：
#   1. 多因子综合评分选出4只ETF
#   2. 每日监控趋势状态
#   3. 趋势行情 → 均线趋势策略
#   4. 震荡行情 → 动量/反转策略
# ============================================================

import numpy as np
import pandas as pd
from scipy import stats
from jqdata import *


# ==================== 初始化 ====================
def initialize(context):
    # --- 系统设置 ---
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option("avoid_future_data", True)
    set_slippage(FixedSlippage(0.002))
    set_order_cost(OrderCost(
        open_tax=0, close_tax=0,
        open_commission=0.0002, close_commission=0.0002,
        close_today_commission=0, min_commission=5
    ), type='fund')
    log.set_level('system', 'error')

     # --- ETF候选池（12只，覆盖宽基/行业/商品/债券/跨境）---
    g.etf_pool = [
        # 宽基
        '510300.XSHG',  # 沪深300ETF
        '510500.XSHG',  # 中证500ETF
        '159915.XSHE',  # 创业板ETF
        # 行业
        '512010.XSHG',  # 医药ETF
        '512660.XSHG',  # 军工ETF
        '512880.XSHG',  # 证券ETF
        '516160.XSHG',  # 新能源ETF
        # 商品
        '518880.XSHG',  # 黄金ETF
        # 债券（替代原511010）
        '511160.XSHG',  # 国债ETF东财（单价约1.02元）
        # 跨境
        '159920.XSHE',  # 恒生ETF
        '513100.XSHG',  # 纳指ETF
        '513500.XSHG',  # 标普500ETF
    ]


    # --- 策略参数 ---
    g.hold_num = 4           # 持仓ETF数量
    g.lookback = 63          # 多因子评分回看窗口（约3个月）
    g.momentum_short = 21    # 短期动量窗口
    g.ma_period = 20         # 均线周期
    g.trend_ratio = 0.5      # 趋势判定阈值：50%以上ETF在均线上方即视为趋势

    # --- 全局状态 ---
    g.current_mode = 'unknown'   # 'trend' / 'shock' / 'unknown'

    # --- 定时任务 ---
    run_daily(trend_monitor, time='14:50')                 # 每日尾盘监控趋势
    run_weekly(weekly_rebalance, weekday=1, time='09:35')  # 每周第1个交易日调仓


# ==================== 多因子评分 ====================
def calculate_scores(etf_pool, lookback=63):
    """
    多因子综合评分：
      因子1: 趋势强度 R²（线性回归拟合优度）
      因子2: 风险调整收益 = 年化收益 / 波动率
      因子3: 短期动量（21日收益率）
      因子4: 波动率惩罚（低波动加分）
    返回按评分降序排列的 etf 列表
    """
    scores = {}
    for etf in etf_pool:
        try:
            df = attribute_history(etf, lookback, '1d', ['close'])
            if len(df) < lookback * 0.8:
                continue

            close = df['close'].values
            y = np.log(close)
            x = np.arange(len(y))

            # 因子1: 趋势强度 R²
            slope, intercept, r_value, p_value, std_err = stats.linregress(x, y)
            r_squared = r_value ** 2

            # 因子2: 年化收益与风险调整收益
            daily_growth = np.exp(slope) - 1
            annualized_ret = (1 + daily_growth) ** 252 - 1
            volatility = np.std(np.diff(y)) * np.sqrt(252)
            risk_adj = annualized_ret / (volatility + 1e-6)

            # 因子3: 短期动量
            if len(close) >= g.momentum_short + 1:
                momentum = close[-1] / close[-g.momentum_short - 1] - 1
            else:
                momentum = 0

            # 因子4: 波动率惩罚
            vol_penalty = 1.0 / (volatility + 0.01)

            # 综合评分
            score = (
                0.30 * risk_adj +
                0.25 * r_squared * np.sign(annualized_ret) +
                0.30 * momentum +
                0.15 * vol_penalty
            )
            scores[etf] = score
        except Exception as e:
            log.error(f"评分计算失败 {etf}: {e}")
            continue

    if not scores:
        return []

    # 标准化后排序
    score_series = pd.Series(scores)
    score_series = (score_series - score_series.mean()) / (score_series.std() + 1e-6)
    score_series = score_series.sort_values(ascending=False)
    return list(score_series.index)


# ==================== 趋势判断 ====================
def judge_trend(target_etfs):
    """
    判断目标ETF池整体是否处于趋势行情。
    规则：若 >= trend_ratio 比例的ETF价格位于MA20之上，则判定为趋势行情。
    """
    above_ma = 0
    valid = 0
    for etf in target_etfs:
        try:
            df = attribute_history(etf, g.ma_period + 5, '1d', ['close'])
            if len(df) < g.ma_period:
                continue
            ma = df['close'].iloc[-g.ma_period:].mean()
            current = df['close'].iloc[-1]
            valid += 1
            if current > ma:
                above_ma += 1
        except:
            continue

    if valid == 0:
        return 'shock'

    ratio = above_ma / valid
    if ratio >= g.trend_ratio:
        return 'trend'
    else:
        return 'shock'


# ==================== 趋势策略（均线） ====================
def trend_strategy(context, target_etfs):
    """
    均线趋势策略：
    持有价格在MA20上方、且趋势评分最高的ETF。
    若持仓ETF跌破MA20，则卖出，换入下一个符合条件的ETF。
    """
    valid_etfs = []
    for etf in target_etfs:
        try:
            df = attribute_history(etf, g.ma_period + 5, '1d', ['close'])
            if len(df) < g.ma_period:
                continue
            ma = df['close'].iloc[-g.ma_period:].mean()
            current = df['close'].iloc[-1]
            if current > ma:
                valid_etfs.append(etf)
        except:
            continue

    if not valid_etfs:
        # 无符合条件的，清仓
        for etf in list(context.portfolio.positions.keys()):
            order_target_value(etf, 0)
        log.info("【趋势策略】无ETF在均线上方，清仓")
        return

    # 按评分排序，取前 hold_num 只
    ranked = calculate_scores(valid_etfs, g.lookback)
    target_list = ranked[:g.hold_num]

    execute_rebalance(context, target_list)
    log.info(f"【趋势策略】目标持仓: {target_list}")


# ==================== 动量/反转策略 ====================
def momentum_reversal_strategy(context, target_etfs):
    """
    震荡行情下的动量/反转策略：
    - 若存在正动量ETF，取动量最高的Top2持有（动量策略）
    - 若所有ETF动量均为负，取跌幅最大的1只ETF持有（反转策略）
    """
    momentum_dict = {}
    for etf in target_etfs:
        try:
            df = attribute_history(etf, g.momentum_short + 5, '1d', ['close'])
            if len(df) < g.momentum_short:
                continue
            ret = df['close'].iloc[-1] / df['close'].iloc[-g.momentum_short - 1] - 1
            momentum_dict[etf] = ret
        except:
            continue

    if not momentum_dict:
        return

    sorted_etfs = sorted(momentum_dict.items(), key=lambda x: x[1], reverse=True)
    positive = [(e, r) for e, r in sorted_etfs if r > 0]

    if positive:
        # 动量策略：持有正动量Top2
        target_list = [e for e, r in positive[:2]]
        log.info(f"【动量策略】正动量Top2: {target_list}, 动量值: {[round(r,4) for _, r in positive[:2]]}")
    else:
        # 反转策略：所有ETF下跌，选跌幅最大的1只
        worst = sorted_etfs[-1]
        target_list = [worst[0]]
        log.info(f"【反转策略】超跌标的: {worst[0]}, 21日收益: {round(worst[1], 4)}")

    execute_rebalance(context, target_list)


# ==================== 调仓执行 ====================
def execute_rebalance(context, target_list):
    """执行调仓：先卖后买，等权分配"""
    # 卖出不在目标列表中的
    hold_list = list(context.portfolio.positions.keys())
    for etf in hold_list:
        if etf not in target_list:
            order_target_value(etf, 0)
            log.info(f"卖出 {etf}")

    # 买入目标标的
    if not target_list:
        return
    cash_per_etf = context.portfolio.total_value / len(target_list)
    for etf in target_list:
        order_target_value(etf, cash_per_etf)
        log.info(f"买入 {etf}, 目标市值 {cash_per_etf:.0f}")


# ==================== 每日趋势监控 ====================
def trend_monitor(context):
    """每日尾盘监控趋势状态，必要时切换策略"""
    current_holds = list(context.portfolio.positions.keys())
    if not current_holds:
        return

    new_mode = judge_trend(current_holds)

    if new_mode != g.current_mode:
        log.info(f"趋势状态切换: {g.current_mode} → {new_mode}")
        g.current_mode = new_mode
        if new_mode == 'trend':
            trend_strategy(context, current_holds)
        else:
            momentum_reversal_strategy(context, current_holds)


# ==================== 每周调仓 ====================
def weekly_rebalance(context):
    """每周第1个交易日执行选股和调仓"""
    # 多因子选股：选出评分最高的4只ETF
    ranked = calculate_scores(g.etf_pool, g.lookback)
    if len(ranked) < g.hold_num:
        log.warning("有效ETF不足，跳过调仓")
        return

    target_etfs = ranked[:g.hold_num]
    log.info(f"本周多因子选股结果: {target_etfs}")

    # 判断趋势状态
    g.current_mode = judge_trend(target_etfs)
    log.info(f"当前趋势状态: {g.current_mode}")

    # 根据状态执行对应策略
    if g.current_mode == 'trend':
        trend_strategy(context, target_etfs)
    else:
        momentum_reversal_strategy(context, target_etfs)