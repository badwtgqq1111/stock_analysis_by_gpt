# P2-33 模拟盘空转修复 + 「模拟盘（前向）」页（2026-10-09）

> 触发：用户「好的，继续」。上一轮末尾给了三个候选（归因分析 / 模拟交易 / 绩效接进每日流程），
> 选**模拟交易** —— 一查发现它**从来没买进过任何票**。
> 证据：`artifacts/paper_trading_fix/`。

---

## 1. 发现：模拟盘 496 天净值恒为 45,000

```
$ wc -l output/paper_trading/account/*.csv
      1 fills.csv        ← 空表
    497 nav.csv          ← 496 行，恒为 45,000
      4 orders.csv       ← 4 条全 pending、executable_from 为空
      1 positions.csv    ← 空表
```

### 根因

`run_paper_account(selections, bars)` 的契约（读源码）：
> The account treats each selection date as a target-weight rebalance.
> Orders use the first later available bar, enforcing T+1.

而调用方喂的是 `output/results_cn/cn_ensemble_selected.csv` —— **只有一天**，
且 trade_date = 最新交易日 = bars 的最后一根 ⇒ `next_date=None`
⇒ 走"最新决策还没有 T+1 bar"的分支 ⇒ **只挂 pending、永不成交**。
第二天该文件被覆盖成新的当天，**历史决策从来没被保留**。

这与 `arm_latest_selection.csv` 是同一类 bug（每天覆盖、无历史）——上一轮修了 arm 的归档，漏了生产名单。

---

## 2. 修了什么

| 改动 | 说明 |
|---|---|
| `scripts/archive_cn_selection.py`（新） | ① 归档当天 → `selection_history/<date>.csv`；② 从 `feishu_*.json`（6 天）与 `cn_ensemble_selected_<YYYYMMDD>.csv`（1 天）**回填**；③ 汇总 → `paper_selection_history.csv` |
| `config/cn_pipeline.toml` | `[paper_account].selection_path` → 历史文件（并写明为什么不能喂单日文件） |
| `scripts/run_daily_production.sh` | `pk` 之后新增 `archive-selection` 步骤（非致命）——否则历史不会继续增长 |

**修复效果**：fills `0 → 28`、positions `0 → 52`、期末净值 **44,478.59（−1.16%）**，
真实的 T+1 成交（decision 2026-09-09 → fill 2026-09-10，`603938.SH` buy 100 @46.3732）。

---

## 3. 「模拟盘（前向）」页

导航新增「模拟盘」。页面开头就把口径讲清楚：

> **唯一的「前向」口径**：按当天实际最终名单下单、T+1 开盘成交、扣佣金滑点、逐日盯市。
> 它不是回测——回测是把历史重算一遍，这里是「照着当天的名单真的下单会怎样」。
> 历史只有 **7 个决策日**（2026-09-09 ~ 2026-10-08）——因为最终名单此前每天被覆盖、没有归档。

⚠️ 不是实盘；样本仅一个月，**不构成统计结论**。

含：指标卡（初始/期末/收益/回撤）、净值曲线、当前持仓（只展示最新一天）、成交流水。

---

## 4. 过程中的 3 个显示问题

1. **净值曲线前两年是平的**：账户 2024-09 就建了，但第一次决策前都是"没买东西"的 45,000
   ⇒ 496 行里 470+ 行是平的，画出来把有信息的 16 天压成一条竖线 → 曲线从首个决策日截取。
2. **持仓表混了多天**（`tail(40)` 把 09-21~10-08 混在一起，像"同时持有 40 只"）→ 只取最后一天。
3. **成交价浮点噪声**（`31.034475000000004`）→ API 层 round(4)。

另外又踩了一次 `pd.Timestamp(mtime, unit="s")` 是 UTC 的坑（页面少 8 小时）。

---

## 5. 遗留

1. **样本只有 7 个决策日 / 约 1 个月，−1.16%** —— 不构成统计结论；价值在于"证明管道跑通 + 口径载体正确"。
2. **更早的最终名单已永久丢失**（每天被覆盖），只能回填 6 天；从今天起才持续累积。
3. 模拟盘与回放引擎**不是同一套执行假设**：回放显式建模停牌/涨跌停/退市清算/整手；
   `run_paper_account` 只做 T+1 + 整手 + 佣金滑点 + 缺 bar 挂 pending。要严格对齐需统一（未做）。
4. **归因分析**仍未接进每日流程、也无页面（上轮三个候选中剩下的）。
