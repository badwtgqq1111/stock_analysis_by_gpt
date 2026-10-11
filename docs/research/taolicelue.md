# 克隆自聚宽文章：https://www.joinquant.com/post/81344
# 标题：稳定的套利算法，回撤稳定，无未来函数
# 作者：星空梦语

from jqdata import *
import pandas as pd
import numpy as np

# ==================== 可调参数 ====================
MIN_DAILY_MONEY = 5000000      # 昨日成交额下限：500万元
MAX_DAILY_MONEY = 200000000    # 昨日成交额上限：2亿元
TARGET_POSITION_COUNT = 10     # 最多持有10只
ENTRY_DISCOUNT_PCT = -0.50     # 新开仓至少折价0.50%
HOLD_DISCOUNT_PCT = 0.00       # 已持仓折价率>=0%就可继续持有
HOLD_RANK_BUFFER = 20          # 已持仓排名在前20名内，允许继续持有
STOP_LOSS = -0.03              # 止损线 -3%
TAKE_PROFIT = 0.05             # 止盈线 +5%

def initialize(context):
    log.set_level('system', 'error')
    set_option('avoid_future_data', True)
    set_option('use_real_price', True)
    set_benchmark('000300.XSHG')
    
    set_order_cost(OrderCost(
        close_tax=0.000,
        open_commission=0.00025,
        close_commission=0.00025,
        min_commission=5,
    ), type='fund')
    
    set_slippage(FixedSlippage(0.001))
    
    run_daily(before_market_open, time='09:20')
    run_daily(trade, time='09:30')
    
    g.selected = None

def before_market_open(context):
    """使用上一交易日数据准备ETF候选池（无未来函数）"""
    try:
        etf_list = get_all_securities(['etf'], context.previous_date).index.tolist()
        
        money_df = history(
            count=1,
            unit='1d',
            field='money',
            security_list=etf_list,
            df=False
        )
        money_df = pd.DataFrame(money_df).T
        money_df.columns = ['money']
        
        liquid = money_df[
            (money_df['money'] > MIN_DAILY_MONEY) & 
            (money_df['money'] < MAX_DAILY_MONEY)
        ]
        
        # 使用正确的字段名 unit_net_value
        nav_df = get_extras(
            'unit_net_value',
            liquid.index.tolist(),
            end_date=context.previous_date,
            count=1,
            df=True
        ).T
        nav_df.columns = ['nav']
        
        g.selected = liquid.join(nav_df).dropna()
        log.info(f"盘前候选池准备完毕，共 {len(g.selected)} 只ETF")
        
    except Exception as e:
        log.error(f'盘前数据准备失败，当天停止交易：{e}')
        g.selected = None

def trade(context):
    """日线回测下，09:30 使用当日开盘价进行交易（无未来函数）"""
    if g.selected is None or g.selected.empty:
        return
        
    current_data = get_current_data()
    positions = list(context.portfolio.positions.keys())
    
    # ==================== 1. 计算实时折价率 ====================
    df = g.selected.copy()
    valid_codes = []
    open_prices = []
    
    for code in df.index:
        if current_data[code].paused:
            continue
        day_open = current_data[code].day_open
        if day_open and day_open > 0:
            valid_codes.append(code)
            open_prices.append(day_open)
            
    if not valid_codes:
        return
        
    df = df.loc[valid_codes]
    df['open'] = open_prices
    df['discount'] = (df['open'] / df['nav'] - 1) * 100
    
    # ==================== 2. 生成目标持仓 ====================
    ranked = df.sort_values(by='discount', ascending=True)
    position_set = set(positions)
    
    hold_pool = ranked[
        (ranked['discount'] <= HOLD_DISCOUNT_PCT) | 
        (ranked.index.isin(position_set) & 
         (ranked.index.isin(ranked.head(HOLD_RANK_BUFFER).index)))
    ]
    retained = [code for code in hold_pool.index if code in position_set]
    
    entrants = ranked[ranked['discount'] <= ENTRY_DISCOUNT_PCT].index.tolist()
    
    target = retained[:TARGET_POSITION_COUNT]
    for code in entrants:
        if code not in target:
            target.append(code)
        if len(target) >= TARGET_POSITION_COUNT:
            break

    # ==================== 3. 执行卖出（使用限价单指定开盘价）====================
    for code in positions:
        pos = context.portfolio.positions[code]
        if pos.total_amount == 0:
            continue
            
        price = current_data[code].day_open
        if price is None or np.isnan(price) or price <= 0:
            continue
            
        cost = pos.avg_cost
        pnl = (price - cost) / cost
        
        if pnl <= STOP_LOSS:
            order_target(code, 0, style=LimitOrderStyle(price))
            log.info(f"止损卖出 {code} @ {price:.3f}，亏损 {pnl:.2%}")
            continue
            
        if pnl >= TAKE_PROFIT:
            order_target(code, 0, style=LimitOrderStyle(price))
            log.info(f"止盈卖出 {code} @ {price:.3f}，盈利 {pnl:.2%}")
            continue
            
        if code not in target:
            order_target(code, 0, style=LimitOrderStyle(price))
            log.info(f"轮动卖出 {code} @ {price:.3f}")

    # ==================== 4. 执行买入（使用限价单指定开盘价）====================
    buy_list = [code for code in target if code not in context.portfolio.positions]
    if not buy_list:
        return
        
    each = min(
        context.portfolio.available_cash / len(buy_list),
        context.portfolio.total_value / TARGET_POSITION_COUNT
    )
    
    for code in buy_list:
        if current_data[code].paused:
            continue
        price = current_data[code].day_open
        if not price or price <= 0:
            continue
            
        amount = int(each / price / 100) * 100
        if amount > 0:
            # ★ 使用 LimitOrderStyle 指定限价，替代不被支持的 price 参数
            order_value(code, amount * price, style=LimitOrderStyle(price))
            log.info(f"买入 {code} @ {price:.3f}，金额 {amount * price:.2f}")