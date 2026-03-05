# 批量保存数据功能使用指南

## 概述

本版本新增了强大的批量数据保存和验证功能，支持：
- ✓ 单股票处理并验证数据
- ✓ 多股票批量处理（指定多个股票代码）
- ✓ 自动数据验证和完整性检查
- ✓ 增量更新机制

---

## 快速开始

### 示例1: 处理单个股票并验证
```bash
python main.py 03633 --verify
```

### 示例2: 批量处理多个股票并验证
```bash
python main.py --stocks "03633,02590,03690" --verify --no-chart
```

### 示例3: 快速验证多个股票是否已保存
```bash
python main.py --stocks "03633,02590,03690" --verify
```

---

## 命令行参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `stock_code` | 单只股票代码 | `03633` |
| `--stocks X,Y,Z` | 多个股票（逗号分隔） | `--stocks "03633,02590"` |
| `--all` | 处理全市场 | `--all` |
| `--limit N` | 限制股票数量 | `--all --limit 50` |
| `--verify` | 验证数据保存 | `--verify` |
| `--no-chart` | 跳过图表生成 | `--no-chart` |
| `--output DIR` | 输出目录 | `--output ./output` |
| `--db DIR` | 数据库目录 | `--db ./assets` |

---

## 详细说明

### 单股票处理并验证

```bash
python main.py 03633 --verify
```

输出：
```
[VERIFY] 数据验证报告
[STOCK] 股票代码：03633
[STATUS] [OK] 数据已保存
[DATA] 记录数：1000 条
[DATE] 时间范围：2022-02-11 至 2026-03-05
[INFO] 股票信息：已保存
[SIZE] 数据库大小：6.76 MB
```

### 多股票批量处理

```bash
python main.py --stocks "03633,02590,03690" --verify
```

输出关键部分：
```
[BATCH] 批量处理指定股票
[INFO] 将处理 3 只股票
[INFO] 股票列表：03633, 02590, 03690

[SUMMARY] 批量处理完成
[INFO] 总计：3 只股票
       成功：3 只
       失败：0 只
       成功率：100.0%

[SUMMARY] 批量验证汇总
  总股票数：3 只
  验证成功：3 只
  验证失败：0 只
  总记录数：2163 条
  验证状态：[OK] 全部验证成功

[DETAILS] 各股票详情
  [OK] 03633: 1000 条记录，时间范围: 2022-02-11 至 2026-03-05
  [OK] 02590:  163 条记录，时间范围: 2025-07-09 至 2026-03-05
  [OK] 03690: 1000 条记录，时间范围: 2022-02-14 至 2026-03-05
```

---

## API 接口

### verify_db_data(stock_code)
验证单只股票的数据是否存在数据库中

```python
from data_saver import DataSaver

saver = DataSaver()
result = saver.verify_db_data('03633')
# 返回: {'stock_code': '03633', 'exists': True, 'total_records': 1000, ...}
```

### batch_verify_data(stock_codes)
批量验证多只股票的数据

```python
from data_saver import DataSaver

saver = DataSaver()
stocks = ['03633', '02590', '03690']
result = saver.batch_verify_data(stocks)
# 返回: {'total_stocks': 3, 'verified_stocks': 3, 'failed_stocks': 0, ...}
```

### print_verification_report(verification_result)
打印验证报告

```python
saver.print_verification_report(result)
```

---

## 核心功能说明

### 1. 数据保存流程

每次处理股票时，系统会执行以下步骤：

1. **检查数据库** - 检查该股票是否已有数据
2. **增量更新检测** - 如有数据，获取最新日期
3. **获取数据** - 从腾讯财经 API 获取数据
4. **保存到数据库** - 使用 DuckDB 高性能批量保存
5. **导出JSON** - 同时生成 JSON 文件备份
6. **验证数据** - 如指定 --verify，验证保存结果

### 2. 增量更新机制

- 首次处理：全量下载并保存
- 后续处理：自动检测最新日期，只下载新数据
- 重复日期：自动更新（新数据覆盖旧数据）
- 操作日志：每次更新都记录在 `update_log` 表

### 3. 验证流程

验证过程检查：
- ✓ 数据是否存在数据库
- ✓ 记录总数
- ✓ 时间范围（最早日期和最新日期）
- ✓ 股票基本信息是否保存
- ✓ 数据库文件状态

---

## 实际使用场景

### 场景1: 监控关键股票
```bash
# 定期更新和验证关键股票
python main.py --stocks "00001,00700,00883,01398,03690" --verify --no-chart
```

### 场景2: 快速批量更新
```bash
# 处理市场前50只股票，跳过图表生成以加快速度
python main.py --all --limit 50 --no-chart
```

### 场景3: 单股票深度分析
```bash
# 处理单个股票并保存图表和详细数据
python main.py 03633 --verify
```

### 场景4: 数据完整性检查
```bash
# 只验证不重新下载
python main.py --stocks "03633,02590,03690" --verify --no-chart
```

---

## 常见问题

**Q: 如何快速验证多个股票的数据？**
A: 使用 `--verify --no-chart` 参数组合，跳过图表生成，只进行数据保存和验证。

**Q: 增量更新是如何工作的？**
A: 系统自动检测数据库中最新的日期，只下载之后的新数据。重复日期的数据会被更新。

**Q: 批量处理失败了怎么办？**
A: 可以从失败的股票开始重新处理，系统会自动进行增量更新，不会重复下载已有数据。

**Q: 数据验证报告中的"验证失败"是什么意思？**
A: 表示该股票在数据库中没有数据，或者数据为空。

**Q: 可以自定义数据库位置吗？**
A: 可以，使用 `--db` 参数指定数据库目录，例如 `--db ./my_database`。

---

## 性能提示

- 使用 `--no-chart` 可以加快处理速度（跳过 K 线图生成）
- 批量处理建议每次不超过 50-100 只股票
- 对于大量股票的定期更新，建议使用脚本定时执行

---

## 技术细节

- **数据库**: DuckDB（列式存储，OLAP 优化）
- **批量操作**: 使用临时表和 UPSERT 实现高效更新
- **增量更新**: 基于 UNIQUE 约束和日期检测
- **验证机制**: 递归检查数据完整性和一致性

---

## 修改列表

### data_saver.py
- ✓ 添加 `verify_db_data()` 方法
- ✓ 添加 `batch_verify_data()` 方法
- ✓ 添加 `print_verification_report()` 方法

### main.py
- ✓ 添加 `process_multiple_stocks()` 函数
- ✓ 添加 `--stocks` 参数支持
- ✓ 添加 `--verify` 参数支持
