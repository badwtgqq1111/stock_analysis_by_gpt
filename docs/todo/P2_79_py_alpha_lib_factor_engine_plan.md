# P2.79 · py-alpha-lib 高性能因子计算库复用评估与落地计划

日期：2026-10-18  
状态：**评估完成，计划已落盘；尚未接入生产依赖**。  
审阅对象：[msd-rs/py-alpha-lib](https://github.com/msd-rs/py-alpha-lib)

- 仓库固定 commit：`843d177ec8ff50e6399a8c0538bd4b94f8a006a5`（2026-09-07）
- PyPI 版本：`py-alpha-lib 0.3.1`
- 许可证：BSD-2-Clause（与本仓库 Apache-2.0 兼容，保留版权声明即可）
- 实现：Rust + PyO3；Python >=3.11，NumPy >=2
- 本地探测环境：macOS arm64、Python 3.12.3、NumPy 2.4.5、Pandas 3.0.3；临时 venv `/tmp/pyalpha_probe_venv`

---

## 0. 结论先行

**直接复用计算核，不做全量二次开发。**

具体是：

1. **把 `py-alpha-lib` 当作 Rust 滚动算子后端**，通过一个薄适配层接入现有因子链路；
2. **保留现有 `factor_dsl` 作为公式语义的 source of truth**，Pandas 实现作为 reference；
3. **不 fork 整个仓库、不 vendor 整个项目、不替换因子池/版本/评测/回测链路**；
4. 只在三个缺口上做本地二次开发：
   - 面板布局适配（date-major → stock-major 的一维数组）；
   - 进程级 `set_ctx` 的并发隔离（独立 worker 或锁）；
   - 缺失算子/语义差异的包装（尤其 `NEUTRALIZE`、`amount`、`moneyflow`、财务列）。
5. **是否 fork 只看一个条件**：上游是否愿意提供“每次调用传入 Context”或 thread-local Context。若不愿意，优先用独立 worker 隔离，不必 fork；只有需要改 Rust 内核且无法上游合并时，才 fork `alpha-algo` crate。

一句话：**复用 90% 的计算，自己写 10% 的适配与安全边界；不要为了 10% 的缺口复制 100% 的代码。**

> 追加（2026-10-18）：**Polars 不是 py-alpha-lib 的替代品**。Polars 更适合做上游 ETL/面板操作，py-alpha-lib 更适合做下游滚动热算子；详见 §16 的真实面板对比。

---

## 1. 这个库到底是什么

### 1.1 定位

`py-alpha-lib` 是一个**滚动窗口/横截面算子库**，不是完整因子平台。它提供：

- 一维 NumPy 数组上的滚动计算；
- `groups` 分组并行；
- 横截面 `CC_RANK` / `CC_ZSCORE` / `GROUP_RANK` / `NEUTRALIZE`；
- WQ Alpha101、GTJA Alpha191 的示例实现；
- 表达式到 Python 代码的 transpiler（面向 WQ/GTJA 语法）；
- Lua/JS/WASM 绑定（与本项目无关）。

它**不提供**：

- PIT 数据层、财务公告日、停复牌/涨跌停处理；
- 因子版本、来源、试验台账；
- IC/ICR/DSR/PBO/成本后组合；
- 多租户/并发安全的上下文；
- 任意列（资金流、财务、行业）的 `ExecContext` 数据模型。

因此，它适合做“计算核”，不适合做“因子平台”。

### 1.2 算子覆盖

仓库 README/`algo.md` 列出的算子覆盖：

| 类别 | 代表算子 |
|---|---|
| 滚动统计 | `MA/SUM/STDDEV/VAR/CORR2/COV/SKEWNESS/KURTOSIS/QUANTILE` |
| 滚动位置 | `HHV/LLV/HHVBARS/LLVBARS/TS_RANK/TS_ARGMAX/TS_ARGMIN` |
| 回归 | `BETA/ALPHA/REGBETA/REGRESI/SLOPE/INTERCEPT` |
| 横截面 | `CC_RANK/CC_ZSCORE/GROUP_RANK/GROUP_ZSCORE/NEUTRALIZE` |
| 时序信号 | `REF/DELTA/EMA/WMA/DMA/BARSLAST/BARSSINCE/SCAN_ADD/SCAN_MUL` |
| 其他 | `ENTROPY/MAX_DRAWDOWN/SHARPE/FRET/SPLIT` |

对本项目最有价值的是：`MA/STDDEV/CORR2/TS_RANK/REGBETA/CC_RANK/GROUP_RANK`，它们正好覆盖现有 `factor_dsl` 与 AlphaGen/AlphaQCM 的大部分重计算。

### 1.3 性能声明与本地实测

仓库声明：4000 只股票 × 261 天、101 个 Alpha101 因子，pandas 约 44 分钟，alpha-lib 约 3.6 秒，约 729x。

这是**仓库作者的基准**，不是本项目复跑。本机做的小面板探测（800 只 × 300 天 = 24 万行，单进程、未限制 rayon 线程、严格窗口）：

| 操作 | alpha-lib | pandas | 本地观察 |
|---|---:|---:|---:|
| `MA20` | 0.10 ms | 44.73 ms | 约 447x |
| `STDDEV20` | 0.19 ms | 38.48 ms | 约 202x |
| `CORR2(close, volume, 20)` | 0.12 ms | 未单测 | 亚毫秒 |
| `CC_RANK(ret20)` | 0.85 ms | 未单测 | 亚毫秒 |
| `ExecContext` 构建 | 8.28 ms | — | 一次性开销 |

结论：**滚动重计算确实值得换后端**。但这不是“整条因子链路快 729x”；数据加载、PIT 对齐、面板转换、评测与回测仍占大头。上表是单次小面板测量，不作为端到端或生产吞吐承诺。

---

## 2. 本地正确性探测

### 2.1 安装与轮子

PyPI 提供 `cp311-abi3-macosx_11_0_arm64` wheel；本机 Python 3.12 可以直接安装，无需 Rust 工具链。PyPI 同时提供 Linux/Windows wheel 与 sdist。

这意味着：

- 开发机可直接用 wheel 做 PoC；
- CI/服务器若不匹配 wheel，需要 Rust + maturin 构建；
- 生产环境应固定版本与 wheel hash，避免浮动升级。

### 2.2 严格窗口下与 pandas 对齐

用 600 只 × 260 天面板，设置：

```python
alpha.set_ctx(groups=600, flags=alpha.FLAG_STRICTLY_CYCLE)
```

结果：

| 指标 | 最大绝对差 | NaN 数量 |
|---|---:|---:|
| `MA20` vs pandas rolling mean | `6.39e-14` | 11,400 vs 11,400 |
| `REF(close,20)` 构造的 20 日收益 | `0.0` | 12,000 vs 12,000 |
| `CC_RANK(ret20)` vs pandas `rank(pct=True)` | `0.0` | 12,000 vs 12,000 |

结论：**严格窗口 + 正确面板布局下，核心算子与 pandas 对齐良好**。

另外用本仓库 `factor_dsl.evaluate('rank(close / delay(close,20) - 1)', frame)` 对照 200 只 × 90 天面板：最大绝对差 `0.0`，NaN 数量 `4,000 vs 4,000`。这说明现有四个基础算子里的 `mean/delay/rank` 可以直接映射。

### 2.3 但默认行为不一样

不设置 `FLAG_STRICTLY_CYCLE` 时：

- `MA` 在窗口未满时返回部分结果；
- `REF` 在初始位置用 `x[0]` 填充（CHANGELOG 明确写了这个行为）；
- 因此 `REF(close,20)` 在最初 20 天不是 NaN，20 日收益也不是 NaN。

如果直接拿默认模式跑因子，暖窗期会被当成有效值，IC/分层/回测都会偏。适配层必须：

1. 默认设置 `FLAG_STRICTLY_CYCLE`；
2. 或者显式输出 warmup mask，由上层统一删失；
3. 测试必须同时覆盖“默认/严格”两种模式，不能只测一种。

### 2.4 `NEUTRALIZE` 不是简单去均值

本地探测：`NEUTRALIZE(category, value)` 的输出范围是 `[0,1]`，与“行业内去均值后的残差”相关系数约 **0.9735**，不是 1.0。

读 Rust 实现可确认：它先按 category 求均值、取残差，再对残差做**全市场百分位 rank**。所以：

- 如果公式需要“行业内 demean 后的连续残差”，不能直接用 `NEUTRALIZE`；
- 如果公式需要“行业内残差的百分位”，它才匹配；
- Alpha101 的 `IndNeutralize` 语义需要逐条核对，不能因为名字相同就认为等价。

### 2.5 全局 Context 的并发风险

`py-binding/src/lib.rs` 使用：

```rust
static _ALGO_CTX_: LazyLock<RwLock<Context>> = ...
```

`alpha.set_ctx(groups=...)` 修改的是**进程级全局状态**。本地实测：

```text
set_ctx(groups=600) → CC_RANK(ret)
set_ctx(groups=300) → CC_RANK(ret)
结果不同，最大差 0.0733
```

在 FastAPI 多线程/多请求环境里，如果请求 A 设置 5000 只、请求 B 设置 300 只，二者会互相污染。**这是接入生产前必须解决的问题**，不是性能问题。

更危险的是：绑定层的 `ctx()` 在 `try_read()` 失败时直接回退 `Context::default()`（`groups=1`），不会抛错。也就是说，即使没有显式切换 `groups`，一次读锁失败也可能让截面计算静默退化成单组计算。**不能依赖调用方“记得设置正确”来保证正确性。**

### 2.6 README 示例有误

README 写：

```python
rank = alpha.RANK(close)  # cross-sectional rank
```

当前 API 中：

- `alpha.RANK(input, periods)` 是**滚动时序 rank**；
- 横截面 rank 是 `ctx.RANK(input)` 或 `alpha.CC_RANK(input)`。

如果照 README 写，会直接 TypeError 或得到错误语义。接入时以 `alpha/algo.md` 和实际签名为准。

---

## 3. 与现有链路的匹配度

### 3.1 当前链路

| 层 | 现有实现 | 角色 |
|---|---|---|
| 公式语义 | `backend/services/factor_dsl.py` | P2.71 DSL AST、校验、Pandas 评测 |
| AlphaGen 表达式 | `backend/services/alphagen_native.py` | 白名单解析、Pandas 评测 |
| 因子池 | `backend/services/factor_pool.py` | 版本、hash、编辑、回滚 |
| 单因子体检 | `scripts/factor_inspector.py` | IC/ICIR/分位/换手/冗余 |
| 组合/回测 | `factor_replay.py`、`p2_71_*` | 成本、NAV、基准 |
| 挖掘 | `alphaqcm_adapter.py`、`alphagen_rl` | 搜索器 |

`py-alpha-lib` 只能替换“公式评测”里的重计算部分；不能替换版本、PIT、评测和回测。

### 3.2 算子映射

| 现有语义 | py-alpha-lib | 注意事项 |
|---|---|---|
| `mean(x,n)` | `MA(x,n)` | 严格窗口 |
| `delay(x,n)` | `REF(x,n)` | 默认会用 `x[0]` 填充；必须 strict 或 mask |
| `rank(x)`（截面） | `CC_RANK(x)` / `ctx.RANK(x)` | 输出 `[0,1]` 百分位；NaN 处理需对齐 |
| `abs(x)` | `ABS(x)` | 直接 |
| `std(x,n)`（待加） | `STDDEV(x,n)` | ddof 语义需核对 |
| `corr(x,y,n)`（待加） | `CORR2(x,y,n)` | 需核对 NaN/常数序列 |
| AlphaGen `Mean` | `MA` | 直接 |
| AlphaGen `Ref` | `REF` | 严格窗口 |
| AlphaGen `Rank` | `TS_RANK` | **AlphaGen 的 Rank 是滚动 rank，不是截面 rank** |
| AlphaGen `CSRank`（若启用） | `CC_RANK` | 不要与 `Rank` 混用 |
| 行业中性 | `NEUTRALIZE` / `GROUP_RANK` | `NEUTRALIZE` 是残差 rank，不是 demean |
| `amount` | 自定义数组 | `ctx.AMOUNT` 实际 alias 到 `VOLUME`，不可用 |
| 资金流/财务 | 自定义数组 | `ExecContext` 不原生支持任意列 |

### 3.3 数据布局差异

当前面板是 `date × stock` 的长表；库要求：

```text
[stock1_day1, stock1_day2, ..., stock1_dayM,
 stock2_day1, ..., stockN_dayM]
```

即 **stock-major、等长、连续 `float64`**，并设置 `groups = N`。

适配层必须：

1. 按 `stock_code, trade_date` 排序（不是 `trade_date, stock_code`）；
2. 转成矩形面板，缺失日期填 NaN；
3. 记录每只股票的日期覆盖，输出时映射回原索引；
4. 对停牌/上市不足的股票，不能把 NaN 当 0；
5. 对非连续数组做 `np.ascontiguousarray`（绑定层要求连续切片）。

---

## 4. 方案比较

| 方案 | 做法 | 工作量 | 风险 | 结论 |
|---|---|---|---|---|
| A. 直接依赖 + 适配层 | `pip install py-alpha-lib`，写 `fast_alpha.py` | 2-3 天 PoC，2 周生产 | 中：全局 Context、语义差异 | **推荐** |
| B. Fork `alpha-algo` crate | 复制 Rust 内核，改成每次传入 Context，加自定义算子 | 3-4 周 + Rust 维护 | 中高：升级/测试/构建 | 仅当上游不接受 thread-safe 改造 |
| C. Vendor 整个仓库 | 把仓库放进 `vendor/`，本地改 | 1 周起步，长期漂移 | 高：generic `alpha` 包名、升级困难 | 不推荐 |
| D. 自研 Rust 算子 | 重写滚动/截面算子 | 2-3 个月 | 很高：正确性/边界/性能 | 不推荐 |
| E. 继续 pandas | 不接入 | 0 | 慢，但语义熟悉 | 保留为 reference/fallback |

### 4.1 为什么推荐 A

- 计算核已经成熟，覆盖大部分需要的算子；
- BSD-2 许可证兼容；
- macOS/Linux/Windows 有 wheel；
- 本地严格窗口已与 pandas 对齐；
- 真正缺的是“适配 + 并发隔离”，不是“重写内核”；
- 保留 pandas reference，可随时回滚。

### 4.2 什么时候升级到 B

满足任一条件：

1. 上游拒绝/长期不处理 thread-safe Context；
2. 我们需要高频调用、不能接受 worker 进程池的序列化开销；
3. 需要新增 Rust 算子且上游不接受 PR；
4. 需要改变数据布局/内存模型（例如流式增量）。

即便 fork，也只 fork `alpha-algo` crate，保留 Python binding 的薄层；不要把整个 `py-alpha-lib` 复制进业务仓库。

---

## 5. 目标架构

```text
因子池 / 研究脚本
      │
      ▼
factor_dsl（AST、校验、版本语义）
      │  evaluate(formula, panel)  ← 公共接口不变
      ├───────────────┐
      ▼               ▼
Pandas reference   FastAlpha adapter
（默认/回滚）       │
                    ├─ 面板 → stock-major 连续数组
                    ├─ strict warmup / NaN mask
                    ├─ operator mapping
                    └─ AlphaWorker 进程池
                              │
                              ▼
                    py-alpha-lib (Rust kernels)
```

### 5.1 新增模块

| 路径 | 职责 |
|---|---|
| `backend/services/fast_alpha.py` | 面板转换、算子映射、`evaluate_fast()` |
| `backend/services/alpha_worker.py` | 独立进程 worker；每个进程固定 Context；超时/崩溃隔离 |
| `backend/services/alpha_engine.py` | 引擎选择 `pandas/alpha_lib`、缓存、fallback |
| `scripts/p2_79_alpha_parity.py` | 与 pandas 的逐算子/逐公式对照 |
| `scripts/p2_79_alpha_bench.py` | 性能/内存/并发基准 |
| `config/p2_79_fast_alpha.json` | 版本、开关、阈值、允许算子 |
| `test/test_p2_79_fast_alpha.py` | 单元/对照/并发测试 |
| `docs/runbooks/fast-alpha-engine.md` | 安装、切换、排障、回滚 |

### 5.2 公共接口

保持 `factor_dsl.evaluate(formula, frame)` 不变；新增：

```python
from backend.services.fast_alpha import evaluate_fast

values = evaluate_fast(formula, frame, engine="alpha_lib")
```

不直接把 `alpha` 调用散落到业务代码里；所有调用经过 `fast_alpha`，便于：

- 统一 strict 窗口；
- 统一 NaN 规则；
- 统一异常/超时；
- 一键回退 pandas。

---

## 6. 数据契约与适配细节

### 6.1 输入

```text
frame: stock_code, trade_date, open, high, low, close, volume, amount, ...
```

要求：

- `stock_code`、`trade_date` 唯一键；
- 价格同一复权版本；
- `volume` 单位统一；
- 缺失日期允许，但输出必须保持 NaN；
- 财务/资金流列按 PIT 对齐后再传入，不由本库处理时点。

### 6.2 转换

```text
1. 按 stock_code, trade_date 排序
2. 建立 codes = 唯一股票，dates = 唯一交易日
3. 生成 N×T 矩阵，缺失填 NaN
4. 展平为 N*T 的 float64 连续数组
5. set_ctx(groups=N, flags=STRICTLY_CYCLE)
6. 计算
7. reshape(N,T) → long panel → 按原索引回填
8. 断言 len == N*T、每行每列分组数正确；不满足则抛错，不返回“看起来正常”的结果
```

### 6.3 自定义列

`ExecContext` 只认 OHLCV + `indclass` + `cap`，且 `AMOUNT` 实际 alias 到 `VOLUME`。因此：

- 资金流、财务、行业、指数权重等列，**不要**用 `ctx('AMOUNT')`；
- 用 `alpha.*` 低层函数直接接收自定义 NumPy 数组；
- 行业中性用 `GROUP_RANK`/自定义 demean，不用 `ctx.NEUTRALIZE` 假装等价。

### 6.4 NaN 与暖窗

默认：

```python
alpha.set_ctx(groups=N, flags=alpha.FLAG_STRICTLY_CYCLE)
```

若需要跳过 NaN（例如停牌日不参与均值），显式组合：

```python
alpha.set_ctx(groups=N, flags=alpha.FLAG_SKIP_NAN | alpha.FLAG_STRICTLY_CYCLE)
```

两种模式的结果不同，必须在因子元数据里记录；同一因子不能一半用 strict、一半用 skip。

---

## 7. 并发与稳定性

### 7.1 问题

- `set_ctx` 是进程级全局；
- `RwLock` 只保护读写，不保护“设置 groups → 计算”这个事务；
- FastAPI 默认线程池会并发执行同步路由；
- 多个请求不同股票池时，rank/group 计算会串。

### 7.2 方案

**首选：独立 worker 进程池**

- 每个 worker 进程只处理一个任务；
- 任务内设置一次 Context，计算完返回；
- 进程崩溃不会拖垮 API 主进程；
- 可设超时、内存上限、重启策略；
- 代价是序列化/进程开销，适合批量因子计算，不适合每根 K 线实时调用。

**备选：模块级锁**

```python
with ALPHA_LOCK:
    alpha.set_ctx(groups=n, flags=...)
    result = alpha.MA(x, 20)
```

- 实现简单；
- 串行化所有计算，吞吐受限；
- 只适合低频/单用户研究。

**不推荐**：直接在主进程多线程调用不同 `groups`。

### 7.3 崩溃隔离

Rust panic 会终止进程；worker 必须：

- 捕获非零退出码；
- 返回结构化错误；
- 自动重启；
- 对同一任务最多重试一次；
- 记录输入 hash，避免无限重试。

---

## 8. 实施阶段

### Phase 0：隔离 PoC（2-3 天）

- 在独立 venv 安装 `py-alpha-lib==0.3.1`；
- 复现 `MA/STDDEV/CORR2/CC_RANK` 与 pandas 对照；
- 记录 wheel hash、Python/NumPy 版本；
- 不修改项目依赖。

**退出条件**：本地探测通过，确认 wheel 可用。

### Phase 1：适配层（W1）

- `fast_alpha.py`：面板转换、strict 窗口、算子映射；
- `alpha_engine.py`：`pandas/alpha_lib` 切换、fallback；
- 支持 `mean/delay/rank/abs` 四个现有算子；
- 50 条公式对照。

**退出条件**：50 条公式最大绝对差 <1e-10，NaN mask 一致。

### Phase 2：扩展算子与 AlphaGen（W2）

- 加 `std/corr/delta/ts_rank`；
- 映射 AlphaGen `Mean/Ref/Rank`；
- 明确 `Rank`=滚动、`CSRank`=截面；
- 处理 `amount/moneyflow/fundamental` 自定义列。

**退出条件**：AlphaGen 20 条表达式对照通过。

### Phase 3：并发隔离（W3）

- `alpha_worker.py` 进程池；
- 超时/崩溃/重启；
- 20 个并发请求不同 `groups`，结果与单线程一致。

**退出条件**：无上下文串扰；worker 崩溃可恢复。

### Phase 4：批量接入（W4）

- `factor_inspector.py` 加 `--engine alpha_lib`；
- AlphaQCM/AlphaGen 批量评测走 fast engine；
- 缓存面板转换结果；
- 记录每次计算的 engine/版本/耗时。

**退出条件**：1M 行 × 20 因子有可复现加速；结果与 pandas 一致。

### Phase 5：灰度与回滚（W5-W6）

- 生产默认仍 pandas；
- 研究/批量任务先切 alpha_lib；
- 前端/日志显示 engine；
- 一键回退 `ALPHA_ENGINE=pandas`；
- 观察一周错误率/延迟/内存。

**退出条件**：无正确性事故；性能达标；回滚演练通过。

---

## 9. 实验矩阵

| ID | 实验 | 对照 | 主指标 | 门禁 |
|---|---|---|---|---|
| E0 | 安装/导入 | 无 | wheel 可用、版本 | Python 3.12 + arm64 |
| E1 | 单算子 1D | pandas | 最大绝对差、NaN | `<1e-10`；NaN 一致 |
| E2 | 面板 50 公式 | `factor_dsl` | 最大绝对差、rank 并列 | 全部通过 |
| E3 | 截面 rank/中性 | pandas | 排名/相关系数 | rank 完全一致；中性语义单独标注 |
| E4 | 性能 24 万行 | pandas | 耗时/内存 | 滚动重算子 ≥5x |
| E5 | 性能 100 万行 | pandas | 耗时/内存 | ≥10x；RSS 可控 |
| E6 | 并发 20 请求 | 单线程 | 结果一致性 | 无串扰 |
| E7 | Worker 崩溃 | 正常 | 恢复时间 | 不拖垮 API |
| E8 | AlphaQCM 批量 | 现有引擎 | 端到端时间 | 不劣化正确性 |
| E9 | 回滚 | alpha_lib | 恢复 pandas | 一键、可复现 |

### 9.1 正确性门禁

- 非 rank 算子：`max_abs_diff < 1e-10`；
- rank 算子：并列名次完全一致；
- NaN/Inf mask 完全一致；
- warmup 区间完全一致；
- 不同股票顺序不改变结果（排列不变性）；
- 缺失日期不改变其他股票结果。

### 9.2 性能门禁

- 24 万行、滚动重因子：≥5x；
- 100 万行、20 因子：≥10x；
- 端到端（含面板转换）≥3x；
- 单因子小数据（<1 万行）不要求加速，允许 pandas 更快；
- 峰值 RSS 不超过当前 1.5x。

---

## 10. 与现有代码的接入点

| 现有模块 | 接入方式 | 风险 |
|---|---|---|
| `backend/services/factor_dsl.py` | 保持 `evaluate`；新增 `evaluate_fast` | 不改语义 |
| `backend/services/alphagen_native.py` | 解析不变；批量评测走 fast engine | `Rank` 语义 |
| `scripts/factor_inspector.py` | 加 `--engine` | 现有缓存/字段 |
| `scripts/p2_71_alphaqcm_backtest.py` | 因子矩阵计算走 fast engine | 结果需对照 |
| `backend/services/factor_replay.py` | 不回测层改计算 | 成本/NAV 不变 |
| `frontend` | 暂不改；日志显示 engine | 无 UI 阻塞 |

### 10.1 不建议改的地方

- 不在 `factor_dsl` 里直接 `import alpha`；
- 不在 FastAPI 路由里直接 `set_ctx`；
- 不把 `alpha` 作为全局单例；
- 不把 `amount` 映射到 `ctx.AMOUNT`；
- 不用 `NEUTRALIZE` 替代所有行业中性。

---

## 11. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 全局 Context 串扰 | 结果错、难复现 | worker 进程隔离；或全局锁 |
| 读锁失败静默回退 `groups=1` | 截面计算悄悄退化 | worker 隔离；结果层校验分组数；禁止静默回退 |
| Rust panic | API 进程崩溃 | worker 隔离 + 超时/重启 |
| 默认暖窗不同 | IC/回测偏 | 强制 strict；warmup mask |
| `NEUTRALIZE` 语义不同 | 行业中性因子错 | 单独测试；必要时自写 demean |
| `amount` alias volume | 成交额因子错 | 自定义数组；禁用 ctx.AMOUNT |
| 面板不完整/排序错 | rank/group 错位 | 适配层校验 + 排列不变性测试 |
| generic `alpha` 包名 | 依赖冲突 | 独立 worker venv/受限导入 |
| 版本漂移 | 结果变化 | 固定 0.3.1 + wheel hash |
| 测试覆盖少 | 隐藏 bug | 自建 50 公式 parity + Rust 边界测试 |
| 许可证 | 合规 | BSD-2 保留版权；NOTICE 记录 |

---

## 12. 时间表与工作量

| 周 | 工作 | 产出 | 工作量 |
|---|---|---|---|
| W1 | 适配层 + 50 公式对照 | `fast_alpha.py` | 3-4 人日 |
| W2 | 扩展算子 + AlphaGen | 算子映射表 | 3-4 人日 |
| W3 | Worker 隔离 + 并发测试 | `alpha_worker.py` | 3-5 人日 |
| W4 | 批量接入 + 性能 | inspector/AlphaQCM | 4-5 人日 |
| W5 | 灰度/回滚 | 开关、日志 | 2-3 人日 |
| W6 | 文档/复盘 | runbook、报告 | 2 人日 |

总计约 **3-4 周**（不含 fork Rust 内核）。若 fork，额外 2-4 周。

---

## 13. 交付物

- `backend/services/fast_alpha.py`；
- `backend/services/alpha_worker.py`；
- `backend/services/alpha_engine.py`；
- `scripts/p2_79_alpha_parity.py`；
- `scripts/p2_79_alpha_bench.py`；
- `config/p2_79_fast_alpha.json`；
- `test/test_p2_79_fast_alpha.py`；
- `docs/runbooks/fast-alpha-engine.md`；
- `output/research/p2_79/parity/`；
- `output/research/p2_79/bench/`；
- `output/research/p2_79/REPORT.md`。

### 验收命令

```bash
uv venv /tmp/p2_79_probe --python 3.12
uv pip install --python /tmp/p2_79_probe/bin/python py-alpha-lib==0.3.1 numpy pandas polars
/tmp/p2_79_probe/bin/python scripts/p2_79_alpha_parity.py --rows 240000
.venv/bin/python -m pytest test/test_p2_79_fast_alpha.py -q
.venv/bin/python scripts/p2_79_alpha_bench.py --stocks 800 --days 300 --factors 20
```

---

## 14. 验收清单

- [ ] 固定 `py-alpha-lib==0.3.1` 与 wheel hash。
- [ ] 25 万行、50 公式 parity 通过。
- [ ] strict/默认两种暖窗行为都有测试。
- [ ] `Rank`/`CSRank`/`TS_RANK` 语义分清。
- [ ] `NEUTRALIZE` 与行业 demean 的差异有测试。
- [ ] `amount`/资金流/财务列不经过 `ctx.AMOUNT`。
- [ ] 并发不同 `groups` 无串扰。
- [ ] worker 崩溃不拖垮 API。
- [ ] 1M 行性能 ≥10x（滚动重因子）。
- [ ] 端到端有可复现加速。
- [ ] 一键回退 pandas。
- [ ] 许可证/NOTICE 更新。

---

## 15. 最终判断

**直接复用，薄适配；不 fork，不 vendor，不重写。**

`py-alpha-lib` 的价值在 Rust 滚动/截面算子，不在因子平台。把它放在“计算核”位置，保留：

- `factor_dsl` 的公式语义；
- `factor_pool` 的版本/来源；
- `factor_inspector` 的评测；
- `factor_replay` 的成本/NAV；
- Pandas reference 作为回退。

真正要自己写的是：

1. 面板布局适配；
2. 严格暖窗与 NaN 规则；
3. 进程级 Context 隔离；
4. 语义差异的包装与测试。

这四件事做完，收益是“重计算快一个数量级”，而不是“换一个库就能提高年化”。性能提升必须通过同一套因子验证与成本门禁，不能把“算得快”误当成“因子有效”。

---

## 16. 追加：Polars 能否替代 pandas / py-alpha-lib？

### 16.1 结论

**不能把三者放在同一层比较。**

| 层 | 最适合的工具 | 原因 |
|---|---|---|
| 数据读取/清洗/Join/字符串/大面板 ETL | **Polars Lazy** | Arrow、列式、并行、谓词下推、scan parquet |
| 大量滚动/截面热算子 | **py-alpha-lib** | Rust 内核，单算子亚毫秒级，适合批量因子 |
| API 输出、绘图、统计模型、历史代码 | **pandas** | 生态兼容、MultiIndex/时间序列/第三方库 |

对当前仓库的建议不是“Polars 替换 pandas”，而是：

```text
Parquet/Arrow
   → Polars Lazy（读取/清洗/对齐/Join）
   → Arrow / 连续 NumPy 数组
   → py-alpha-lib worker（滚动/截面热算子）
   → Arrow / Polars
   → pandas（API 序列化、绘图、旧代码边界）
```

### 16.2 真实 A 股面板对比

数据：`assets/data/clean/ohlcv/market=CN` 的 2025–2026 日线 qfq，4 个 parquet，**2,268,085 行、5,354 只股票、431 个交易日**。  
环境：macOS arm64、Python 3.12、pandas 3.0.3、Polars 2.0.0、py-alpha-lib 0.3.1；单进程，未限制 rayon 线程。

| 操作 | pandas | Polars | py-alpha-lib | 说明 |
|---|---:|---:|---:|---|
| 读取 + 拼接 + 排序 | 2.831s | **0.107s** | — | Polars 约 26x |
| `MA20` 每股滚动 | 0.262s | 0.0241s | **0.001s** | alpha-lib 不含 Context 构建 |
| 20 日收益 | 0.312s | 0.0248s | **0.003s** | 严格窗口 |
| 截面 `rank` | 0.517s | 0.0395s | **0.005s** | pandas/Polars/alpha 结果对齐 |
| 20 日 `corr(close,volume)` | 0.628s | 0.153s | 未测全量 | 合成面板上 alpha 约 0.12ms |
| 内存（加载后） | 202.1MB | 183.7MB | — | 本数值面板仅低约 9%，不是 50% |

结论：

- **Polars 比 pandas 快，但不是所有操作 5-10x。** 本地面板：加载约 26x、`MA20` 约 10.9x、`rank` 约 13x、`corr` 约 4.1x；内存只低约 9%。
- **py-alpha-lib 的热算子又比 Polars 快约 8-24x**：`MA20` 约 24x、收益约 8x、rank 约 8x（单算子，不含 Context）。
- **Context 构建约 0.145s**。单因子任务 Polars 可能更快；20-100 个因子的批量任务，alpha-lib 的固定成本被摊薄，优势明显。
- **Polars 的强项在 ETL**：读取、拼接、排序、Join、Lazy 谓词下推；这些正是当前 pandas 最慢、最容易吃内存的部分。

### 16.3 对常见说法的校正

| 说法 | 实际 |
|---|---|
| “Polars 通常比 Pandas 快 5-10x” | 取决于工作负载；本地面板加载 26x、rolling 10-13x、corr 4x；不能一概而论 |
| “内存降低 50% 以上” | 本数值面板约 9%；字符串/分类/宽表可能更明显，但不是普遍保证 |
| “cudf.pandas / Modin 是 Rust 兼容层” | `cudf.pandas` 是 RAPIDS GPU 后端；Modin 是并行/分布式 pandas，不是 Rust 重写 |
| “Pandas 2.0 已被 Rust 重写” | pandas 主线仍以 C/Cython 为主；Arrow 后端经 pyarrow，`libpandas-rs` 仍是实验方向 |
| “Polars 可无缝替换 pandas” | API、缺失值、排序、索引语义不同；本仓库有 316 个 Python 文件导入 pandas，不能全量重写 |

### 16.4 选型矩阵

| 场景 | 选择 |
|---|---|
| 新 ETL 管道、分钟线清洗、Parquet/CSV 读取、Join | **Polars Lazy** |
| 100+ 个滚动因子反复计算 | **py-alpha-lib worker** |
| 单因子/小面板/一次性分析 | pandas 或 Polars；alpha-lib 的 Context 开销可能不划算 |
| FastAPI 现有路由、绘图、statsmodels、MultiIndex | **pandas** |
| 截面 rank/groupby/行业聚合 | Polars `over` 或 alpha-lib `CC_RANK`；都可用 |
| 行业 demean（非 rank） | Polars 显式 groupby demean；不要直接用 alpha-lib `NEUTRALIZE` |
| 并发多请求 | Polars 本身线程安全；alpha-lib 必须 worker 隔离 |

### 16.5 推荐落地顺序

1. **先 Polars 化 ETL，不动 API**：在 `factor_inspector.py`、AlphaQCM 数据准备、`build_*_features` 这类批处理脚本里用 `pl.scan_parquet`；输出保持 Arrow/Parquet，边界再转 pandas。
2. **再 py-alpha-lib 化热算子**：按 P2.79 W1-W3 做适配层与 worker 隔离。
3. **最后才考虑 pandas 边界替换**：只替换确有性能瓶颈且不依赖 pandas 特有语义的模块；不做 316 文件全量迁移。

### 16.6 验收指标

- Polars ETL：同一输入输出 schema/行数/排序一致；加载端到端 ≥5x；内存不高于 pandas。
- Polars 因子：与 pandas 最大差 `<1e-10`（rank 并列完全一致）。
- alpha-lib：与 pandas/Polars 三方一致；批量 20+ 因子端到端 ≥5x。
- 任何切换都能通过 `engine=pandas` 回退。

**最终建议：Polars 替换的是“pandas 的 ETL/DataFrame 层”，不是“py-alpha-lib 的滚动算子层”。两者组合，而不是二选一。**
