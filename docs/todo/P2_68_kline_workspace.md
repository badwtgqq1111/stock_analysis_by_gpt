# P2-68 K 线工作台：全市场搜索、自选闭环与行情优先布局

## 目标与布局

入口不再藏在策略分组后面。使用既有 `/api/stocks/search` 主数据搜索接口，支持代码、中文名称、拼音首字母，以及主板、退市/摘牌筛选；搜索不依赖策略入选名单。

桌面顺序：
- 左栏：全市场搜股 → 当前标的加入自选 → 我的自选/全部策略/实盘 → 其他策略分组（展开）→ 股票列表 → 名单刷新状态（折叠）。
- 主区：时间/代码直达/叠加工具栏 → 标的行情 → 入选日历 → K 线与筹码 → 当日实时/指定历史日分时及集合竞价 → AI 多空分析 → 威科夫研究（展开加载）→ 信号统计、个股资料。
- 手机/平板：单列；自选列表限制高度，筹码窄屏压缩，canvas 随容器自动调整，宽表在面板内横滚。

## 实施清单

- [x] 复用并强化 `StockSelector`，移除侧栏重复的静默搜索/默认加第一个结果的逻辑。
- [x] 点击结果或键盘选中只切换图表；明确点击「加入自选」才写入，添加后切换「我的自选」。已在自选的按钮用于查看，重复添加幂等。
- [x] 名称与代码持久化沿用 `kline.watchlist.v1` / `kline.watchlist.names.v1`，保留旧自选；移除操作即时更新星标。
- [x] 加载、无结果和请求失败分别反馈；输入变化立即作废旧请求结果；清空、选中、切股、卸载均防过期响应。
- [x] 保留实盘六种排序下始终置顶；其余策略默认年化榜排序、组内个股排名和个人自选顺序保持原规则。
- [x] K 线、分时前置，分析和研究下沉；威科夫默认不请求，展开时加载；切股请求序号防串。
- [x] 保留原代码直达、当日分时/历史分时切换、集合竞价、入选日历及筹码开关。
- [x] K 线 `autoSize` 修复屏幕宽度/筹码开关变化后 canvas 留存旧宽度的问题。
- [x] 新增 9 项浏览器回归；旧 K 线日期、年化排序、策略池、个股资料共 62 项回归及前端分组脚本通过。
- [x] 真实服务验证 600519/贵州茅台/gzmt/688981 搜索、查看与添加分离、去重、刷新保留、移除；390/768/1440 宽度无页面横溢出。
- [x] 生产构建与独立副本回滚验收；本轮原始工作区快照为回滚基线，保留此前未提交工作。

## 验证入口

```bash
npm --prefix frontend run build
.venv/bin/python -m pytest -q test/test_kline_workspace_browser.py
.venv/bin/python -m pytest -q test/test_kline_watchlist_layout.py test/test_strategy_annual_ranking.py test/test_kline_today_browser.py test/test_strategy_pool.py test/test_stock_bundle_and_strategies.py
bash frontend/test/watchlistStrategies.sh
```

浏览器回归使用独立存储、可控行情与搜索响应；真实接口验收独立记录，搜索主数据命中与行情可用状态分别展示。

本轮证据：`output/verification/kline_workspace_20261016/VERIFICATION.txt`。`MODIFIED_FILE.tar.gz` 为修改文件快照；`DIFF_FILE.patch` 仅相对本轮进入时工作区；`ROLLBACK.sh` 通过哈希校验只恢复本轮文件，在独立副本上验证。
