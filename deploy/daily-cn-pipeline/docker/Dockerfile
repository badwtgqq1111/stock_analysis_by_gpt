# CN 选股流水线镜像（Linux 容器；CPU 推理）
#
# 关键取舍：容器里**没有** Apple GPU/NPU 加速（Metal / MPS / ANE 只能在 macOS 原生进程里用），
# 因此 transformer 训练/推理会退回 CPU。本机跑请用原生 venv + launchd；
# 容器适合云端（阿里云/腾讯云 ECS）或需要环境可复现的批量回放。
FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/root/.local/bin:${PATH}" \
    DAILY_EARLIEST_HOUR=16

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git tzdata libgomp1 build-essential \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /app
# 依赖层单独缓存：锁文件不变时不重装
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen

# 数据与产物目录（用 volume 挂载持久盘；镜像内不烘焙数据）
VOLUME ["/app/assets", "/app/output", "/app/config"]
ENTRYPOINT ["bash", "scripts/run_daily_production.sh"]
