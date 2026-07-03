# RD-Agent 因子挖掘搭建指南

## 概述

RD-Agent 是微软开源的 LLM 驱动因子挖掘框架，论文已中 NeurIPS 2025。核心流程：

```
LLM 提出因子想法 → 生成 Python 代码 → Qlib 回测评估 → IC/收益反馈 → LLM 迭代改进
```

## 现状

| 组件 | 状态 | 说明 |
|------|------|------|
| Docker | 已安装 | v29.4.3 |
| RD-Agent 包 | 未安装 | `pip install rdagent` |
| Qlib 数据 | 未转换 | 需要把 Parquet → Qlib HDF5 |
| LLM API | 未配置 | 可用 DeepSeek（已有 Anthropic/DeepSeek 代理） |
| .env 配置 | 不存在 | 需要创建 |

## 搭建步骤

### 第一步：安装 RD-Agent

```bash
cd /Users/ccs/code/quant/stock_analysis_by_gpt
uv pip install rdagent
```

依赖链：`rdagent` → `qlib` + `litellm` + `docker` + `pydantic-settings` + `openai`

### 第二步：配置 LLM API

在项目根目录创建 `.env`：

```bash
# LLM 配置（用 DeepSeek，国内可访问）
CHAT_MODEL=deepseek/deepseek-chat
OPENAI_API_KEY=你的DeepSeek_API_Key
OPENAI_BASE_URL=https://api.deepseek.com/v1
EMBEDDING_MODEL=openai/text-embedding-3-small
```

> RD-Agent 通过 LiteLLM 支持所有兼容 OpenAI 的接口。DeepSeek、通义千问、GLM 都可以。

### 第三步：转换 HK 股票数据到 Qlib 格式

这是**工作量最大的一步**。Qlib 要求数据存储为 HDF5 格式，目录结构如下：

```
~/.qlib/qlib_data/hk_data/
├── calendars/
│   └── day.txt          # 交易日历
├── instruments/
│   └── all.txt          # 股票列表，每行: 代码 上市日期 退市日期
└── features/
    └── <stock_code>/
        ├── open.day.bin
        ├── close.day.bin
        ├── high.day.bin
        ├── low.day.bin
        ├── volume.day.bin
        ├── adjfactor.day.bin    # 复权因子
        └── ...
```

需要做：

```bash
# 1. 导出当前 Parquet 日线数据为 CSV
uv run python scripts/export_ohlcv_for_qlib.py --output /tmp/qlib_export/

# 2. 用 qlib 的 dump_bin 工具转换
uv run python -c "
from qlib.data import D
from qlib.data.storage import file_storage
# 把 CSV → Qlib 二进制格式
D.dump_bin(data, '/tmp/qlib_export/', '~/.qlib/qlib_data/hk_data/')
"

# 3. 注册港股市场配置
# 编辑 ~/.qlib/qlib_data/hk_data/ 下的配置文件
```

**预估耗时**：2-3 小时（主要是调试数据格式兼容性）

### 第四步：配置 RD-Agent 港股场景

编辑 `rdagent/scenarios/qlib/experiment/factor_template/conf_baseline.yaml`：

```yaml
qlib_init:
    provider_uri: "~/.qlib/qlib_data/hk_data"   # 改：指向港股数据
    region: hk                                    # 改：港股市场

market: &market hsi                               # 改：恒生指数成分股
benchmark: &benchmark HSI                         # 改：恒生指数基准

data_handler_config: &data_handler_config
    start_time: "2014-01-01"                      # 改：港股数据起始
    end_time: "2026-05-28"
    instruments: *market
    # ... Alpha158DL 自动从 Qlib 数据生成特征
```

还需要创建自定义 `DataLoader` 类来适配港股（代码量约 50-100 行 Python）。

**预估耗时**：1-2 小时

### 第五步：运行因子挖掘

```bash
# 健康检查
rdagent health_check

# 只跑因子挖掘（不跑模型优化）
rdagent fin_factor \
  --train_start 2014-01-01 \
  --train_end 2021-12-31 \
  --valid_start 2022-01-01 \
  --valid_end 2024-12-31 \
  --test_start 2025-01-01 \
  --test_end 2026-05-28 \
  --max_factors 20 \
  --iterations 10
```

每次迭代约 5-15 分钟（取决于 LLM 响应速度），10 轮迭代约 1-2 小时。

### 第六步：提取发现的因子

RD-Agent 产出的因子代码存放在 `FactorFBWorkspace/` 下。评估指标在回测日志中：

- 新因子 IC / Rank IC
- 与现有因子的去重 IC（>0.99 的会被自动过滤）
- 单因子回测收益

把通过筛选的新因子代码复制到 `factor_engine/expressions/custom_factors.py` 即可集成到现有 pipeline。

## 总时间估算

| 步骤 | 时间 |
|------|------|
| 安装包 | 10 min |
| 配置 LLM | 15 min |
| 数据格式转换 | 2-3 hrs |
| 港股场景适配 | 1-2 hrs |
| 运行因子挖掘 | 1-2 hrs |
| 提取 & 集成 | 30 min |
| **合计** | **5-8 hrs** |

## 风险点

1. **Qlib 港股兼容性**：Qlib 原生为 A 股设计，日历、复权逻辑、停牌处理需验证是否兼容港股
2. **数据量**：港股 2780 只股票 vs A 股 5000+，因子挖掘的统计显著性可能不足
3. **LLM 成本**：10 轮迭代约消耗 100-500K tokens，DeepSeek API 约 ¥1-5
4. **Docker 稳定性**：每个因子评估启动一个新容器，macOS Docker 性能可能成为瓶颈

## 简化替代：手动设计因子

基于已完成的 668 只股票相关性分析，直接实现高潜力因子：

| 数据依据 | 相关性 | 候选新因子 |
|----------|--------|-----------|
| PB 成长溢价 | +0.366 | `pb_ratio_sector_relative` |
| 超热动量 | +0.375 | `price_position_52w_high` |
| 冷门板块反转 | -0.217 | `sector_rps_reversal_20d` |
| 行业资金流 | — | `sector_turnover_change_5d` |
| 量价背离 | — | `volume_price_divergence_10d` |
| 连阳连阴 | — | `consecutive_up_days_5d` |

这部分 1 小时内可完成，立即验证效果。
