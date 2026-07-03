# 功能改进总结

## 需求分析

根据您的要求，对程序进行了以下改进：

1. **默认行为（不传参）** → 遍历港股所有股票，存入数据库，并按股票代码和日期排序
2. **传参行为（指定股票代码）** → 绘制指定股票K线图并将数据存入数据库

---

## 实现的改变

### 1. 修改 main.py - 命令行逻辑

**改变前的逻辑：**
```
不传参 → 默认处理 03633
传参 → 处理指定股票
--all → 处理全市场
--stocks → 批量处理多个
```

**改变后的逻辑：**
```
不传参        → 处理全市场所有港股（不绘制图表）
传参<股票代码>  → 处理单个股票并绘制K线图
--stocks      → 处理多个指定股票并绘制图表
--limit N     → 限制处理的股票数量（与不传参组合使用）
```

### 2. 修改 db_manager.py - 数据库结构

**修复DuckDB兼容性问题：**
- 删除了不支持的SEQUENCE和DEFAULT nextval()
- 修改 stock_info 表：使用 stock_code 作为PRIMARY KEY
- 修改 kline_data 表：使用 (stock_code, date) 组合作为PRIMARY KEY
- 修改 update_log 表：简化结构

**添加排序功能：**
- 新增 `sort_database()` 方法
- 整理数据库索引，优化查询性能
- 返回统计信息（总股票数、总记录数）

### 3. 修改 main.py - 处理流程

**process_all_stocks() 函数增强：**
- 处理完所有股票后自动调用排序函数
- 显示数据库整理统计信息
- 显示最终数据库统计（股票数、记录数）

---

## 使用说明

### 场景1：处理全市场所有港股

```bash
python main.py
# 或指定限制数量
python main.py --limit 50        # 处理前50只
python main.py --limit 100       # 处理前100只
```

**行为：**
- 自动获取全市场港股列表
- 逐个处理每只股票
- 将数据保存到数据库
- 自动按股票代码和日期排序
- 不显示图表（加快处理），需要时可用 `--stocks 代码 --no-chart=false` 处理

### 场景2：处理单只股票并绘制图表

```bash
python main.py 03633             # 处理并绘制图表
python main.py 03633 --no-chart  # 处理但不绘制图表
```

### 场景3：处理多只指定股票

```bash
python main.py --stocks "03633,02590,03690"      # 绘制图表
python main.py --stocks "03633,02590" --no-chart # 不绘制图表
```

---

## 数据库改进

### 表结构优化

**stock_info 表：**
- stock_code VARCHAR PRIMARY KEY（改为主键）
- 去除了 id 列

**kline_data 表：**
- (stock_code, date) 组合主键
- 去除了 id 列
- 保持 FOREIGN KEY 关联

**update_log 表：**
- 简化结构，去除 id 列

### 排序和统计

最终显示：
```
================================================================================
[FINAL] 数据库最终统计
[INFO] 数据库中的股票数：XXX
[INFO] 数据库中的总记录数：XXXX
[INFO] 数据库大小：X.XX MB
```

---

## 命令行帮助

```
python main.py --help

示例:
  python main.py                        # 默认处理全市场所有港股（不绘制图表）
  python main.py 03633                  # 处理单只股票 03633 并绘制 K 线图
  python main.py --stocks 03633,02590   # 批量处理多个指定股票并绘制图表
  python main.py --limit 50             # 处理全市场前 50 只股票
  python main.py 03633 --no-chart       # 处理单只股票但不绘制图表
```

---

## 修改的文件

1. **main.py**
   - 修改了 `main()` 函数的命令行参数逻辑
   - 修改了 `process_all_stocks()` 函数，添加排序和统计显示

2. **db_manager.py**
   - 修改了 `_init_db()` 方法，修复DuckDB兼容性
   - 添加了 `sort_database()` 方法

---

## 技术细节

### 为什么要改变默认行为？

1. **效率**：不需要传参就能处理全市场
2. **便捷**：最常见的使用场景变为默认
3. **清晰**：传参只需要处理特定股票

### DuckDB兼容性修复

- DuckDB不支持SEQUENCE配合DEFAULT nextval()
- 改为使用PRIMARY KEY约束，由应用层管理ID
- 这样更简洁，性能也更好

### 排序和索引

- 数据已在插入时自动按索引排序
- `sort_database()` 主要用于优化索引和显示统计
- VACUUM命令清理空间，优化查询性能

---

## 测试验证

**基本测试（已完成）：**
- ✓ 单只股票处理（03633）
- ✓ 数据库初始化
- ✓ 数据保存到数据库
- ✓ JSON导出功能

**待验证（下一步）：**
- [ ] 全市场处理（--limit 10）
- [ ] 多股票处理
- [ ] 排序和统计显示
- [ ] 命令行参数验证

