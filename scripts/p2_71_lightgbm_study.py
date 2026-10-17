#!/usr/bin/env python3
"""Independent, prespecified LightGBM rank-combiner pilot; never replaces baseline."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from backend.services.factor_dsl import panel
from backend.services.factor_pool import FactorStore
from backend.services.factor_moneyflow import build_signals, equivalent_spec
from backend.services.factor_lightgbm import FEATURES, features, labels, fit_model, score, gate, freeze_digest
from backend.services.factor_replay import ReplayConfig, replay, metrics
from p2_71_factor_audit import digest


def run(output):
    protocol_path = ROOT/'config/p2_71_lightgbm.json'
    protocol = json.loads(protocol_path.read_text())
    output.mkdir(parents=True, exist_ok=True)
    end = protocol['evaluation'][1]
    files = sorted(p for year in range(2024, pd.Timestamp(end).year + 1) for p in
        (ROOT/'assets/data/clean/ohlcv/market=CN').glob(f'exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet'))
    flow_path = ROOT/'assets/data/derived/cn_moneyflow_features.parquet'
    basic_path = ROOT/'assets/data/meta/cn_stock_basic.parquet'
    benchmark_path = ROOT/'assets/data/meta/cn_benchmark_daily.parquet'
    inputs = [*files, flow_path, basic_path, benchmark_path, protocol_path, ROOT/'config/factor_pool_seed.json', ROOT/'config/factor_strategy_families.json']
    inputs_before = {str(p.relative_to(ROOT)): digest(p) for p in inputs}
    bounds = (ds.field('trade_date') >= pd.Timestamp('2024-06-01').to_pydatetime()) & (ds.field('trade_date') <= pd.Timestamp(end).to_pydatetime())
    bars = ds.dataset([str(p) for p in files], format='parquet').to_table(columns=['stock_code','trade_date','open','close','amount'],filter=bounds).to_pandas()
    universe = set(pd.read_parquet(basic_path,columns=['ts_code']).ts_code.astype(str))
    bars = panel(bars[bars.stock_code.isin(universe)])
    flow = ds.dataset(str(flow_path),format='parquet').to_table(columns=['stock_code','trade_date','source','moneyflow_net_z_5d','moneyflow_positive_ratio_10d'],filter=bounds).to_pandas()
    flow = flow[flow.stock_code.isin(universe)]
    store = FactorStore(output/'factors.sqlite3')
    signals = build_signals(bars, flow, store, universe)
    x = features(signals, bars)
    bm = pd.read_parquet(benchmark_path);bm.trade_date = pd.to_datetime(bm.trade_date)
    bm = bm[(bm.ts_code=='000905.SH') & bm.trade_date.between('2024-06-01',end)].set_index('trade_date').close.sort_index()
    calendar = pd.DatetimeIndex(bm.index)
    train = labels(x,bars,calendar,*protocol['train'],boundary=protocol['validation'][0],horizon=protocol['horizon_sessions'])
    validation = labels(x,bars,calendar,*protocol['validation'],boundary=protocol['evaluation'][0],horizon=protocol['horizon_sessions'])
    assert validation.label_end.max() < pd.Timestamp(protocol['evaluation'][0])
    model = fit_model(train,validation,protocol['parameters'])
    model_text = model.booster_.model_to_string()
    (output/'model.txt').write_text(model_text)
    spec = equivalent_spec()
    versions = [{'factor_id':r['factor_id'],'version':r['version'],'sha256':store.get(r['factor_id'],r['version'])['sha256']} for r in spec['factor_refs']]
    execution = spec['execution'];config = ReplayConfig(**{k:execution[k] for k in ['max_positions','max_hold','stop_pct','stop_basis','holding_clock','cost_one_way']})
    def compare(start,end,kind):
        days = calendar[(calendar>=start)&(calendar<=end)]
        baseline_signals = signals[signals.trade_date.between(start,end)]
        selected = x[x.trade_date.between(start,end)]
        predictions = score(model,selected)
        base = replay(baseline_signals,bars[bars.trade_date.isin(days)],bm,config,days)
        enhanced = replay(predictions,bars[bars.trade_date.isin(days)],bm,config,days)
        bmetrics,emetrics = metrics(base['nav']),metrics(enhanced['nav'])
        verdict = gate(emetrics,[bmetrics,bmetrics],protocol['gate'],len(days)-1)
        pd.DataFrame({'rule_causal_nav':base['nav'],'factor_baseline_nav':base['nav'],'lightgbm_nav':enhanced['nav'],'csi500_nav':base['benchmark_nav']}).to_csv(output/f'{kind}_nav.csv')
        predictions[['stock_code','trade_date',*FEATURES,'score','cause_width']].to_csv(output/f'{kind}_predictions.csv',index=False)
        enhanced['ledger'].to_csv(output/f'{kind}_ledger.csv',index=False)
        return {'start':start,'end':end,'returns':len(days)-1,'baseline_rule':bmetrics,'baseline_factor':bmetrics,
                'lightgbm':emetrics,'benchmark':metrics(base['benchmark_nav']),'gate':verdict}
    valid_report = compare(*protocol['validation'],'validation')
    validation_selects = valid_report['gate']['cagr_not_lower_than_both'] and valid_report['gate']['drawdown_pass']
    frozen = {'frozen_at':datetime.now(timezone.utc).isoformat(),'protocol':protocol,'factor_versions':versions,
              'artifact_sha256':freeze_digest(model_text,protocol,versions),'model_sha256':digest(output/'model.txt'),
              'train_rows':len(train),'validation_label_rows':len(validation),
              'train_last_label_end':str(train.label_end.max().date()),'validation_last_label_end':str(validation.label_end.max().date()),
              'validation_selected_model':bool(validation_selects),'selection':'model if validation gate passes else frozen factor baseline',
              'execution':asdict(config),'test_labels_used_for_training':False}
    # The model, protocol and validation decision are persisted BEFORE holdout replay.
    (output/'frozen.json').write_text(json.dumps(frozen,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    test_report = compare(*protocol['evaluation'],'evaluation')
    assert digest(output/'model.txt') == frozen['model_sha256']
    assert freeze_digest(model.booster_.model_to_string(),protocol,versions) == frozen['artifact_sha256']
    test_gate = test_report['gate']
    status = 'RETAIN_BASELINE'
    if validation_selects and test_gate['cagr_not_lower_than_both'] and test_gate['drawdown_pass']:
        status = 'CANDIDATE_OBSERVED_NOT_PROMOTED'
    report = {'status':status,'production_changed':False,'training_completed':True,'validation':valid_report,
              'evaluation':test_report,'frozen_artifact_sha256':frozen['artifact_sha256'],
              'holdout_not_used_for_selection':True,'holdout_kind':protocol['evaluation_kind'],
              'input_hashes':inputs_before,'protocol':protocol,'source_hashes':{p:digest(ROOT/p) for p in ['backend/services/factor_lightgbm.py','scripts/p2_71_lightgbm_study.py']},
              'remaining':['>=252 holdout returns','PIT and publication-time checks','A-share execution/capacity','forward shadow'],
              'actual_choice':'frozen factor baseline retained; LightGBM diagnostic only'}
    if {str(p.relative_to(ROOT)):digest(p) for p in inputs} != inputs_before: raise ValueError('运行期间输入变化')
    (output/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    pd.Series(model.feature_importances_,index=FEATURES,name='split_count').to_csv(output/'feature_importance.csv')
    table=''
    for name,part in [('验证',valid_report),('回顾留出',test_report)]:
        table+=f"| {name} {part['start']}~{part['end']} | {part['baseline_factor']['calendar_cagr']:+.4%} | {part['lightgbm']['calendar_cagr']:+.4%} | {part['baseline_factor']['max_drawdown']:+.4%} | {part['lightgbm']['max_drawdown']:+.4%} |\n"
    (output/'REPORT.md').write_text(f'''# P2.71 独立LightGBM因子组合实验\n\n状态：**{status}**。训练已完成；当前保留等价因子基线，未替换生产、未把失败模型包装成优化策略。\n\n固定6个因子变换，仅在原硬门槛后的候选池内以LightGBM替换排名；仓位/费用/退出均与基线相同。原规则与因子版已有逐笔等价，因此以同公共评测器的两版基线共同验收，而不是把legacy执行差异当模型收益。\n\n| 区间 | 两版基线日历CAGR | LightGBM CAGR | 基线回撤 | LightGBM回撤 |\n|---|---:|---:|---:|---:|\n{table}\n训练样本{len(train)}，验证标签{len(validation)}；按标签结束日purge，训练标签止于{frozen['train_last_label_end']}，验证标签止于{frozen['validation_last_label_end']}。单一预登记100树/7叶/seed71，无网格/测试期调参；验证后先冻结model、因子hash、协议、选择结果，再计算留出表现。留出只作诊断和准入判定，不回流改模型。\n\n验证门槛选择LightGBM：{validation_selects}。留出年化不低于两基线：{test_gate['cagr_not_lower_than_both']}。留出收益观测{test_report['returns']}，252期最低要求通过：{test_gate['sample_pass']}。PIT/真实成交/前向门禁未通过，promote={test_gate['promote']}。\n\n该历史窗口已被研究使用，是回顾式留出而非全新盲测；当前股票池非PIT、历史复权/资金流发布时点/整手涨跌停容量待审计。标签是10交易日开盘收益代理，不等于带止损的逐笔实际收益；最终验收使用统一账本。年化“不下降”是准入门槛，不是训练保证；未通过即保留基线。\n\n[冻结决策](frozen.json) · [完整manifest](manifest.json) · [留出净值](evaluation_nav.csv) · [验证净值](validation_nav.csv) · [特征重要性（split count非因果贡献）](feature_importance.csv)\n''')
    print(f"{status} train_rows={len(train)} validation_cagr={valid_report['lightgbm']['calendar_cagr']:.8f} evaluation_cagr={test_report['lightgbm']['calendar_cagr']:.8f} baseline_evaluation_cagr={test_report['baseline_factor']['calendar_cagr']:.8f} promoted=False")
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=ROOT/'output/research/p2_71/lightgbm_v1');args=parser.parse_args();run(args.output.resolve())
