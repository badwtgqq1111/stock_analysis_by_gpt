# Docs Index

本文档目录按状态和优先级重新整理：

- `todo/`: 仍需推进的设计、研究、路线图和优化清单。
- `done/`: 已落地功能、部署说明、使用指南和历史总结。
- `reference/`: 论文、因子说明和外部研究资料。
- `report/`: 历史选股/LLM 报告。
- `superpowers/`: 早期规格和执行计划归档。

命名规则：`P0` 最重要或最常用，`P1` 次之，`P2` 偏长期或归档参考；同一优先级内用 `01/02/...` 排序。

## Todo

| 优先级 | 文档 | 主题 |
|---|---|---|
| P0 | [P0_01_quant_system_overall_design.md](./todo/P0_01_quant_system_overall_design.md) | 系统总体设计与阶段路线图 |
| P0 | [P0_02_ml_alpha_lightgbm_research_summary.md](./todo/P0_02_ml_alpha_lightgbm_research_summary.md) | 机器学习选股与 LightGBM 主线 |
| P0 | [P0_03_lightgbm_alpha158_optimization_roadmap.md](./todo/P0_03_lightgbm_alpha158_optimization_roadmap.md) | Alpha158/GTJA191 优化路线 |
| P0 | [P0_04_lightgbm_usage_review.md](./todo/P0_04_lightgbm_usage_review.md) | LightGBM 使用审计 |
| P0 | [P0_05_industry_data_selection_todo.md](./todo/P0_05_industry_data_selection_todo.md) | 行业数据与分行业选股 TODO |
| P0 | [P0_06_sector_neutral_vs_timing_optimization.md](./todo/P0_06_sector_neutral_vs_timing_optimization.md) | 分行业选股与行业择时拆分 |
| P0 | [P0_07_selection_optimizations_todo.md](./todo/P0_07_selection_optimizations_todo.md) | 选股优化 TODO |
| P0 | [P0_08_selection_filter_optimization.md](./todo/P0_08_selection_filter_optimization.md) | 选股硬过滤优化 |
| P0 | [P0_09_financial_factor_coverage_todo.md](./todo/P0_09_financial_factor_coverage_todo.md) | 财务因子覆盖与补全 TODO |
| P1 | [P1_01_alt_data_event_signal_design.md](./todo/P1_01_alt_data_event_signal_design.md) | 另类数据事件驱动信号 |
| P1 | [P1_02_stock_profile_graph_recommender_design.md](./todo/P1_02_stock_profile_graph_recommender_design.md) | 股票画像图谱与推荐式标签 |
| P1 | [P1_03_tag_registry_design.md](./todo/P1_03_tag_registry_design.md) | 行业与主题标签知识图谱 |
| P1 | [P1_04_portfolio_execution_rl_research.md](./todo/P1_04_portfolio_execution_rl_research.md) | 组合层与执行层 RL 研究 |
| P2 | [P2_01_third_party_integration_analysis.md](./todo/P2_01_third_party_integration_analysis.md) | 第三方项目整合分析 |
| P2 | [P2_02_paper_summary_optimizations.md](./todo/P2_02_paper_summary_optimizations.md) | 论文综述与优化方向 |
| P2 | [P2_03_quant_ecosystem_roadmap.md](./todo/P2_03_quant_ecosystem_roadmap.md) | Quant 生态路线图 |

## Done

| 优先级 | 文档 | 主题 |
|---|---|---|
| P0 | [P0_01_database_guide.md](./done/P0_01_database_guide.md) | 数据库使用说明 |
| P0 | [P0_02_lightrag_deployment.md](./done/P0_02_lightrag_deployment.md) | LightRAG 部署 |
| P0 | [P0_03_searxng_search_integration.md](./done/P0_03_searxng_search_integration.md) | SearXNG 搜索接入 |
| P0 | [P0_04_batch_save_guide.md](./done/P0_04_batch_save_guide.md) | 批量保存使用指南 |
| P0 | [P0_05_batch_save_feature.md](./done/P0_05_batch_save_feature.md) | 批量保存功能说明 |
| P1 | [P1_01_technical_deep_dive.md](./done/P1_01_technical_deep_dive.md) | 技术深度说明 |
| P1 | [P1_02_hotlist_api_guide.md](./done/P1_02_hotlist_api_guide.md) | 热榜 API 文档 |
| P1 | [P1_03_rdagent_setup.md](./done/P1_03_rdagent_setup.md) | RD-Agent 搭建指南 |
| P2 | [P2_01_project_summary.md](./done/P2_01_project_summary.md) | 项目总结 |
| P2 | [P2_02_implementation_complete.md](./done/P2_02_implementation_complete.md) | 功能完成报告 |
| P2 | [P2_03_implementation_summary.md](./done/P2_03_implementation_summary.md) | 功能改进总结 |
| P2 | [P2_04_02513_stock_profile_report.md](./done/P2_04_02513_stock_profile_report.md) | 02513 股票画像报告 |
