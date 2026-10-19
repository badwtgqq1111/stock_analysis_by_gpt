# Runbook：单票「多空 AI + 研报证据」按需更新

前端入口：K 线页/选股页的「个股多空」面板 → **更新多空**。

## 流程

1. **研报取证**：先调用 Bynewlt `search_company_research_evidence`（成功检索可能消耗 1 次额度）；
   未收录/额度/网络失败时改用 `cn_public_stock_research.py` 的公开来源。
2. **多空稿**：对证据执行 `draft`（DeepSeek + 逐字引文核对），再 `promote --level auto_checked`。
3. **AI 决策**：把刚生成的研报上下文、持仓/动量信息一起喂给
   `analyze_strategy_picks_ai.py --codes <code> --date <今天>`，写回
   `output/results_cn/ai_pick_analysis/<date>.json`。

任意自选股都可以更新，不要求在当日策略名单里；这类行使用
`watchlist_manual`（前端显示“自选股按需分析”）。

## 接口

```text
POST /api/stocks/{code}/longshort/refresh
GET  /api/stocks/{code}/longshort/refresh/{job_id}
```

任务文件：`output/results_cn/longshort_jobs/<job_id>.json`。

状态：`queued → running → done / partial / failed`；`percent` 为 0–100，
`message` 写当前阶段，`error` 记录可诊断错误。

## 计费与复用

- 同一代码同时只允许一个任务，重复点击会复用当前任务。
- 10 分钟内已取回的 Bynewlt/公开证据会复用，不重复发起计费检索；AI 仍会重算。
- 只有 `status=done` 且 AI 行 `status=completed` 才算完整更新；`partial` 表示研报已更新但
  AI 未完成（常见原因：`DEEPSEEK_API_KEY` 未配置或模型返回 pending）。

## 手工命令

```bash
uv run python scripts/bynewlt_stock_research.py fetch --codes 600066.SH --refetch --max-calls 1
uv run python scripts/bynewlt_stock_research.py draft --codes 600066.SH --refetch
uv run python scripts/bynewlt_stock_research.py promote --code 600066.SH --level auto_checked
uv run python scripts/analyze_strategy_picks_ai.py --date 2026-10-19 --codes 600066.SH
```
