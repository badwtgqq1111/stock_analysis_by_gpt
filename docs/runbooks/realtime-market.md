# Runbook：当日实时统计的降级链

页面：涨跌排行、行业热度、连板统计、分时行情、K 线左侧自选、实盘镜像。

## 全市场快照

`backend/services/realtime_market.py::snapshot()`：

1. **东财 clist**（`push2.eastmoney.com/api/qt/clist/get`）——含 `f62` 主力净额，优先；
2. 东财失败时 **腾讯批量快照**（`qt.gtimg.cn/q=`，每批 300 只，约 2 秒）——
   2026-10-19 实测 5,570/5,911 有效（94.2%），价格/成交量/成交额可用；
3. 两路都失败才返回 `available:false`，页面保留归档并显示原因。

腾讯快照 `flow_available:false`，因此：

- 涨跌排行、行业热度、连板统计：价格/成交额/涨停可实时；
- 行业热度的资金流分项暂缺（页面明示“按其余四项”）；
- 资金流排行仍显示归档数据，并显示“资金流实时源不可用”；
- 不把归档资金流伪装成实时资金流。

快照失败缓存 5 秒，成功 15 秒；页面 15 秒轮询。

## 页面审计

| 页面 | 实时源 | 当前行为 |
|---|---|---|
| 涨跌排行 | 全市场快照 | 实时；无 flow 不影响涨跌 |
| 行业热度（网格/热力图） | 全市场快照 + 本地日线 | 实时价格；资金流分项缺失时明示 |
| 行业热度（动态曲线） | 历史快照 `build_industry_heat_history.py` | 历史回溯，非实时 |
| 连板统计 | 全市场快照 + 历史日线 | 实时梯队；封板资金等字段源缺失留空 |
| 分时行情 | 腾讯 minute/query | 实时，30 秒轮询 |
| K 线左侧自选 | 腾讯批量快照 | 3 秒轮询；页面隐藏/非交易时段降频 |
| 实盘镜像 | 腾讯批量快照 | 15 秒轮询 |
| 模拟盘 | `/api/paper` | 盘中 15 秒刷新；不是实盘 |

## 验证

```bash
.venv/bin/python -m pytest test/test_realtime_market.py -q
curl -sS 'http://127.0.0.1:8000/api/industry/heat?period=daily&limit=3&live=1'
curl -sS 'http://127.0.0.1:8000/api/rankings/change?period=daily&limit=5&live=1'
```
