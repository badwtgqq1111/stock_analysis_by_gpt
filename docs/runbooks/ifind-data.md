# iFinD 数据接入 runbook

## 1. 凭据

不要把 key 写进仓库、命令行参数、URL、日志或回复。二选一：

```bash
export IFIND_REFRESH_TOKEN='...'
```

或写到私有文件：

```bash
mkdir -p ~/.config/quant
chmod 700 ~/.config/quant
printf '%s' '...' > ~/.config/quant/ifind_token
chmod 600 ~/.config/quant/ifind_token
```

`scripts/ifind_client.py` 会用 refresh_token 换 access_token，并缓存：

```text
~/.config/quant/ifind_access_token       (600)
~/.config/quant/ifind_curl.conf          (600，curl -K 读取 header)
```

## 2. 已验证能力

| 能力 | 端点 | 结果 |
|---|---|---|
| 实时行情 | `real_time_quotation` | ✅ |
| 历史日线 | `cmd_history_quotation` | ✅ |
| 基础数据 | `basic_data_service` | ✅ |
| 日期序列 | `date_sequence` | ✅ |
| 自然语言选股（历史日期快照） | `smart_stock_picking` | ✅ |
| 区间资金流（月/周合计） | `smart_stock_picking` | ✅ |
| 申万行业指数历史 | 801010 等 | ❌ 试用返回空/None |
| 概念成员 PIT 历史 | 日期查询 | ❌ 仍返回当前概念 |
| 日频主力资金流（逐日快照） | `smart_stock_picking` | ✅ **2016-01-04 ~ 今，2,622 个交易日、14,632,144 行、5,581 只**，零缺口 |
| 5 分钟历史 | `high_frequency` | ⚠️ 试用仅近 1 年（2019 及更早返回 -4309） |

## 3. 补资金流（全量已完成）

**现状（2026-10-23 实测）**：日频主力资金流 `2016-01-04 ~ 2026-10-22` 已连续覆盖，
**2,622 个交易日 / 14,632,144 行 / 5,581 只标的 / 0 缺口**（`2019-10-17` 首次返回 no data，
重试一次即成功）。月度 `2016-2024`：108 个月、413,968 行。

```bash
# 日频全量（可断点续跑：已存在的日期自动跳过；1 个交易日 ≈ 1.3 秒）
.venv/bin/python scripts/fetch_ifind_moneyflow_history.py --start 2016-01-01 --end 2026-10-22

# 特征（0.5 分钟）：按日归一 + z/rank + 5d/20d 滚动（gap>5 天分段，避免跨缺口泄漏）
.venv/bin/python scripts/build_ifind_moneyflow_daily_features.py

# 月度（2016-2024，108 个月，约 9 分钟）
.venv/bin/python scripts/fetch_ifind_moneyflow_monthly.py --start 2016-01-01 --end 2024-12-31
.venv/bin/python scripts/build_ifind_moneyflow_monthly_features.py

# 每日单日快照（生产自动化里已接，非阻塞）
.venv/bin/python scripts/fetch_ifind_moneyflow_daily_snapshot.py --date 2026-10-22
.venv/bin/python scripts/build_ifind_moneyflow_daily_features.py

# 元数据集重拼（16 秒）
.venv/bin/python scripts/build_meta_dataset.py --start 2016-01-01 --end 2026-10-22
```

校验口径（别再只看 `notna().mean()`）：

```python
# iFinD 快照包含未上市/已退市代码，ratio 在"没有日线"的行上必然为空
has_bar = feats[feats.amount.notna()]
assert has_bar.mf_ifind_daily_ratio.notna().mean() > 0.98   # 实测 99.99%
```

写入位置：

```text
assets/data/raw/ifind/market=CN/moneyflow/year=YYYY/part.parquet   # 原始快照（按年分区 + 去重）
assets/data/derived/cn_ifind_moneyflow_daily_features.parquet      # mf_ifind_* 特征
assets/data/derived/cn_meta_dataset.parquet                        # 已 merge，2016-2026 覆盖率 ~99.9%
```

## 4. 注意

- iFinD 网关对 LibreSSL 默认握手不稳定；客户端使用 `curl --tls-max 1.2` + 重试。
- `smart_stock_picking` 的日期查询对「资金流」是历史快照；对「所属概念」仍返回当前概念，不能当 PIT。
- iFinD 资金流口径与本地多源 moneyflow 不同，字段单独加 `mf_ifind_*` 前缀，不覆盖 `cn_moneyflow_features.parquet`。
- 月度数据只到 2024-12；元数据集对超过 45 天的月度值不再前向填充。
- 日频 raw 会随时间累积重复快照（同一日期重跑）；`build_ifind_moneyflow_daily_features.py` 按
  `(trade_date, stock_code)` 去重，`fetch_ifind_moneyflow_history.py` 写入时也去重，可安全重跑。
- 每日自动化里的 `ifind_moneyflow` 步骤只做研究/交叉核对，**本地多源 moneyflow 仍是生产口径**。

## 5. 分时（intraday）说明

iFinD 试用对 5min 只能给近 1 年（2019 及更早 `-4309`），**历史分时不能靠 iFinD 补齐**。
分时源的实测对比、每日自愈链路、深度回补任务见 [`docs/runbooks/intraday-history.md`](intraday-history.md)。
