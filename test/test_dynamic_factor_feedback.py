import sys
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.dynamic_jq_factor_mining import dynamic_feedback,choose_train_only

def bases():
 return {k:dict(ic_mean=.05,train_total=1.,train_ir=.5,train_drawdown=-.2,train_turnover=.4,
                h1_relative_csi500=.1,h2_relative_csi500=.2) for k in ['roe_only','size_only','original_size_roe']}

def candidate(**overrides):
 r=dict(name='x',expression='rank(roe)+rank(roa)',error='',p_value=.001,ic_mean=.1,
        delta_total_vs_roe=.1,delta_ir_vs_roe=.1,delta_h1_vs_roe=.1,delta_h2_vs_roe=.1,
        train_total=1.1,train_ir=.6,train_turnover=.3)
 return {**r,**overrides}

def test_round_feedback_changes_actual_task():
 b=bases();a=dynamic_feedback(b,[],1);c=dynamic_feedback(b,[candidate()],2)
 assert a!=c and '上一轮候选' in c and '净累计差=' in c
 assert '允许删除market_cap' in a

def test_invalid_candidate_reported_not_silenced():
 p=dynamic_feedback(bases(),[candidate(error='bad min arity')],2)
 assert 'bad min arity' in p

def test_no_positive_increment_keeps_roe():
 name,t=choose_train_only([candidate(delta_total_vs_roe=-.1)])
 assert name is None

def test_half_window_failure_rejects_big_full_gain():
 name,t=choose_train_only([candidate(delta_total_vs_roe=1.,delta_h2_vs_roe=-.01)])
 assert name is None

def test_statistical_failure_rejected():
 name,t=choose_train_only([candidate(p_value=.9)])
 assert name is None

def test_selection_ignores_holdout_payload():
 a=choose_train_only([candidate(holdout_return=-100)])[0]
 b=choose_train_only([candidate(holdout_return=100)])[0]
 assert a==b=='x'

def test_prompt_does_not_serialize_unknown_holdout_payload():
 p=dynamic_feedback(bases(),[candidate(holdout_secret='SECRET_2024_PROFIT')],2)
 assert 'SECRET_2024_PROFIT' not in p

def test_empty_candidates_keeps_baseline():
 assert choose_train_only([])[0] is None

def test_default_entry_uses_dynamic(monkeypatch):
 from scripts import validate_jq_multi_factor as v,dynamic_jq_factor_mining as d
 monkeypatch.setattr(sys,'argv',['test_entry','--rounds','2','--proposals','4'])
 called=[]
 monkeypatch.setattr(d,'main',lambda:called.append(sys.argv.copy()) or 0)
 assert v.main()==0 and called and '--rounds' in called[0]
