"""P2.71 standalone factor combiner. Fit API accepts train/validation only.

The six fixed transforms contain no learned test statistics. Labels use explicit
market-calendar endpoints, purged BEFORE price lookup at every split boundary.
"""
from __future__ import annotations
import hashlib
import json
import numpy as np
import pandas as pd

FEATURES = ['mf_strength', 'mf_persistence', 'mf_return20', 'price_to_ma20', 'log_amount20', 'log_price']


def features(signals, bars):
    b = bars.sort_values(['stock_code', 'trade_date']).copy()
    if b.duplicated(['stock_code', 'trade_date']).any():
        raise ValueError('行情键重复')
    b['ma20'] = b.groupby('stock_code', sort=False).close.transform(lambda s: s.rolling(20).mean())
    out = signals.merge(b[['stock_code', 'trade_date', 'ma20']], on=['stock_code', 'trade_date'], validate='one_to_one')
    out['mf_strength'] = out.moneyflow_net_z_5d
    out['mf_persistence'] = out.moneyflow_positive_ratio_10d
    out['mf_return20'] = out.ret20
    out['price_to_ma20'] = out.close / out.ma20 - 1
    out['log_amount20'] = np.log(out.amount_ma)
    out['log_price'] = np.log(out.close)
    if not np.isfinite(out[FEATURES]).all().all():
        raise ValueError('特征含非有限数')
    return out.sort_values(['trade_date', 'stock_code'], kind='stable').reset_index(drop=True)


def labels(frame, bars, calendar, start, end, boundary, horizon=10):
    """Only return rows whose full label ends strictly before boundary."""
    dates = pd.DatetimeIndex(calendar)
    if dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ValueError('标签日历非法')
    out = frame[frame.trade_date.between(start, end)].copy()
    positions = dates.get_indexer(out.trade_date)
    if (positions < 0).any():
        raise ValueError('信号不在日历中')
    entry, exit_ = positions + 1, positions + horizon + 1
    valid = exit_ < len(dates)
    out = out.loc[valid].copy()
    out['label_start'] = dates[entry[valid]].to_numpy()
    out['label_end'] = dates[exit_[valid]].to_numpy()
    out = out[out.label_end < pd.Timestamp(boundary)].copy()
    # No crossing-label prices have been accessed above this point.
    prices = bars.set_index(['stock_code', 'trade_date']).open
    if prices.index.has_duplicates:
        raise ValueError('行情键重复')
    def lookup(column):
        keys = pd.MultiIndex.from_arrays([out.stock_code, out[column]])
        return prices.reindex(keys).to_numpy(dtype=float)
    first, last = lookup('label_start'), lookup('label_end')
    good = np.isfinite(first) & np.isfinite(last) & (first > 0) & (last > 0)
    out = out.loc[good].copy()
    out['label'] = last[good] / first[good] - 1
    return out


def fit_model(train, validation, parameters):
    from lightgbm import LGBMRegressor
    if train.empty or validation.empty:
        raise ValueError('训练/验证集为空')
    if not train.label_end.max() < validation.trade_date.min():
        raise ValueError('训练标签跨越验证边界')
    model = LGBMRegressor(**parameters)
    model.fit(train[FEATURES], train.label)
    return model


def score(model, frame):
    out = frame.copy()
    out['score'] = model.predict(out[FEATURES])
    if not np.isfinite(out.score).all():
        raise ValueError('模型分数非法')
    out['cause_width'] = -out.score
    return out.sort_values(['trade_date', 'cause_width', 'stock_code'], kind='stable').reset_index(drop=True)


def gate(candidate, baselines, spec, observations, data_checks=False):
    c = candidate.get('calendar_cagr')
    if c is None or not np.isfinite(c) or not baselines:
        raise ValueError('晋级指标缺失')
    passes = all(c + spec['cagr_tolerance'] >= b['calendar_cagr'] for b in baselines)
    drawdown = all(abs(candidate['max_drawdown']) <= abs(b['max_drawdown']) + spec['max_drawdown_extra'] for b in baselines)
    sample = observations >= spec['min_evaluation_returns']
    return {'cagr_not_lower_than_both': bool(passes), 'drawdown_pass': bool(drawdown),
            'sample_pass': bool(sample), 'data_execution_forward_pass': bool(data_checks),
            'promote': bool(passes and drawdown and sample and data_checks)}


def freeze_digest(model_text, protocol, factor_versions):
    raw = json.dumps({'model': model_text, 'protocol': protocol, 'factors': factor_versions}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()
