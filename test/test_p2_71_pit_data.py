"""P2.71 PIT data layer tests (synthetic fixtures, no network)."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.services import pit_data


@pytest.fixture()
def pit(tmp_path, monkeypatch):
    """Build a synthetic PIT root + meta root far from any real cache."""
    root = tmp_path / 'pit'
    meta = tmp_path / 'meta'
    meta.mkdir(parents=True)
    monkeypatch.setattr(pit_data, 'ADJ_SNAPSHOTS', tmp_path / 'no_snapshots')

    pd.DataFrame({
        'ts_code': ['000001.SZ', '600000.SH', '600002.SH'],
        'list_date': ['20100101', '20100101', '20150101'],
    }).to_parquet(meta / 'cn_stock_basic.parquet')

    pd.DataFrame({
        'ts_code': ['000001.SZ', '000001.SZ'],
        'start_date': ['2020-01-01', '2021-03-01'],
        'end_date': ['2020-06-30', None],
        'is_st': [True, False],
        'is_delist_risk': [True, False],
    }).to_parquet(meta / 'cn_stock_namechange.parquet')

    # 600001.SH: official delist date 2020-12-31 but last bar 2020-12-28
    # 600002.SH: only in delisted_daily -> last bar fallback
    pd.DataFrame({
        'ts_code': ['600001.SH'] * 2 + ['600002.SH'],
        'trade_date': ['2020-12-28', '2020-12-25', '2022-03-05'],
        'adj_factor': [2.0, 2.0, 1.5],
    }).to_parquet(meta / 'cn_delisted_daily.parquet')

    bulk = root / 'stock_basic_delisted'
    bulk.mkdir(parents=True)
    pd.DataFrame({
        'ts_code': ['T600000.SH', '600001.SH'],
        'delist_date': ['20210630', '20201231'],
    }).to_parquet(bulk / 'bulk.parquet')

    susp = root / 'suspend_d' / 'year=2020'
    susp.mkdir(parents=True)
    pd.DataFrame({
        'ts_code': ['000001.SZ', '000001.SZ'],
        'trade_date': ['20200203', '20200204'],
        'suspend_type': ['S', 'R'],
    }).to_parquet(susp / 'bulk.parquet')

    idx = root / 'index_weight' / 'index_code=000905.SH'
    idx.mkdir(parents=True)
    pd.DataFrame({
        'index_code': ['000905.SH'] * 3,
        'con_code': ['000001.SZ', '000001.SZ', '600000.SH'],
        'trade_date': ['20200131', '20200229', '20200229'],
        'weight': [1.0, 0.9, 0.2],
    }).to_parquet(idx / 'bulk.parquet')

    for code, rows in {
        '000001.SZ': [
            ('20200102', 10.00, 10.50, 9.50, 10.20),
            ('20200103', 10.20, 11.22, 10.10, 11.22),
        ],
        '600000.SH': [('20200102', 8.00, 8.10, 7.90, 8.05)],
    }.items():
        code_dir = root / 'code_data' / f'code={code}'
        code_dir.mkdir(parents=True)
        frame = pd.DataFrame(rows, columns=['trade_date', 'open', 'high', 'low', 'close'])
        frame['ts_code'] = code
        frame['pre_close'] = [9.90, 10.20][: len(frame)]
        frame['pct_chg'] = 0.0
        frame[['ts_code', 'trade_date', 'open', 'high', 'low', 'close',
               'pre_close', 'pct_chg']].to_parquet(code_dir / 'daily_raw.parquet')
        # 000001.SZ hits the 10% cap on 20200103 (up_limit == close)
        up = [11.00, 11.22][: len(frame)]
        down = [9.00, 9.18][: len(frame)]
        pd.DataFrame({
            'ts_code': code, 'trade_date': frame['trade_date'],
            'up_limit': up, 'down_limit': down,
        }).to_parquet(code_dir / 'stk_limit.parquet')
        if code == '000001.SZ':
            pd.DataFrame({
                'ts_code': code, 'trade_date': frame['trade_date'],
                'adj_factor': [2.0, 2.0],
            }).to_parquet(code_dir / 'adj_factor.parquet')
    return {'root': root, 'meta': meta}


def test_normalize_code():
    assert pit_data.normalize_code('T600018.SH') == '600018.SH'
    assert pit_data.normalize_code('000001_SZ') == '000001.SZ'
    assert pit_data.normalize_code('600000.SH') == '600000.SH'


def test_delist_dates_precedence(pit):
    out = pit_data.delist_dates(pit['root'], pit['meta']).set_index('stock_code')
    assert out.loc['600000.SH', 'delist_date'] == pd.Timestamp('2021-06-30')
    assert out.loc['600000.SH', 'source'] == 'stock_basic_D'
    assert out.loc['600001.SH', 'delist_date'] == pd.Timestamp('2020-12-31')
    assert out.loc['600002.SH', 'delist_date'] == pd.Timestamp('2022-03-05')
    assert out.loc['600002.SH', 'source'] == 'delisted_last_bar'


def test_st_panel_intervals(pit):
    keys = pd.DataFrame({
        'stock_code': ['000001.SZ'] * 5,
        'trade_date': pd.to_datetime(['2019-12-31', '2020-03-15', '2020-06-30',
                                      '2020-07-01', '2021-06-01']),
    })
    out = pit_data.st_panel(keys, pit['meta'])
    assert list(out['is_st']) == [False, True, True, False, False]
    assert list(out['is_delist_risk']) == [False, True, True, False, False]


def test_index_panel_forward_fill(pit):
    keys = pd.DataFrame({
        'stock_code': ['000001.SZ', '600000.SH', '000001.SZ', '600000.SH'],
        'trade_date': pd.to_datetime(['2020-02-15', '2020-02-15', '2020-03-01',
                                      '2020-01-15']),
    })
    out = pit_data.index_panel(keys, '000905.SH', pit['root'])
    assert list(out['in_index']) == [True, False, True, False]


def test_limit_panel_flags(pit):
    keys = pd.DataFrame({
        'stock_code': ['000001.SZ', '000001.SZ', '600000.SH', '600002.SH'],
        'trade_date': pd.to_datetime(['2020-01-02', '2020-01-03', '2020-01-02',
                                      '2020-01-02']),
    })
    out = pit_data.limit_panel(keys, pit['root']).set_index(['stock_code', 'trade_date'])
    assert out.loc[('000001.SZ', pd.Timestamp('2020-01-03')), 'close_at_up'] == True  # noqa: E712
    assert out.loc[('000001.SZ', pd.Timestamp('2020-01-02')), 'close_at_up'] == False  # noqa: E712
    assert out.loc[('600000.SH', pd.Timestamp('2020-01-02')), 'close_at_up'] == False  # noqa: E712
    assert pd.isna(out.loc[('600002.SH', pd.Timestamp('2020-01-02')), 'close_at_up'])


def test_adj_factors_sources(pit):
    out = pit_data.adj_factors(['600002.SH'], pit['root'], pit['meta'])
    assert len(out) == 1
    assert out.iloc[0]['adj_factor'] == 1.5
    assert out.iloc[0]['source'] == 'delisted_daily'


def test_annotate_bars(pit):
    bars = pd.DataFrame({
        'stock_code': ['000001.SZ', '000001.SZ', '600000.SH', '600002.SH'],
        'trade_date': pd.to_datetime(['2020-01-03', '2019-12-31', '2021-07-01',
                                      '2020-02-03']),
        'close': [11.22, 9.0, 8.0, 5.0],
    })
    out = pit_data.annotate_bars(bars, index_code='000905.SH', root=pit['root'],
                                 meta_root=pit['meta'])
    by = out.set_index(['stock_code', 'trade_date'])
    assert bool(by.loc[('000001.SZ', pd.Timestamp('2020-01-03')), 'close_at_up']) is True
    # first index snapshot is 2020-01-31, so 2020-01-03 is not yet a member
    assert bool(by.loc[('000001.SZ', pd.Timestamp('2020-01-03')), 'in_index']) is False
    assert bool(by.loc[('000001.SZ', pd.Timestamp('2019-12-31')), 'is_listed']) is True
    assert bool(by.loc[('600000.SH', pd.Timestamp('2021-07-01')), 'is_listed']) is False
    assert bool(by.loc[('000001.SZ', pd.Timestamp('2020-01-03')), 'suspended_official']) is False
    assert bool(by.loc[('600002.SH', pd.Timestamp('2020-02-03')), 'is_st']) is False
    for column in ('raw_close', 'up_limit', 'down_limit', 'is_st', 'is_listed',
                   'suspended_official', 'in_index'):
        assert column in out.columns


def test_suspension_panel(pit):
    keys = pd.DataFrame({
        'stock_code': ['000001.SZ', '000001.SZ', '000001.SZ'],
        'trade_date': pd.to_datetime(['2020-02-03', '2020-02-04', '2020-02-05']),
    })
    out = pit_data.suspension_panel(keys, pit['root'])
    # 2020-02-03 has an S record; 02-04 is an R (resumption) record
    assert list(out['suspended_official']) == [True, False, False]


def test_qfq_vintage_report(pit):
    bars = pd.DataFrame({
        'stock_code': ['000001.SZ'] * 2,
        'trade_date': pd.to_datetime(['2020-01-02', '2020-01-03']),
        # raw 10.20/factor 2 -> base 4.00; raw 11.22/factor 2 -> base 4.00
        'close': [5.10, 5.61],
    })
    out = pit_data.qfq_vintage_report(bars, ['000001.SZ'], pit['root'])
    assert len(out) == 1
    assert out.iloc[0]['base_mean'] == pytest.approx(4.0)
    assert out.iloc[0]['base_std'] == pytest.approx(0.0, abs=1e-9)


def test_coverage_report(pit):
    (pit['root'] / 'coverage.json').write_text(
        '{"units": {"daily_raw/000001.SZ": {"rows": 2},'
        ' "stk_limit/000001.SZ": {"rows": 2}}, "failures": {}}')
    report = pit_data.coverage_report(pit['root'], pit['meta'])
    assert report['datasets']['daily_raw']['rows'] == 2
    assert report['datasets']['stk_limit']['units'] == 1
    assert report['code_data_files']['daily_raw'] == 2
    assert report['delist_codes'] == 3


def test_select_panel_stock_filter(tmp_path, monkeypatch):
    """Index series in the clean layer must never enter the liquid panel."""
    from backend.services.market_contract import normalize_panel
    from scripts import p2_71_alphaqcm_pilot as pilot

    dates = pd.bdate_range('2020-01-01', periods=30)
    rows = []
    for code, amount in (('000001.SH', 900.0), ('600000.SH', 100.0)):
        for day in dates:
            rows.append({'stock_code': code, 'trade_date': day, 'open': 1.0,
                         'high': 1.1, 'low': 0.9, 'close': 1.0, 'volume': 1.0,
                         'amount': amount})
    bars = normalize_panel(pd.DataFrame(rows))
    fake = pd.DataFrame({'stock_code': ['600000.SH'],
                         'list_date': [pd.Timestamp('2000-01-01')]})
    monkeypatch.setattr(pilot, 'listing_dates', lambda *a, **k: fake)

    panel = pilot.select_panel(1, sel_start='2020-01-01', sel_end='2020-02-28',
                               bars=bars, with_fundamentals=False)
    # 000001.SH has the larger amount but is an index, not a stock
    assert list(panel.stock_code.unique()) == ['600000.SH']
    both = pilot.select_panel(2, sel_start='2020-01-01', sel_end='2020-02-28',
                              bars=bars, stock_only=False, with_fundamentals=False)
    assert set(both.stock_code.unique()) == {'000001.SH', '600000.SH'}
