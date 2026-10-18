# 行业热度动态曲线

页面：`行业热度 → 3D 动态排名`（`frontend/src/pages/IndustryHeat.vue`）。

实现：纯 SVG 多线图（无 WebGL 依赖，任何浏览器都能渲染）。
3D 排名折线/地形在行业一多时遮挡严重、颜色趋同，已放弃；改用最直观的
`X=时间, Y=热度, 每条线=一个行业`。

## 数据

```bash
python3 scripts/build_industry_heat_history.py --days 40 --top 20
```

- 逐交易日重算 `/api/industry/heat` 同一口径的行业热度（5 分项横截面 z → 0~100）；
- 取**最新交易日**热度排名前 20 的行业（与页面热度榜同一批）；
- 输出 `output/research/industry_heat/history.json`：
  `dates[]`、`industries[]`、`ranks[industry][date]`、`heats[industry][date]`。
- 每日自动化：`DAILY_INDUSTRY_3D=1`（默认），约 2 分钟。

API：`GET /api/industry/heat-history?days=40`（只读产物）。

## 坐标与交互

- X = 时间（最近 40 个交易日）；
- Y = 热度 0~100；
- 每条线 = 一个行业（默认当前前 8，可切 8/12/16），颜色区分行业；
- 右侧标签 = `#最新排名 行业名`；
- 悬停竖线：显示当天热度前 6 的行业与排名；
- `播放`：按时间推进，看热度如何此消彼长。

## 口径提醒

- 3D 图只展示**排名与热度变化**，不代表未来收益；
- 行业热度本身尚未做行业层面前瞻检验（见 `industry_heat_service.py` 模块说明）；
- 排名轴是 110 个行业里的横截面排名，不是只在这 20 个行业内部重排。
