"""Versioned, causal trading-crowding proxies, not investor holdings estimates."""
from __future__ import annotations
import numpy as np
import pandas as pd

VERSION = 'crowding.v1'

def historical_percentile(s: pd.Series, window=252):
    """Mid-rank vs previous `window` market sessions, excluding current observation.

    A full, contiguous window is required. Equal prior values count half; all
    ties map to 0.5 instead of falsely flagging a constant series as extreme.
    """
    return (s.rolling(window+1, min_periods=window+1).rank(method='average')-1)/window

def features(bars, calendar, validated_sources, *, rank_window=252):
    if bars.empty or bars.stock_code.nunique()!=1:
        raise ValueError('one nonempty stock required')
    f=bars.copy();f['trade_date']=pd.to_datetime(f.trade_date)
    if f.trade_date.isna().any() or f.trade_date.duplicated().any():raise ValueError('unique valid dates required')
    cal=pd.DatetimeIndex(calendar)
    if not cal.is_unique or not cal.is_monotonic_increasing:raise ValueError('sorted unique calendar required')
    if not f.trade_date.isin(cal).all():raise ValueError('bar outside market calendar')
    code=f.stock_code.iloc[0]
    cal=cal[(cal>=f.trade_date.min())&(cal<=f.trade_date.max())]
    f=f.set_index('trade_date').reindex(cal);f.index.name='trade_date';f['stock_code']=code
    cols=['open','high','low','close','volume','amount','turnover']
    for c in cols:f[c]=pd.to_numeric(f[c],errors='coerce')
    valid=np.isfinite(f[cols[:-1]]).all(axis=1)&(f[cols[:-1]]>0).all(axis=1)
    valid&=(f.high>=f[['open','close','low']].max(axis=1))&(f.low<=f[['open','close']].min(axis=1))
    f['valid_bar']=valid
    c=f.close.where(valid)
    volume_ok=valid & f.source.isin(validated_sources)
    v,a=(f[k].where(volume_ok) for k in ['volume','amount'])
    tr=f.turnover.where(valid & f.source.isin(validated_sources) & (f.turnover>0))
    f['turnover_float']=tr
    f['ret1']=c.pct_change(fill_method=None)
    f['momentum20']=c/c.shift(20)-1
    f['vol20']=f.ret1.rolling(20).std(ddof=1)*np.sqrt(252)
    f['amount20']=a.rolling(20).mean()
    f['turnover_mean5']=tr.rolling(5).mean()
    f['turnover_pct252']=historical_percentile(f.turnover_mean5,rank_window)
    for name,s in [('price',c),('volume',v),('amount',a)]:
        mean=s.rolling(20).mean();f[name+'_bias20']=(s/mean-1).where(mean>0)
    for name,s in [('volume',v),('amount',a),('turnover',tr)]:
        corr=c.rolling(40).corr(s).clip(-1,1)
        f['price_'+name+'_corr40']=corr.where((c.rolling(40).std()>1e-12)&(s.rolling(40).std()>1e-12))
    da=np.log(a).diff()
    f['ret_dlogamount_corr40']=f.ret1.rolling(40).corr(da).clip(-1,1).where(
        (f.ret1.rolling(40).std()>1e-12)&(da.rolling(40).std()>1e-12))
    for name,s in [('return',f.ret1),('price',c)]:
        n=60
        sd=s.rolling(n).std(ddof=0)
        f[name+'_skew60']=(s.rolling(n).skew()*(n-2)/np.sqrt(n*(n-1))).where(sd>1e-12)
        if name=='return':
            f['return_kurt60']=((s.rolling(n).kurt()*(n-2)*(n-3)/(n-1)-6)/(n+1)+3).where(sd>1e-12)
    ph=c.shift().rolling(252).max();pl=c.shift().rolling(252).min()
    eligible=valid & ph.notna() & pl.notna()
    f['breadth_eligible']=eligible
    f['new_high252']=(c>=ph).where(eligible).astype(float)
    f['new_low252']=(c<=pl).where(eligible).astype(float)
    f['high_crowding']=(f.turnover_pct252>=.95).astype('boolean').where(f.turnover_pct252.notna())
    f['data_status']=np.select([~valid,tr.isna(),f.turnover_pct252.isna()],['invalid_or_missing_bar','turnover_unvalidated','warming_up'],default='ready')
    f['factor_version']=VERSION
    return f.reset_index()

def breadth(factors):
    f=factors[factors.breadth_eligible]
    g=f.groupby('trade_date').agg(eligible=('stock_code','size'),high=('new_high252','sum'),low=('new_low252','sum')).reset_index()
    g['hindenburg_high']=g.high/g.eligible;g['low_fraction']=g.low/g.eligible
    g['hindenburg_delta']=g.hindenburg_high-g.low_fraction
    g['hindenburg_sqrt']=np.sqrt(g.hindenburg_high*g.low_fraction)
    return g

def soft_weights(weights, high):
    """Unknown does not silently remove a name; caller logs unknown exposure."""
    if weights.index.has_duplicates or (weights<0).any() or not np.isfinite(weights).all() or weights.sum()>1.000001:
        raise ValueError('invalid weights')
    return weights*pd.Series([.5 if high.get(c) is True or isinstance(high.get(c),np.bool_) and high.get(c) else 1. for c in weights.index],index=weights.index)
