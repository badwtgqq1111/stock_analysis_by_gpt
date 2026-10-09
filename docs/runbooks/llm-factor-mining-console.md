# Runbook：LLM 因子挖掘对话面板

关联：`docs/todo/P1_25_llm_factor_mining_plan.md`、`scripts/llm_factor_mining.py`、`backend/api/factor_mining.py`。

## 1. 启动

```bash
cd /Users/ccs/code/quant/stock_analysis_by_gpt
cp config/scheduler_env.sh /tmp/scheduler_env.sh   # 或确认 DEEPSEEK_API_KEY 已在环境里
set -a && source /tmp/scheduler_env.sh && set +a
uv run uvicorn backend.main:app --port 8000
```

打开：

- Vue 页面：`http://127.0.0.1:8000/#/factor-mining`
- 无构建依赖的单页：`http://127.0.0.1:8000/api/factor-mining/page`
- 健康检查：`curl -s localhost:8000/health` → `{"status":"ok"}`

## 2. 用法

1. 填「挖掘方向」（自然语言，写清想要的经济含义与要避开的已有因子）；
2. 第一次先勾 **自检模式**（秒级、不调 LLM）确认评估链路与面板可用；
3. 再去掉勾选正式跑（`deepseek-flash` 一次 2 轮 × 6 候选约数分钟）；
4. 结果区只列**通过 BH-FDR(q=0.05)** 的因子，按 `|ICIR|` 排序；产物目录显示在状态栏。
5. **历史会话**：两个页面都有「历史会话」表（创建时间 / 状态 / 方向 / 消息数 / 因子数 → 载入）。
   会话落盘在 `output/factor_mining_sessions/<id>.json`，**uvicorn 重启后仍可点开复看**；
   重启时仍在跑的会话会显示 `interrupted`。

## 3. 命令行等价物（面板只是调用者）

```bash
cd /Users/ccs/code/quant/stock_analysis_by_gpt
uv run python scripts/llm_factor_mining.py --rounds 1 --dry-run
uv run python scripts/llm_factor_mining.py --rounds 2 --proposals 6 --model deepseek-flash \
  --direction "A 股小市值与量价/估值的交互因子：要求能在月度横截面上稳定排序，避免与 pe/pb/rps/rev 单因子高度重合"
ls output/research/llm_factor_mining_*/
```

## 4. 排障

| 现象 | 原因 / 处理 |
|---|---|
| `GET /` 返回 `{"detail":"Not Found"}` | 前端构建产物缺失或路径不对；确认 `stock_analysis_by_gpt/frontend/dist/index.html` 存在 |
| 会话 `status=failed`，日志 `HTTP 429/5xx` | DeepSeek 限流；等一会儿重发同一会话方向即可，脚本内置 3 次重试（间隔 5/10/15s） |
| 只想验证后端联通、不想等 LLM | 勾「自检模式」（`dry_run=true`） |
| 内存吃紧 / 机器变卡 | 面板加载约 2GB；服务端串行执行，别同时开两个会话（已用全局锁兜底） |
| 历史会话列表空了 | 确认 `output/factor_mining_sessions/` 是否被清空/换目录；该目录是会话落盘位置 |
| 会话一直显示 `running` | 若服务被杀，重启后会自动改判 `interrupted`；再次重启或刷新页面即可看到 |
| `bun run build` 报类型错误 | 类型债已于 2026-10-07 修完，`bun run build`（含 `vue-tsc -b`）应退出码 0 |

## 5. 因子验真（OOS / DSR / PBO / 增量信息）

挖完必须再过一遍这个，否则"全样本 IC + 单次 BH-FDR"说明不了问题：

```bash
cd /Users/ccs/code/quant/stock_analysis_by_gpt
uv run python scripts/factor_oos_deflated.py --run output/research/llm_factor_mining_<ts>
# 产物: <run>/oos_deflated/REPORT.md（人读）+ deflated_summary.csv（逐因子指标与未过原因）
```

看三件事：**折间 IC 同号率**（时间稳定性）、**DSR/PBO**（多重检验与过拟合概率）、
**残差 IC + FM t**（相对现有 25 列到底有没有增量）。当前实测结论见 `P1_25` §7：0/20 通过，
瓶颈是增量信息而非稳定性。

## 6. 面板 v2（补数据）与在新面板上挖掘

```bash
cd /Users/ccs/code/quant/stock_analysis_by_gpt
# 1) 生成面板 v2（v1 + 12 财务字段[PIT] + fin_age_days + size_ind_pct + listed_days + soe_flag）
uv run python scripts/panel_extend_v2.py
# 2) 在 v2 菜单上挖掘（41 字段）
uv run python scripts/llm_factor_mining.py \
  --panel assets/data/meta/cn_sleeve_features_long_v2.parquet \
  --rounds 2 --proposals 6 --model deepseek-flash --direction "<方向>"
# 3) 验真（控制集仍是 v1 的 25 列，保证"相对现有模型有没有增量"口径不变）
uv run python scripts/factor_oos_deflated.py \
  --run output/research/llm_factor_mining_<ts> \
  --panel assets/data/meta/cn_sleeve_features_long_v2.parquet
```

实测（`P1_25` §8）：残差 IC 上限 0.0116 → **0.0428**，通过因子 **0/20 → 2/12**；
但 PBO 0.000 → **0.603**，含义是"别挑单一最优，要用 sleeve 组合"。

> 数据缺口（别再重复排查）：分时 5min 只有 2025-10-09 起、alpha zoo 只有 2025-03-20 起、
> 资金流/筹码 2024-08-23 起、`ps_ind_pct` 上游全空；退市股日线要并需重算特征（`P1_25` L8/L9）。
