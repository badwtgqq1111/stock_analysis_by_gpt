# 克隆自聚宽文章：https://www.joinquant.com/post/81318
# 标题：首板涨停+回调+企稳+低吸-年化79%-第1版
# 作者：顶级理解

from jqdata import *
from jqfactor import *
import pandas as pd
from datetime import datetime, timedelta, date


# 初始化设置
def initialize(context):
    unschedule_all()  
    context.hg = {}
    context.lw = {}
    context.preClose = {}
    context.sts = []
    g.n_days_limit_up_list = []   # 重新初始化列表

    # ===== 框架外壳：基准 / 执行设置 =====
    set_benchmark('000905.XSHG')            # 框架基准
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)   # 避免未来函数
    set_slippage(FixedSlippage(0.005))      # 滑点
    set_order_cost(OrderCost(open_tax=0, close_tax=0.0005, open_commission=0.0003,
                             close_commission=0.0003, close_today_commission=0,
                             min_commission=5), type='fund')  
    log.set_level('order', 'error')         # 框架外壳
    log.set_level('system', 'error')        # 原策略

    # ===== 策略排程=====
    run_daily(buy, '09:30:00')  # 竞价完就买入
    run_daily(sell, time='09:40', reference_security='000300.XSHG')
    run_daily(sell, time='10:40', reference_security='000300.XSHG')
    run_daily(sell, time='11:00', reference_security='000300.XSHG')
    run_daily(sell, time='13:01', reference_security='000300.XSHG')
    #run_daily(sell, time='10:40', reference_security='000300.XSHG')
    run_daily(sell, time='14:50', reference_security='000300.XSHG')
    # 框架外壳：复盘打印
    run_daily(print_trade_info, time='15:30')


def handle_data(context, data):
    stime = context.current_dt.strftime("%H%M")
    if int(stime) > 1000:
        return
    current_data = get_current_data()
    for s in list(context.portfolio.positions):  # 亏了-7以上止损
        p = current_data[s].high_limit / 1.1
        if context.portfolio.positions[s].closeable_amount != 0 and current_data[s].last_price <= current_data[s].low_limit:  # avg_cost当前持仓成本
            order_target_value(s, 0)


def sell(context):
    stime = context.current_dt.strftime("%H%M")
    current_data = get_current_data()
    #print(111)
    # 根据时间执行不同的卖出策略
    if stime == '0940':
        for s in list(context.portfolio.positions):  # 亏了-7以上止损
            p = current_data[s].high_limit / 1.1
            if ((context.portfolio.positions[s].closeable_amount != 0) and (current_data[s].last_price / p) <= 0.95):  # avg_cost当前持仓成本
                #order_target_value(s, 0)
                pass
    if stime == '1040' or stime == '1100':
        for s in list(context.portfolio.positions):  # 亏了-7以上止损
            p = current_data[s].high_limit / 1.1
            if ((context.portfolio.positions[s].closeable_amount != 0) and (current_data[s].last_price / p) <= 0.96):  # avg_cost当前持仓成本
                order_target_value(s, 0)

    elif stime == '1301':
        for s in list(context.portfolio.positions):  # 上午有利润就跑
            if ((context.portfolio.positions[s].closeable_amount != 0) and (current_data[s].last_price < current_data[s].high_limit) and (current_data[s].last_price > context.portfolio.positions[s].avg_cost)):  # avg_cost当前持仓成本
                order_target_value(s, 0)
    elif stime == '1450':
        for s in list(context.portfolio.positions):  # 上午没有利润尾盘确认
            if ((context.portfolio.positions[s].closeable_amount != 0) and (current_data[s].last_price < current_data[s].high_limit)):  # closeable_amount可卖出的仓位
                order_target_value(s, 0)


# 交易函数群
def buy(context):
    qualified_stocks = prepare_stock_list(context)
    if len(qualified_stocks) > 5:
        qualified_stocks = qualified_stocks[0:5]

    stime = context.current_dt.strftime("%Y-%m-%d %H:%M:%S")
    #if len(qualified_stocks)==0 or len(qualified_stocks)>4 :
    #    return
    current_data = get_current_data()
    # 始终取竞昨比最大的前几个进行补仓
    cnt = 5 - len(context.portfolio.positions)
    if len(qualified_stocks) > cnt:
        qualified_stocks = qualified_stocks[0:cnt - 1]
    # 每天最多买2只
    #if len(qualified_stocks)>3:
    #   qualified_stocks =qualified_stocks[0:2]
    if qualified_stocks:
        value = context.portfolio.available_cash / cnt
        f1 = len(qualified_stocks) == 1 and len(context.portfolio.positions) == 0
        #if f1:
        #    value=value/5*5
        for s in qualified_stocks:
            # 下单   #至少够买1手
            if context.portfolio.available_cash / current_data[s].last_price > 100:
                order_value(s, value, MarketOrderStyle(current_data[s].day_open))
                print('买入' + s)
                print('———————————————————————————————————')
    
    print(f"买入{len(qualified_stocks)}只股票")


# 选股函数群
# 每日初始股票池，筛选昨日涨停的票
# 定义股票池
def set_stockpool(context):
    yesterday = context.previous_date
    # 获取当日竞价涨幅为涨停的票
    context.openHigh = []
    context.buyed = []
    initial_list = get_all_securities('stock', yesterday).index.tolist()

    return initial_list


def prepare_stock_list(context):
    context.hg = {}
    context.lw = {}
    #set_option("match_by_signal", True)
    context.preClose = {}
    context.sts = []
    # print('22222')
    today = context.current_dt.date()
    #print('222')
    current_data = get_current_data()
    context.currentDay = current_data
    yesterday = context.previous_date
    initial_list = set_stockpool(context)
    lst = []
    sts = []
    g = {}   

    hl_list = get_hl_stock(initial_list, yesterday, 1)     # 昨日涨停
    for stock in hl_list:
        sts.append(stock)
    #print(sts)
    for s in sts:
        security_info = get_security_info(s)

        stock_name = security_info.display_name
        if '退' in stock_name or 'ST' in stock_name or s[0] == '3' or s[0:3] == '688' or s[0] == '9':
            continue
        df = attribute_history(s, 5, '1d', fields=['open', 'close', 'high_limit', 'low', 'volume', 'pre_close'], skip_paused=True)

        df_reversed = df.iloc[::-1]
        #print(df_reversed)
        zr = df_reversed['high_limit'][0]
        # 排除掉昨日的一字板
        if df_reversed['open'][0] >= df_reversed['high_limit'][0] or df_reversed['low'][0] >= df_reversed['high_limit'][0]:
            continue
        #股价条件
        #if df_reversed['close'][0]<1 :
        #    continue
        df1 = attribute_history(s, 30, '1d', fields=['open', 'close', 'high_limit', 'low', 'high', 'pre_close'], skip_paused=True)
        # 最低价和昨日价格
        x = df_reversed['close'][0] / df1['low'].min()
        #涨幅过高
        if x > 3:
            continue
        #if  df_reversed['close'][0]>100:
        #    continue
        #if  '石化' in stock_name:
        #    print(df_reversed)
        # 记录连板数
        lbs = 0
        # 连续1字板的天数
        lbs_yz = 0
        for index, row in df_reversed.iterrows():
            if row[1] == row[2]:
                lbs = lbs + 1
            else:
                break
        for index, row in df_reversed.iterrows():
            if row[3] == row[2]:
                lbs_yz = lbs_yz + 1
            else:
                break

        if lbs != 1:
            continue
        # 倒数第二天不能涨停
        if df_reversed['close'][1] >= df_reversed['high_limit'][1]:
            continue
        d1 = context.previous_date.strftime("%Y-%m-%d")
        # 昨日分时是否烂板
        df_price = get_price(s, start_date=d1 + ' 09:30:00', end_date=d1 + ' 15:30:00', frequency='1m', fields=['close', 'open', 'low', 'high'])
        #print(df_price)
        # 首次封板
        scpb = None
        # 封板后回封次数
        hfsj = 0
        for index, row in df_price.iterrows():
            dt = index
            if row['high'] >= zr and scpb is None:
                scpb = dt
            if scpb is not None and dt > scpb and row['low'] != zr:
                hfsj = hfsj + 1

        if '002356' in s:
            #print(ca)
            print(f'{hfsj}')
        # 炸板时长
        if hfsj <= 2:
            continue

        # 昨日开盘涨幅
        if df_reversed['low'][0] / df_reversed['pre_close'][0] - 1 < -0.03:
            continue

        turnover_ratio_data = get_valuation(s, start_date=context.previous_date, end_date=context.previous_date, fields=['turnover_ratio', 'circulating_cap', 'market_cap', 'circulating_market_cap', 'pe_ratio_lyr', 'capitalization'])
        #市值小于200亿
        if turnover_ratio_data.empty or turnover_ratio_data['market_cap'][0] >= 300 or turnover_ratio_data['market_cap'][0] < 10:
            continue
        ca = get_call_auction(s, start_date=context.current_dt.date(), end_date=context.current_dt.date())
        if len(ca) == 0 or ca is None:
            continue
        #print(s)
        # 竞价成交量
        jkp = ca['current'][0] / df_reversed['close'][0] - 1
        jl = ca['volume'][0]
        je = round(ca['money'][0] / 10000, 2)
        hs = round(jl / turnover_ratio_data['circulating_cap'][0] / 10000, 4)
        zjb = round(jl / df_reversed['volume'][0], 4)

        #print(f'{s} 竞价额{je} 换手{hs} 今开盘{jkp} {lbs}连板 昨日收盘{df_reversed["close"][0]} 今开盘{ca["current"][0] }')
        if jkp < 0.01 or jkp > 0.06:
            #print(f'{s} 竞价额{je} 换手{hs} 今开盘{jkp} {lbs}连板 昨日收盘{df_reversed["close"][0]} 今开盘{ca["current"][0] }')
            continue

        if zjb < 0.03:
            continue
        #if hs<0.01:
        #    continue

        if je < 1500:
            continue
        #print(s)
        stock_name = security_info.display_name

        if s not in g:
            g[s] = {}
        # 昨竞比*涨幅

        zca = get_call_auction(s, start_date=context.previous_date, end_date=context.previous_date)
        if len(zca) == 0 or zca is None or zca['volume'] is None or zca['volume'][0] is None:
            continue
        jjb = ca['volume'][0] / zca['volume'][0]
        g[s] = jjb
        print(f'{stock_name} 市值{ turnover_ratio_data["market_cap"][0]} 流通股本{turnover_ratio_data["circulating_cap"][0]} 竞昨比{zjb} 今昨竞价比{jjb} 竞价额{je} 换手{hs}  今开盘{jkp} {lbs}连板 昨日收盘{df_reversed["close"][0]} 今开盘{ca["current"][0] } 昨日打开涨停{hfsj}分钟')

        #print(df_reversed)
        lst.append(s)
    st = sorted(g.items(), key=lambda item: item[1], reverse=True)
    for s in st:
        context.sts.append(s[0])
    print(context.sts)
    return context.sts


# 其它函数群

# 筛选出某一日涨停的股票
def get_hl_stock(stock_list, date1, days):
    if not stock_list: return []
    h_s = get_price(stock_list, end_date=date1, frequency='daily', fields=['close', 'high_limit', 'paused'],
                    count=days, panel=False, fill_paused=False, skip_paused=True
                    ).query('close==high_limit and paused==0').groupby('code').size()
    return h_s.index.tolist()


# 过滤函数
def filter_new_stock(initial_list, date, days=50):
    return [stock for stock in initial_list if get_security_info(stock).start_date < date - timedelta(days=days)]


def filter_st_paused_stock(initial_list, date):
    current_data = get_current_data()
    return [stock for stock in initial_list if not (
            current_data[stock].is_st or
            current_data[stock].paused or
            '退' in current_data[stock].name)]


def filter_kcbj_stock(initial_list):
    return [stock for stock in initial_list if stock[0] != '4' and stock[0] != '8' and stock[0] != '3' and stock[:2] != '68']  # and stock[0] != '3'


# ========== 复盘模块-打印 ==========
def print_trade_info(context):
    # 打印当天成交记录
    trades = get_trades()
    for _trade in trades.values():
        print('成交记录：' + str(_trade))
    # 打印账户信息
    for position in list(context.portfolio.positions.values()):
        securities = position.security
        cost = position.avg_cost
        price = position.price
        ret = 100 * (price / cost - 1) if cost != 0 else 0
        value = position.value
        amount = position.total_amount
        print('代码:{}'.format(securities))
        print('成本价:{}'.format(format(cost, '.2f')))
        print('现价:{}'.format(price))
        print('收益率:{}%'.format(format(ret, '.2f')))
        print('持仓(股):{}'.format(amount))
        print('市值:{}'.format(format(value, '.2f')))
        print('———————————————————————————————————')
    print('———————————————————————————————————————分割线————————————————————————————————————————')
