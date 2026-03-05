# 港股数据获取与分析工具

## 项目概述
本项目是一个模块化的港股数据获取、处理和分析工具，使用腾讯财经 API 获取港股历史数据，
并生成专业的 K 线蜡烛图。

## 项目结构

### 核心模块

```
stock/
├── main.py                  # 主程序入口，协调各个模块
├── data_fetcher.py         # 数据获取模块
│   ├── StockInfoFetcher    # 获取股票基本信息
│   └── HistoryDataFetcher  # 获取历史K线数据
├── data_display.py         # 数据显示模块
│   ├── StockInfoDisplay    # 显示基本信息
│   └── HistoryDataDisplay  # 显示历史数据
├── data_saver.py           # 数据保存模块
│   └── DataSaver           # 保存数据到JSON文件
├── chart_plotter.py        # 图表绘制模块
│   └── KLineChartPlotter   # 绘制K线蜡烛图
├── utils.py                # 工具函数模块
│   ├── setup_output_dir()  # 设置输出目录
│   ├── print_section()     # 打印章节标题
│   └── print_summary()     # 打印处理概要
├── requirements.txt        # 依赖包列表
└── README.md               # 项目文档
```

## 模块说明

### 1. data_fetcher.py - 数据获取模块
**职责**：从腾讯财经 API 获取股票数据

**主要类**：
- `StockInfoFetcher`: 获取实时股票信息（价格、成交量等）
- `HistoryDataFetcher`: 获取历史K线数据（1000条记录）

**使用示例**：
```python
from data_fetcher import StockInfoFetcher, HistoryDataFetcher

# 获取基本信息
info_fetcher = StockInfoFetcher('03633')
info = info_fetcher.fetch()

# 获取历史数据
data_fetcher = HistoryDataFetcher('03633')
df = data_fetcher.fetch()
```

### 2. data_display.py - 数据显示模块
**职责**：格式化和显示各类股票数据

**主要类**：
- `StockInfoDisplay`: 显示股票基本信息
- `HistoryDataDisplay`: 显示历史数据统计

**特点**：
- 清晰的表格格式
- 自动对齐和美化
- 支持中文显示

### 3. data_saver.py - 数据保存模块
**职责**：将获取的数据保存到本地文件

**主要类**：
- `DataSaver`: 保存数据为JSON格式

**保存内容**：
- 股票代码
- 记录数量
- 日期范围
- 时间戳
- 完整的K线数据

### 4. chart_plotter.py - 图表绘制模块
**职责**：生成专业的K线蜡烛图

**主要类**：
- `KLineChartPlotter`: 绘制K线图

**图表特点**：
- 支持上下影线表示最高/最低价
- 红色蜡烛表示上升（收盘 > 开盘）
- 绿色蜡烛表示下跌（收盘 < 开盘）
- 自动缩放和标签
- 添加图例和网格

### 5. utils.py - 工具模块
**职责**：提供通用工具函数

**主要函数**：
- `setup_output_dir()`: 清理历史数据并创建输出目录
- `print_section()`: 打印格式化的章节标题
- `print_summary()`: 打印处理完成概要

### 6. main.py - 主程序
**职责**：按顺序调用各模块完成完整流程

**执行流程**：
1. 初始化 - 清理旧数据，创建输出目录
2. 下载数据 - 获取基本信息和历史数据
3. 显示数据 - 格式化输出数据统计
4. 保存数据 - 保存为JSON格式
5. 绘制图表 - 生成K线蜡烛图
6. 输出总结 - 显示完成信息

## 使用方法

### 基础使用
```bash
python main.py
```

### 输出目录
所有生成的文件保存在 `./output/` 目录下：
- `hk_stock_03633_YYYYMMDD_HHMMSS.json` - 股票数据
- `kline_chart_03633_YYYYMMDD_HHMMSS.png` - K线图表

## 依赖包
- requests >= 2.31.0      # HTTP请求
- pandas >= 2.0.0         # 数据处理
- matplotlib >= 3.5.0     # 图表绘制

## 数据来源
- API: 腾讯财经(腾讯行情)
- 更新频率: 实时更新
- 历史数据: 最多1000条K线记录

## 功能特点

1. **模块化设计**
   - 各模块职责清晰
   - 低耦合高内聚
   - 易于维护和扩展

2. **健壮的错误处理**
   - 网络错误捕获
   - 数据验证
   - 编码兼容性

3. **清晰的输出信息**
   - 分阶段显示处理信息
   - [INFO] [OK] [ERROR] 标签区分
   - 完成后显示总结

4. **专业的图表输出**
   - 高分辨率PNG图表
   - 自动布局和标签
   - 支持中文显示

## 扩展说明

### 添加新的数据显示方式
在 `data_display.py` 中添加新的类：
```python
class CustomDisplay:
    @staticmethod
    def display(data):
        # 自定义显示逻辑
        pass
```

### 添加新的图表类型
在 `chart_plotter.py` 中添加新的类：
```python
class VolumeChartPlotter:
    def plot(self, hist, output_dir):
        # 绘制成交量图表
        pass
```

### 添加数据导出格式
在 `data_saver.py` 中添加新的方法：
```python
class DataSaver:
    @staticmethod
    def save_csv(data, output_dir):
        # 保存为CSV格式
        pass
```

## 注意事项

1. 首次运行时会自动创建 `output` 目录
2. 每次运行会清理之前的历史数据
3. 需要网络连接才能获取数据
4. 图表生成需要安装 matplotlib

## 版本信息
- Python 3.7+
- 最后更新: 2026-03-06
