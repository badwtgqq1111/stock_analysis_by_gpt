# Linux（含阿里云 / 腾讯云 ECS）部署

```bash
# 1. 代码与依赖（uv 管理的环境，与 macOS 完全一致）
sudo mkdir -p /opt/cn-quant && cd /opt/cn-quant
git clone <repo> stock_analysis_by_gpt && cd stock_analysis_by_gpt
curl -LsSf https://astral.sh/uv/install.sh | sh &&  uv sync --frozen

# 2. 把数据目录挂到持久盘（OHLCV / 特征 / 模型动辄上百 GB）
#    默认 base_dir=./assets，用软链指到大盘
ln -s /data/cn-quant/assets ./assets

# 3. 安装 systemd 定时任务（把 %i 换成实际用户，例如 root）
sudo cp deploy/daily-cn-pipeline/linux/cn-pipeline.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cn-pipeline.timer
systemctl list-timers cn-pipeline.timer

# 4. 手动补跑 / 看日志
sudo systemctl start cn-pipeline.service
journalctl -u cn-pipeline.service -n 100 --no-pager
ls output/pipeline_reports/daily/
```

cron 等价写法（若不用 systemd）：

```cron
# m h dom mon dow command
30 19 * * 1-5 cd /opt/cn-quant/stock_analysis_by_gpt && TZ=Asia/Shanghai /usr/bin/env bash scripts/run_daily_production.sh >> output/pipeline_reports/daily/cron.log 2>&1
30 20 * * 1-5 cd /opt/cn-quant/stock_analysis_by_gpt && TZ=Asia/Shanghai /usr/bin/env bash scripts/run_daily_production.sh >> output/pipeline_reports/daily/cron.log 2>&1
0  22 * * 1-5 cd /opt/cn-quant/stock_analysis_by_gpt && TZ=Asia/Shanghai /usr/bin/env bash scripts/run_daily_production.sh >> output/pipeline_reports/daily/cron.log 2>&1
@reboot sleep 120 && cd /opt/cn-quant/stock_analysis_by_gpt && /usr/bin/env bash scripts/run_daily_production.sh >> output/pipeline_reports/daily/cron.log 2>&1
```

要点：

- **注册在 `@reboot` / `Persistent=true` 上的开机补跑是安全的**：脚本内部按"今天是否已成功"判定，
  重复触发直接跳过（`output/pipeline_reports/daily/<date>/production.done`）。
- 云上务必 `TZ=Asia/Shanghai`，否则 `date +%H` 的"收盘后"判断会错。
- 交易所日历：脚本按"本地时间 + 幂等标记"判断，节假日会空跑一次（数据源无新交易日 → 阶段自动跳过/无新数据），
  如要更严格可以在脚本里接 `output/regime/cn_market_regime.csv` 的最新交易日做校验。
