# 快速开始指南

## 5分钟快速启动

### 1. 检查依赖
```bash
pip install -r requirements.txt
```

### 2. 运行程序
```bash
python main.py
```

### 3. 查看输出
输出文件在 `output/` 目录：
- `hk_stock_03633_*.json` - 股票数据
- `kline_chart_03633_*.png` - K线图表

## 常见任务

### 获取不同股票的数据
编辑 `main.py`：
```python
stock_code = '00700'  # 腾讯
```

### 改变输出位置
编辑 `main.py`：
```python
output_dir = 'C:/data/charts'
```

### 获取更多数据细节
查看 `output/` 目录中的 JSON 文件获取完整数据。

## 理解输出

### JSON 文件结构
```json
{
  "stock_code": "03633",
  "record_count": 1000,
  "date_range": "2022-02-11 to 2026-03-05",
  "update_time": "2026-03-06 01:38:33",
  "data": [
    {
      "date": "2026-03-05",
      "open": 2.93,
      "high": 2.98,
      "low": 2.90,
      "close": 2.94,
      "volume": 68543000
    },
    ...
  ]
}
```

### PNG 图表
- **红色蜡烛**: 收盘价 > 开盘价 (上升)
- **绿色蜡烛**: 收盘价 < 开盘价 (下跌)
- **上影线**: 最高价
- **下影线**: 最低价
- **中间矩形**: 开盘到收盘的价格区间

## 文件说明

| 文件 | 用途 |
|------|------|
| `main.py` | 程序入口 |
| `data_fetcher.py` | 数据获取 |
| `data_display.py` | 数据展示 |
| `data_saver.py` | 数据保存 |
| `chart_plotter.py` | 图表绘制 |
| `utils.py` | 工具函数 |
| `README.md` | 完整文档 |
| `PROJECT_SUMMARY.md` | 项目总结 |

## 故障排除

### 网络错误
- 检查网络连接
- 尝试使用 VPN
- 等待腾讯 API 服务恢复

### 中文显示乱码
- 在 Windows 中运行，系统会自动处理编码
- 或修改 console 编码为 UTF-8

### 找不到 matplotlib
```bash
pip install matplotlib
```

## 进阶用法

### 修改图表样式
编辑 `chart_plotter.py` 中的 `plot()` 方法：
- 改变颜色方案
- 调整图表大小
- 添加更多指标线

### 添加新的数据导出格式
在 `data_saver.py` 中添加新方法：
```python
def save_csv(self, data, stock_code, output_dir):
    # CSV 导出逻辑
    pass
```

### 集成到其他项目
```python
from data_fetcher import StockInfoFetcher, HistoryDataFetcher
from data_display import StockInfoDisplay

# 在你的代码中使用
fetcher = StockInfoFetcher('03633')
info = fetcher.fetch()
StockInfoDisplay.display(info)
```

## 更多帮助

- 详细文档：见 [README.md](README.md)
- 项目信息：见 [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md)
- 代码示例：查看各模块源代码中的注释

## 反馈与改进

如遇到问题或有功能建议，欢迎提出！

---
**最后更新**: 2026-03-06
