import sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from factor_engine.signals import BottomMomentumRecipe, create_signal_recipe

def test_bottom_momentum_triggers_after_six_month_low():
    dates=pd.date_range('2024-01-01', periods=150, freq='B')
    close=np.r_[np.linspace(10,5,100), np.linspace(5.1,7,50)]
    frame=pd.DataFrame({'Close':close,'High':close*1.01,'Low':close*.99,'Volume':np.r_[np.full(130,100.),np.full(20,180.)]}, index=dates)
    out=BottomMomentumRecipe(min_score=40).evaluate(frame).to_dict()
    assert out['setup_type']=='bottom_momentum'; assert out['bars_since_low']>=3

def test_registered():
    assert isinstance(create_signal_recipe('bottom_momentum'), BottomMomentumRecipe)
