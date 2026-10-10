#!/usr/bin/env python
"""Baseline-aware LLM factor audit. Prompts/selection use 2015-2020 only."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.validate_jq_multi_factor import FIELDS,load_inputs,ic_stats,replay,performance,dump
from scripts.llm_factor_mining import bh_fdr,call_llm,safe_eval
OUT=ROOT/'output/research/jq_multi_factor_dynamic'
BASES={'roe_only':'rank(roe)','size_only':'-rank(market_cap)',
       'original_size_roe':'rank(roe)-rank(market_cap)'}

def fmt(x):
    return 'NaN' if x is None or not np.isfinite(x) else f'{x:+.4f}'

def train_metrics(frame,score,mats,bench,cols,roe=None):
    ic=ic_stats(frame,score)[0]
    curve,full,_=replay(frame,score,mats,bench,cols)
    halves={}
    for name,a,b in [('h1',None,'2017-12-31'),('h2','2018-01-01','2020-12-31')]:
        x=curve.loc[a:b];halves[name]=performance(x.nav,x.csi500)['relative_terminal']
    x={**ic,'train_total':full['total_return'],'train_ir':full['information_ratio'],
       'train_drawdown':full['max_drawdown'],'train_turnover':full['avg_two_way_turnover'],
       'h1_relative_csi500':halves['h1'],'h2_relative_csi500':halves['h2']}
    if roe is not None:
        x.update(delta_total_vs_roe=x['train_total']-roe['train_total'],
                 delta_ir_vs_roe=x['train_ir']-roe['train_ir'],
                 delta_h1_vs_roe=x['h1_relative_csi500']-roe['h1_relative_csi500'],
                 delta_h2_vs_roe=x['h2_relative_csi500']-roe['h2_relative_csi500'])
    return x

def dynamic_feedback(bases,history,round_no):
    rows=[
      '这是一个诊断性因子挖掘，不得强制多因子。目标是比较相对ROE-only的扣费增益，而非只最大化IC。',
      '历史沪深300、20股等权、15交易日调仓；t收盘形成信号，t+1开盘成交；佣金3bp、卖税历史10/5bp、单边滑点10bp。',
      '所有反馈仅来自2015-2020训练期。2021年以后任何结果都没有给你；已知2024+曾被人看过，不能视为真正新留出。',
      '字段是原始值，rank(x)为当日横截面百分位；ROE是累计报告口径。只用列出的字段及rank/zscore/四则运算。',
      '允许删除market_cap或其他拖累字段；至少一个候选不能含market_cap，至少一个只在ROE上增加一个字段。',
      '最多4个原始字段，优先单一变化。不要shift、rolling、min/max、复杂嵌套、参数网格或未来信息。',
      '训练基线（净收益、IC、换手、两半窗均从同一数据和成交模型实测）：']
    for name,m in bases.items():
        rows.append(f'{name}={BASES[name]} | 净累计={fmt(m["train_total"])} IC={fmt(m["ic_mean"])} '
          f'IR={fmt(m["train_ir"])} 回撤={fmt(m["train_drawdown"])} 换手={fmt(m["train_turnover"])} '
          f'前半相对500={fmt(m["h1_relative_csi500"])} 后半相对500={fmt(m["h2_relative_csi500"])}')
    roe=bases['roe_only'];ori=bases['original_size_roe'];size=bases['size_only']
    rows.append(f'消融诊断：原策略净累计相对ROE差={fmt(ori["train_total"]-roe["train_total"])}，'
                f'仅市值相对ROE差={fmt(size["train_total"]-roe["train_total"])}。')
    if round_no>1:
        rows.append('上一轮候选的训练期反馈；请对失败方案减法修正，不要盲目叠加字段：')
        valid=sorted([x for x in history if not x.get('error')],key=lambda x:x['delta_total_vs_roe'],reverse=True)
        for x in valid[:6]:
            rows.append(f'{x["name"]}: {x["expression"]}; 净累计差={fmt(x["delta_total_vs_roe"])} '
              f'IR差={fmt(x["delta_ir_vs_roe"])} 前半差={fmt(x["delta_h1_vs_roe"])} '
              f'后半差={fmt(x["delta_h2_vs_roe"])} IC={fmt(x["ic_mean"])} 换手={fmt(x["train_turnover"])}')
        for x in history:
            if x.get('error'):rows.append(f'失效：{x["expression"]} -> {x["error"]}')
        rows.append('若没有候选稳定胜过ROE，给出简洁ROE变体或明确保持ROE，不要为了新颖性加规模。')
    else:
        rows.append('第一轮提出消融优先的4个简洁候选，包含ROE+仅一个字段的方案。')
    rows.append('仅输出4个JSON factors，字段name/expression/reason。')
    return '\n'.join(rows)

def choose_train_only(rows):
    t=pd.DataFrame([r for r in rows if not r.get('error')]).copy()
    if t.empty:return None,t
    t['train_fdr_pass']=bh_fdr(t.p_value).to_numpy()
    eligible=t[t.train_fdr_pass&(t.ic_mean>0)&(t.delta_total_vs_roe>0)&
      (t.delta_ir_vs_roe>0)&(t.delta_h1_vs_roe>0)&(t.delta_h2_vs_roe>0)]
    eligible=eligible.sort_values(['delta_total_vs_roe','name'],ascending=[False,True])
    return (eligible.iloc[0]['name'] if len(eligible) else None),t

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--reuse-proposals',action='store_true')
    ap.add_argument('--rounds',type=int,default=2);ap.add_argument('--proposals',type=int,default=4)
    args=ap.parse_args();OUT.mkdir(parents=True,exist_ok=True)
    frame,mats,bench,cols,_=load_inputs('2015-01-05','2026-10-09')
    n=len(bench.loc[:'2020-12-31']);tb=bench.iloc[:n];tm={k:v[:n] for k,v in mats.items()}
    train=frame[frame.trade_date<'2020-12-01'].copy()
    bases={}
    for name,expr in BASES.items():
        score,err=safe_eval(expr,train,pd.Series(FIELDS));assert not err
        bases[name]=train_metrics(train,score,tm,tb,cols)
    dump(OUT/'baselines_train.json',bases)
    print('[baselines]',json.dumps(bases,ensure_ascii=False),flush=True)
    hist=[];seen=set(BASES.values())
    for rnd in range(1,args.rounds+1):
        direction=dynamic_feedback(bases,hist,rnd)
        dump(OUT/f'round_{rnd}_prompt.json',{'direction':direction,'fields':FIELDS,
          'model':'deepseek-flash','temperature':.35,'feedback_period':'train_2015_2020_only'})
        path=OUT/f'round_{rnd}_proposals.json'
        if args.reuse_proposals and path.exists():items=json.loads(path.read_text())
        else:
            items=call_llm(direction,FIELDS,args.proposals,[],'deepseek-flash',temperature=.35,
              note='字段是原始值，通用模板中的“已预处理字段”不适用于本任务；只用rank/zscore和四则运算。')
            dump(path,items)
        for j,item in enumerate(items[:args.proposals],1):
            expr=str(item.get('expression','')).strip()
            row={'name':f'dynamic_r{rnd}_{j}_{item.get("name","factor")}',
                 'expression':expr,'reason':item.get('reason'),'round':rnd}
            if expr in seen:row['error']='duplicate expression'
            else:
                seen.add(expr);score,err=safe_eval(expr,train,pd.Series(FIELDS));row['error']=err
                if score is not None:row.update(train_metrics(train,score,tm,tb,cols,roe=bases['roe_only']))
            hist.append(row);print('[train]',json.dumps(row,ensure_ascii=False,default=str),flush=True)
        pd.DataFrame(hist).to_csv(OUT/'train_candidates.csv',index=False)
    choice,table=choose_train_only(hist);table.to_csv(OUT/'train_candidates_with_fdr.csv',index=False)
    selected=next((r for r in hist if r['name']==choice),None)
    frozen={'selected_name':choice or 'roe_only','selected_expression':selected['expression'] if selected else BASES['roe_only'],
      'candidate_improves_roe_under_train_gates':bool(choice),'selection_uses_only':'2015-2020 train',
      'validation':'2021-2023','retrospective_confirmation':'2024-2026; previously disclosed'}
    dump(OUT/'frozen_choice.json',frozen)
    final={'roe_only':BASES['roe_only'],'original_size_roe':BASES['original_size_roe']}
    if selected:final['dynamic_selected']=selected['expression']
    elif len(table):
        top=table.sort_values('delta_total_vs_roe',ascending=False).iloc[0]
        final['dynamic_diagnostic']=top['expression']
    metrics={};curves={}
    for name,expr in final.items():
        score,err=safe_eval(expr,frame,pd.Series(FIELDS));assert not err,(name,err)
        curve,_,_=replay(frame,score,mats,bench,cols);curves[name]=curve
        curve.to_csv(OUT/f'nav_{name}.csv');metrics[name]={}
        for split,a,b in [('train',None,'2020-12-31'),('validation','2021-01-01','2023-12-31'),
                          ('retrospective_2024_plus','2024-01-01',None),('full',None,None)]:
            z=curve.loc[a:b];metrics[name][split]=performance(z.nav,z.csi500)
    reference=next(iter(curves.values()))
    for split,a,b in [('train',None,'2020-12-31'),('validation','2021-01-01','2023-12-31'),
                      ('retrospective_2024_plus','2024-01-01',None),('full',None,None)]:
        z=reference.loc[a:b];metrics.setdefault('csi500',{})[split]=performance(z.csi500,z.csi500)
    dump(OUT/'metrics.json',metrics)
    print('[frozen]',json.dumps(frozen,ensure_ascii=False),flush=True)
    print('[results]',json.dumps(metrics,ensure_ascii=False),flush=True)
    return 0
if __name__=='__main__':raise SystemExit(main())
