# P2-27 选股结果：多数据源与出处指纹（关闭工单 F2 / F4）

> 2026-10-08 第二十一轮。承接 `P2_26 §4 P1-1`。
> 证据：`artifacts/selection_sources/`（VERIFICATION.txt / evidence/ui/*.png / evidence/api_*.txt）。

---

## 1. 问题（两个，都是实测）

### 1.1 F2：「选股结果」页显示的名单不是系统在跑的名单

`/api/selection` 只读一个文件：

```
backend/services/data_service.py
  load_selected_data() → output/lightgbm_top10_selected.csv      ← 港股，5 位代码
```

而整个系统的主工作流早已是 A 股，真正每天在跑的生产名单在：

```
output/results_cn/cn_ensemble_selected.csv      ← preselection → PK 的最终入选
output/results_cn/cn_ensemble_preselected.csv   ← 候选池
output/results_cn/arm_latest_selection.csv      ← 研究手臂（P1_25 §20/§21）
```

结果是首页展示港股 5 只票、命令行跑出来的是 CN 8+10 只票，**页面上没有任何提示**。
（旧遗留 #5，`P2_26 §1` 已经把它列进盘点表。）

### 1.2 F4：「SHAP 因子贡献」区块**从来没有显示过任何数据**

页面上那个区块一直都在，但永远是空的。两个 bug 叠加：

```python
# backend/services/data_service.py（旧）
data = ast.literal_eval(factor_explanation_str)   # ← 这里抛异常
return data.get("stock_shap_values")
```

抛的是 `ValueError: malformed node or string on line 1: <ast.Name object at 0x...>`。
根因：CSV 里 `factor_explanation` 是 Python dict 的 repr，而里面夹了一个**裸 `nan`**：

```
... 'component_scores': {'composite_score': 86.47, 'quality_score': nan, ...} ...
```

`nan` 不是字面量，`ast.literal_eval` 遇到未定义名直接拒收。

```python
# backend/services/selection_service.py（旧）
parsed = parse_shap_values(str(explanation))       # 已经返回 {'positive': [...], 'negative': [...]}
stock_shap = parsed.get("stock_shap_values", {})   # ← 又取了一层，返回 {}
```

即使第一个 bug 修好，第二个也会让结果为空。

---

## 2. 修法

### 2.1 数据源注册表（F2）

新增 `backend/services/selection_sources.py`：把三个源显式登记，每个源声明
「文件列表 / 代码风格 / 展示列白名单 / 是否支持 SHAP」。

```
cn_ensemble   CN 生产名单（preselect → PK）   ← 默认
cn_arm        CN 研究手臂（base_v1）
hk_lightgbm   港股 LightGBM Top10（历史遗留） ← 本页 SHAP/重要性只对它有效
```

接口：`GET /api/selection?source=<key>`，响应新增

```json
{
  "source":   {"key","label","note","files":[{"role","path","mtime","rows"}],
               "trade_date","row_count","supports_shap"},
  "available": [{"key","label","available","supports_shap","files"}],
  "rows": [...], "columns": [...], "empty": false
}
```

`rows/columns/empty` 保持原结构 ⇒ 老调用方不受影响。未知 `source` 回退到默认源并在 `note` 里说明。

**踩过的两个坑（都留在代码注释里）**：

1. `preselected` 与 `selected` 天然重叠 —— 入选的票必然先被预选。
   直接 `concat` 会让 `301230.SZ` 出现两次、`rank` 都是 1。
   正确做法是外连接 + **逐列 coalesce**：只在 `preselected` 里出现的票不能丢 `rank`/`model_score`，
   而 `selected` 独有的 61 列（行业/收盘/手数/可交易）要能补上。
2. `stage` 标记必须打在 `selected` 侧。第一版打在 `preselected` 侧，
   结果所有票都在 preselect 里 ⇒ `stage` 全变成"候选池"，入选那两只认不出来。

### 2.2 统一解析 `factor_explanation`（F4）

`backend/services/data_service.py` 新增 `parse_explanation()`：先直接 `literal_eval`，
失败则把裸 `nan/inf/-inf` 替换成 `None` 再试（`None` 是合法字面量，语义上就是"缺失"）。
`parse_shap_values` 改为基于它，`get_shap` / `get_importance` 都走这一条路径。

`get_importance` 也改成**优先结构化解析** `top_features`、正则只作兜底。

---

## 3. 结果（实测）

### 3.1 三个源在页面上都能切，且带出处指纹

```
数据源 tabs: ['CN 生产名单（preselect → PK）', 'CN 研究手臂（base_v1）', '港股 LightGBM Top10（历史遗留）']

--- CN 生产名单（preselect → PK） ---
    表格行数 = 8
    来源指纹 = ['交易日 2026-09-30', '8 行',
               'preselected · cn_ensemble_preselected.csv · 2026-10-06 16:03',
               'selected · cn_ensemble_selected.csv · 2026-10-06 16:27']
    首行     = ['301230.SZ', '泓博医药', '1', '最终入选（PK 后）', '99.8526', ...]

--- CN 研究手臂（base_v1） ---
    表格行数 = 10
    来源指纹 = ['交易日 2026-09-30', '10 行', 'arm · arm_latest_selection.csv · 2026-10-08 08:57']
    首行     = ['603007.SH', '顺景科技', '1', '主板', '建筑工程', '0.9121', '是', '0.2000']

--- 港股 LightGBM Top10（历史遗留） ---
    表格行数 = 5
    来源指纹 = ['交易日 —', '5 行', 'selected · lightgbm_top10_selected.csv · 2026-05-24 01:43']
    首行     = ['01358', '111.5172', 'strong', 'pre_breakout', '86.4743', ...]

console/page errors: none
```

### 3.2 SHAP 从"永远空"变回有数据

```
$ curl -s localhost:8000/api/selection/01358/shap          # 修复前 → {"features":[]}
特征数: 10
  {'name': 'LOW0', 'value': 0.0381, 'direction': 'positive'}
  {'name': 'startup_breakout_gap_20', 'value': 0.0307, 'direction': 'positive'}
  {'name': 'CLOSE0', 'value': -0.0123, 'direction': 'negative'}
  ...

$ curl -s localhost:8000/api/importance
feature_count = 373 | train_rows = 4159779 | 因子数 = 10
  {'factor': 'CLOSE0', 'importance': 0.09526}
  {'factor': 'LOW0', 'importance': 0.074682}
  ...

浏览器（切到港股源）：
  h6 标题 = ['名单（5 / 5）', '评分榜（综合评分）',
            '全局因子重要性（Top 20，共 373 个因子）', 'SHAP 因子贡献（单股）']
  选股后 bar-row 数 = 25（5 评分 + 10 重要性 + 10 SHAP）
  state-block 数 = 0
```

### 3.3 顺带修好的小问题

* **港股名称一直是空的**：`get_stock_names_batch(['01358'])` 在本机 SQLite 注册表里返回 `{}`。
  改用 `docs/hk_entity_alias_registry.csv`（`alias_type=company_name`）后
  `01358 → 普华和顺`、`02550 → 宜搜科技`。注意该 CSV 的 `00001` 会被 pandas 读成 int，需 `zfill(5)`。

---

## 4. 这一轮踩的第三个坑：`useAsync` 不会自动取数

`useAsync` 第一版只返回 `reload`，不自动执行。页面 setup 里忘了调 ⇒
**组件正常渲染、一个请求都不发、没有任何 console 错误**，表现为"页面是空的但看起来没坏"。

已把 `immediate` 改成默认 `true`（挂载即取），并在注释里写明这个坑。

---

## 5. 边界（引用本轮结论必须一起说）

1. CN 生产名单两个文件都是 **2026-10-06** 生成的、交易日 **2026-09-30**；
   研究手臂是 **2026-10-08 08:57** 生成的。三者不是同一时刻的快照，页面只如实展示时间，不做对齐。
2. `cn_ensemble` 的「候选池」行只有 22 列（preselected 文件本身就这么宽），
   所以行业/收盘/手数在候选行上是 `—`，不是取数失败。
3. 港股源没有 `trade_date` 列，页面上交易日显示 `—`。
4. SHAP/重要性仍然**只在港股链路**成立（CN 名单没有 `factor_explanation` 字段）；
   页面按 `supports_shap` 分支并给出"切到港股源"的按钮，不做假的空图。
5. `source=bogus` 会静默回退到默认源并在 `note` 里说明 —— 不报 4xx，避免前端切换时闪错误页。
