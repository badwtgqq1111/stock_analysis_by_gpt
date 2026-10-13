# P2.57 策略抽象层：一条声明 → 所有消费端自动接线

> 用户（2026-10-13）："能把这些策略都做成抽象统一的么？方便后续新增策略的时候，
> 可以在 web 端自动同步好数据，把策略行为的共性提取出来做一层抽象。"

## 0. 问题不是"没有配置"，而是**共性散落在 6 个地方**

| 策略行为（共性） | 以前写在哪 | 新增一条策略时会发生什么（实测） |
|---|---|---|
| 每日刷新（跑什么/超时/当日无票） | `config/strategies.json` + 各脚本 | P2.56 已修：有契约、七态、体检点名 |
| **选股结果页的数据源**（roles/文件/展示列/code_style/口径说明） | `backend/services/selection_sources.py` 硬编码 `SOURCES` | **必须手改 Python**；漏改的表现是 `/api/selection?source=xxx` 回退默认源，**页面上看不出是接线漏了** |
| **曲线口径**（名单跟踪/影子净值/独立回放/原始逐笔） | `scripts/build_strategy_pool_performance.py` 里两个硬编码 dict | **必须手改 Python**，否则策略池里没有中证500 对比曲线 |
| 台账来源（选股日历） | `build_selection_ledger.py` 的 per-source 解析 + `ledger_sources` | 声明了但没人解析 ⇒ 日历里永远缺这一天 |
| 同花顺导出 | `export_ths_watchlist.py` 硬编码五源 | 新策略**永远不会**出现在导出里 |
| K 线自选 / AI 点评 / 体检 | 各自依赖 `selection_source` / `history_dir` | 有的自动、有的不会出现 |

## 1. 抽象层：`config/strategies.json` 是唯一声明，其余全部派生

新增 `backend/services/strategy_registry.py`（抽象层核心）：

```
specs() / spec(id)         # 归一化后的策略声明
selection_sources()        # → selection_sources.SOURCES（选股结果页的数据源）
curve_plan(spec)           # → 曲线怎么画（tracking / replay / shadow / original）
consumers(spec)            # → 这条策略接到哪些消费端、靠什么接（接线表）
coverage()                 # → 覆盖矩阵：谁没接上、为什么（gaps vs by_design）
validate()                 # → 声明层问题清单（空 = 通过）
```

声明块（每条策略新增的部分）：

```jsonc
"selection_source": "cn_arm_rot",
"selection": {                                  // 选股结果页：文件/列/口径/代码风格
  "label": "CN 行业轮动手臂（rotation_arm）",
  "note": "……口径说明（直接显示在页面上）",
  "roles": [["research", "output/results_cn/rot_arm_selection.csv"]],
  "columns": ["rank", "stock_code", "board", "…"], "code_style": "ts_code", "supports_shap": false
},
"ledger_sources": ["rot_arm"],                  // 选股日历：进哪几条台账
"curve": {                                      // 曲线：影子净值 / 独立回放（可选）
  "shadow": {"nav_csv": "output/paper_trading/shadow/rot_arm/nav.csv",
             "history_csv": "output/results_cn/shadow_variants/rot_arm.csv"}
}
```

## 2. 消费端改成"派生"（这次动的代码）

| 消费端 | 改动 |
|---|---|
| 选股结果页 | `selection_sources.SOURCES` 由注册表派生（`_SourceMap` 懒加载 ⇒ **改完 config 不用重启**）；代码里只留非策略的遗留源（`hk_lightgbm`） |
| 曲线 | `build_strategy_pool_performance.py` 的 `replay_sources` / `shadow_sources` / 模型臂硬编码 → 读 `curve.replay` / `curve.shadow` / `model_arms` |
| 同花顺导出 | 新增 `--sources auto`：注册表里"还没有专门读法"的策略源自动成为导出源（实测一次多出 5 组） |
| 策略池页 | 新增「接线（抽象层）」面板 + 顶部"接线全覆盖/漏接 N 处"pill（`GET /api/strategies/registry`） |
| 每日流程 | 新增 `run_data_step strategy_registry … --check --save`（非阻塞，漏接线在日志里点名） |
| 体检 | 新增「策略接线（抽象层自检）」行（读 `output/strategy_pool/registry_check.json`） |

## 3. 新增一条策略现在的动作（唯一动线）

```bash
uv run python scripts/strategy_registry.py --new my_strategy \
    --kind rule_strategy --entry "uv run python scripts/select_my_strategy.py" \
    --selection-source cn_my_strategy --file output/results_cn/my_strategy_selection.csv \
    --refresh run                     # 打印骨架 + 干跑校验 + 打印"会自动接上哪些消费端"
# 补完骨架里的 TODO（summary/note/columns），再 --write 写进 config
uv run python scripts/strategy_registry.py --check      # 漏接线当场点名
```

写完即生效：选股结果页下拉多一项、K 线自选多一个分组、策略池多一张卡、每日刷新多一条契约、
同花顺导出多一组、体检多点名一行。**不需要改任何 Python。**

## 4. 实测（本轮结束时）

```
$ uv run python scripts/strategy_registry.py --check
[registry] 策略 10 条；消费端 8 个
  - strategy_pool   已接 10/10 ｜ selection_page 已接 9/10 ｜ selection_calendar 已接 8/10
  - kline_watchlist / ths_export / ai_picks / daily_refresh 各 9/10 ｜ web_freshness 10/10
[registry] 按设计未接 7 处（不是漏接线）：cn_ensemble_pk_legacy（影子对照·无独立名单）、
           live_holdings（实盘镜像·无台账来源）
[registry] ✅ 声明层校验通过
```

* **重构不改变行为**：派生 `SOURCES` 与迁移前硬编码值**逐字段一致**（10 条，冻结在
  `output/strategy_pool/selection_sources.frozen.json`）；曲线接线与迁移前硬编码的 replay(2)/shadow(3)
  逐条一致 —— 两条都写成了回归测试。
* **新增策略 = 只改声明**：测试 `test_new_strategy_needs_no_code_change` 往临时 config 追加一条策略后，
  `/api/selection?source=cn_demo_alpha` 立刻可用、`available` 里立刻出现（不改代码、不重启）。
* 全量预检 **42 个唯一测试文件 / 338 passed**。

## 5. 边界（不粉饰）

* 台账（选股日历）的解析器仍是**每个 source 一个适配器**（`build_selection_ledger.py`）：
  这层没有强行抽象 —— 各来源的列语义（rank/权重/备注）本来就不同，硬套一个模板会丢口径。
  抽象层做的是**登记 + 校验**：声明了 `ledger_sources` 却没有适配器，`--check` 当场点名。
* 实盘镜像（`live_holdings`）不是策略：它没有台账来源、不下单，`by_design` 里如实标注。
* `--new --write` 只写声明、不生成策略实现脚本；产物与名单仍要策略自己产出（这是有意的）。
