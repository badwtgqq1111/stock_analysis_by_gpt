# P2-35 选股归因接进每日流程 + 页面（2026-10-09，第三十一轮）

> 触发：上一轮列的四个遗留里的一条 ——「归因分析仍未接进每日流程、也无页面（产物字段很丰富，含个股 tilt 四分项、正负因子、族贡献）」。
> 证据：`artifacts/attribution_page/`（含 `VERIFICATION.txt`、`baseline/`、`diffs/`、`ROLLBACK.sh`、回滚复验输出）。

## 1. 问题
`scripts/pick_attribution_report.py` 早就在仓库里，`output/results_cn/attribution/<date>/pick_attribution.csv`
字段也早就很丰富（`strategy/channel/sleeve/recipe/rank/raw_pred` + `tilt_points` 与
`tilt_from_{dist,high,volume,flow}` 四个分解项 + `top_positive_factors/top_negative_factors` + `fam_*` 族贡献），
但是：

1. 它的**上游** `explain_selection.py` 躺在 `output/verification/p1_22_shap_attribution_20260925/` 里
   —— 那是一次性验证产物的目录，谁也不会去那儿找脚本；
2. 它自己**每天没人调度**，`run_daily_production.sh` 里引用数为 0
   ⇒ `output/results_cn/attribution/` 最新只到 **2026-09-29**，等于一个月没更新；
3. 前端没有页面，要看归因得手动去翻 csv。

## 2. 改动
| 位置 | 改动 |
|---|---|
| `scripts/explain_selection.py` | 从 `output/verification/…` **搬进** `scripts/`（输出目录不变，仍写 `output/results_cn/attribution/<date>/`）；加注释说明来源 |
| `scripts/run_daily_production.sh` | 新增 `attribution` 块：`explain_selection.py <date>` → `pick_attribution_report.py <date>` 两步串联，**非致命**，`DAILY_ATTRIBUTION=0` 可关；dry-run 也列出（当时 21 步 → 23 步） |
| `backend/api/attribution.py`（新） | `GET /api/attribution`（可用日期）、`GET /api/attribution/{date}`（23 列 + 逐股 12 条族贡献 + markdown 原文 + winners_vs_losers） |
| `frontend/src/pages/Attribution.vue`（新） | 「选股归因」页，导航「研究」组 |
| `scripts/archive_cn_selection.py` | 新增「归因回填」分支 + `weight_source` 列（见 §3） |

成本提醒（写在脚本注释里）：`explain_selection` 要加载 LGB 模型算全市场 SHAP，`shap_detail` 一次约 **30MB**。

## 3. 顺带捞回 3 天历史（遗留第 2 条的一半）
`pick_attribution.csv` 里有 `in_selection` 标记 ⇒ 能知道**当时**哪些票进了最终名单。于是
`archive_cn_selection.py` 多了一条回填来源：把 09-24 / 09-28 / 09-29 三天的入选代码补进
`paper_selection_history.csv`，决策日 **7 → 10**、行数 35 → 49。

**限制必须说清楚**：这些行的 `target_weight` 全是 0（那是中间量，不是下单权重），只能用**等权兜底**。
所以新增 `weight_source` 列把它们标成 `equal_weight_fallback`，和另外 7 天的 `actual` 分开；
引用这 10 天的净值时必须说明其中 3 天是等权近似。

## 4. 验收（细节见 `artifacts/attribution_page/VERIFICATION.txt`）
- 链路：`explain_selection.py 2026-10-08` + `pick_attribution_report.py 2026-10-08` 均 exit 0；
  可用日期 `['2026-10-08','2026-09-29','2026-09-28','2026-09-24']`。
- API：`/api/attribution/2026-10-08` 返回 8 行 × 23 列，`family_columns` 12 类，
  示例入选股 `002708.SZ` 带 12 条族贡献。
- 页面：浏览器实测导航/表格/族贡献条形图/markdown 原文都在，`console errors: none`；
  截图 `artifacts/attribution_page/evidence/ui/attribution.png`。
- 回滚：临时树复验 **OK=10 DIFF=0**，baseline 版脚本不再引用 attribution。

## 5. 仍未做
- `winners_vs_losers.md` 目前全 NaN —— T+N 收益窗口还没到，需要等后续交易日；
- 归因**没有回灌**到选股决策（只做展示，不做在线学习/自动调参）；
- 第三十二轮又改了本轮的 4 个文件（`backend/main.py`、`run_daily_production.sh`、`client.ts`、`types/api.ts`），
  本 artifact 的 `modified/` 是"第三十一轮结束时"的快照，不再等于 live。
