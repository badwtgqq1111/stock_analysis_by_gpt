# Docker 部署与"macOS Docker vs Linux Docker"的关键差异

## 什么时候用容器

| 场景 | 建议 | 原因 |
|---|---|---|
| Mac 本地每日生产 | **原生 venv + launchd** | 只有原生进程能用 Apple GPU（MPS）；容器里没有 |
| 云端每日生产（阿里云/腾讯云 ECS） | **容器** 或原生 venv + systemd | 环境可复现、依赖一次构建多处运行 |
| 批量回放 / 多账户并行（rich + small） | **容器** | 一次性任务、互不污染，可 `docker compose run` 多份 |
| 需要 GPU 训练（NVIDIA） | Linux + `--gpus all` + NVIDIA Container Toolkit | macOS 上不可用 |

## macOS Docker 与 Linux Docker 的差异（硬件加速重点）

| 维度 | macOS 上的 Docker | Linux 上的 Docker |
|---|---|---|
| 运行时 | 起一个 Linux 虚拟机（Virtualization.framework / HyperKit） | 直接共享宿主内核 |
| CPU | 虚拟化开销，通常 85-95% 原生 | 接近原生 |
| **Apple GPU（Metal / MPS）** | **不可用**：容器内没有 Metal 驱动，`torch.backends.mps` 为 False | 不适用 |
| **Apple NPU（ANE）** | **不可用**：Core ML / ANE 只在原生 macOS 进程可用 | 不适用 |
| NVIDIA GPU | 不可用（无 passthrough） | 需 NVIDIA Container Toolkit + `--gpus all` |
| 内存/文件 I/O | 受 VM 限制，大文件（OHLCV/特征库）IO 明显更慢 | 接近原生；建议数据放宿主卷或云盘 |
| 时间/时区 | 继承宿主，但容器内仍建议显式 `TZ` | 同上 |

**结论**：本项目里唯一吃 GPU 的是 transformer（`device=mps`）。在 Mac 上用 Docker 会把训练/推理
降级为 CPU（本机实测 10 epoch 训练在 MPS 上约 60s，CPU 会慢数倍）。所以：

- **本机**：`uv run python scripts/run_cn_pipeline.py --stage ...` + `launchd`（全速）；
- **容器**：只用于云端或"同构复现"，且把 `[transformer] device = "cpu"` 显式写死，避免容器里
  静默退回 CPU 却以为在用 MPS。

## 用法

```bash
# 构建
docker compose -f deploy/daily-cn-pipeline/docker/docker-compose.yml build

# 手动跑一次（幂等：当天成功后再跑会跳过，加 --force 强制）
docker compose -f deploy/daily-cn-pipeline/docker/docker-compose.yml run --rm daily-pipeline
docker compose -f deploy/daily-cn-pipeline/docker/docker-compose.yml run --rm daily-pipeline --force
docker compose -f deploy/daily-cn-pipeline/docker/docker-compose.yml run --rm daily-pipeline --dry-run

# 只跑两阶段报告（已有产物时）
docker compose -f deploy/daily-cn-pipeline/docker/docker-compose.yml run --rm \
  --entrypoint "uv run python scripts/render_two_stage_report.py" daily-pipeline
```

云上调度（容器版）：ECS / 轻量应用服务器上用上面的 systemd timer 或 cron 调用 `docker compose run`，
或用云厂商的"定时任务 / 函数计算 + 容器镜像"（阿里云 ECI 定时任务、腾讯云 SCF 容器镜像），
把 `assets/ output/ config/` 挂到 NAS/云盘即可（数据目录不要放在容器层）。

## 数据与状态

- 必须持久化：`assets/`（OHLCV、特征库、模型）、`output/`（选股、报告、paper 账户）、`config/`。
- 幂等标记：`output/pipeline_reports/daily/<date>/production.done`；容器重启/重复触发不会重复下单逻辑。
- 建议把 `assets/` 放在云盘或本地 SSD（百 GB 级；特征库单次重建约 6GB、OHLCV 更大）。
