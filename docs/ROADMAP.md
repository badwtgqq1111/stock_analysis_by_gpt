# Quant 生态全景 & 路线图

## 已有项目地图

| 项目 | 定位 | 与 stock_analysis_by_gpt 的关联 |
|------|------|------|
| `akshare` | A股/HK 数据源 | 已是核心依赖，数据层可深挖 |
| `qlib` | 微软量化平台 | **模型库、自动化调参、RL 组合管理** |
| `akquant` | Rust TA-Lib + 期货 | **因子计算加速 10-100x** |
| `FinGPT` | LLM 金融情感 | **比 DistilBERT 更强的 alt_data 信号** |
| `RD-Agent` | LLM 自动因子挖掘 | **替代手工设计因子** |
| `Kronos` | 时序大模型 | **可能替代 LightGBM 做预测** |
| `TradeTrap` | Agent 交易平台 | 监控面板、插件架构 |
| `vnpy` | 生产级事件驱动 | 成熟但太重，范式不同 |
| `AI-Trader` | 多 Agent 博弈 | 思路参考 |
| `quantcoder` | 论文→代码 | 已废弃，不维护 |
| `a-share-heatmap` | A股热力图 | Next.js 可视化，可参考前端的 sector heatmap |
| `qstock` | 轻量量化库 | RPS、MM 趋势、资金流向等选股模块可参考 |

---

## 当前状态

### stock_analysis_by_gpt 数据流

```
akshare (数据获取)
  → sync_hk_market.py (日线 + 分钟线同步)
  → generate_factors (193 Alpha158 特征, ClickHouse/Parquet)
  → select_stocks (LightGBM Ranker + 排名公式 + 回测)
```

### 排名公式演进 (V3 → V8)

| Ver | Rank~Ret 相关性 | 关键改进 |
|------|----------------|----------|
| V3 | -0.035 | 起点（偏向 startup） |
| V4 | -0.016 | win_rate gate >=30% |
| V5 | +0.027 | win_rate 权重 0.55 |
| V6 | +0.111 | 加入 PB 成长溢价 |
| V7 | +0.111 | win_rate gate 收紧到 35% |
| V8 | **+0.187** | 纯数据驱动，去掉启发式 bonus |

V8 最终公式:

```
ranking = wr*0.65 + overheat*0.18 + pb_bonus + model*0.10 + risk_adj*0.05
         + freshness*0.02 - hsv*0.05 - dd*0.03 - downtrend*w - tc*0.02
```

### 核心瓶颈

**LightGBM 的 `expected_3m_score` 与真实收益相关性只有 +0.064**。
排名公式优化已接近天花板 —— 信号源本身才是瓶颈。
模型 Rank IC 仅 0.14，预测能力是最大短板。

---

## 四个方向

### 1. qlib 模型库 — 换更好的预测模型 (优先级: 高, 本周)

LightGBM Ranker IC 只有 0.14。qlib 自带几十个模型:
- XGBoost, CatBoost, LightGBM (GBM 系)
- TabNet, MLP, LSTM, GRU, Transformer (DL 系)
- HIST (图神经网络), TRA (Transformer Attention)
- 自动化超参搜索 `rl_tuner`

**做法**: 用 qlib 的 model zoo 在相同数据上跑 benchmark，直接换到 IC 最高的模型。

**改动范围**: `core/lightgbm_analysis.py` 的训练部分改为调用 qlib 模型接口。

### 2. akquant Rust 加速 — 因子计算快 10-100x (优先级: 高, 本周)

193 个特征、695 只股票，当前一轮 ~15 分钟。akquant 用 Rust 重写了 TA-Lib 和滚动统计，通过 maturin 暴露 Python 绑定。

**做法**: 把 `factor_engine/expressions/ta_operators.py` 和滚动统计的热路径切到 akquant 的 Rust 实现。

**改动范围**: `factor_engine/expressions/`, `factor_engine/registry.py`。

注意: 需要评估 akquant 的算子覆盖度，不够的部分补充 Rust 实现。

### 3. RD-Agent — 自动挖掘高 IC 因子 (优先级: 中, 下周)

当前 193 个特征中真正有用的可能就十几个（top features 总是 `ipo_trading_days`、`TA_ATR` 等）。RD-Agent 用 LLM 自动生成和验证新因子。

**做法**: 对 HK 股票池跑 RD-Agent 的 factor loop，筛选 IC>0.2 的新因子加入特征集。

**改动范围**: 新增 `experiments/rd_agent_factors/` 目录。

### 4. FinGPT 情感信号 — 替换规则引擎 (优先级: 中, 长线)

当前 `alt_data/sentiment.py` 的 fallback 是规则引擎（关键词字典），full model 是 DistilBERT。FinGPT 针对中文金融语料做过 LoRA 微调（ChatGLM、Meta-Llama-3 版本）。

**做法**: 用 FinGPT 的 `sentiment-analysis-v3` 替换当前 sentiment pipeline，产出更可靠的 `alt_sentiment_*` 特征。

**改动范围**: `alt_data/sentiment.py`。

---

## 时间线

| 周次 | 任务 | 预期效果 |
|------|------|----------|
| 本周 | (1) qlib 模型 benchmark + (2) akquant 加速探索 | 找到 IC>0.20 的模型, 因子计算 < 2min |
| 下周 | (3) RD-Agent 因子挖掘 | 增加 3-5 个 IC>0.2 的新因子 |
| 长线 | (4) FinGPT 情感替换 | 情感特征 IC 从 0 提升到 >0.05 |

---

## 项目笔记

- `akshare/`: 修改上游代码时注意 `pyproject.toml` 中的 `[tool.uv.sources]` 指向同级目录
- `qlib/`: 需要先验证 qlib 的数据格式是否兼容现有的 ClickHouse/Parquet 后端
- `akquant/`: 需要 `maturin` 编译，macOS 注意 `libomp` 依赖
- macOS 运行 LightGBM 需要 `brew install libomp`
