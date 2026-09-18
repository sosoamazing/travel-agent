# =============================================================================
# travel-agent 应用镜像（多阶段构建）
# -----------------------------------------------------------------------------
# 一个镜像承载三个服务（gateway / backend / monitor）——它们共享同一份代码与依赖，
# 仅入口命令不同，由 docker-compose 的 command 区分，避免维护三份近乎相同的镜像。
#
# 构建（在 travel-agent/travel-agent 目录下执行）：
#   docker build -t travel-agent:latest .
#
# 单独运行某个服务：
#   docker run --rm -p 8000:8000 travel-agent:latest \
#       uvicorn gateway.main:app --host 0.0.0.0 --port 8000
#
# 说明：
# - 代码中的 Windows SelectorEventLoop 策略与 UTF-8 重配置均由 `sys.platform ==
#   "win32"` 守卫，在 Linux 容器内自动跳过，因此可直接用 uvicorn CLI 启动。
# - backend/server.py 的 __main__ 分支硬编码 127.0.0.1:8001（防止外部直连绕过网关
#   鉴权）；容器内需让同网络的 gateway 能访问，故改用 uvicorn CLI 绑定 0.0.0.0，
#   并通过 compose 不发布 8001 端口来保持“不对外暴露”的等价安全约束。
# =============================================================================

# ── 构建阶段：装依赖到独立前缀，避免编译工具链进入运行镜像 ──────────────────
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# psycopg/chromadb 等含 C 扩展的包在无 wheel 时需要编译工具链
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY backend/requirements.lock.txt ./requirements.lock.txt

# 装到 /install 前缀，运行阶段整体拷走（只带产物，不带工具链）
RUN pip install --prefix=/install -r requirements.lock.txt


# ── 运行阶段：仅拷贝依赖产物 + 应用代码 ────────────────────────────────────
FROM python:3.13-slim AS runtime

# PYTHONUNBUFFERED: 日志实时刷出，容器内可被 docker logs 即时看到
# PYTHONDONTWRITEBYTECODE: 不生成 .pyc，保持镜像与挂载卷干净
# PYTHONUTF8/PYTHONIOENCODING: 与 start_services.ps1 对齐，保证 emoji 日志不炸
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PATH=/usr/local/bin:$PATH

# 运行期依赖：curl 供 compose healthcheck 使用
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 拷贝构建阶段安装好的第三方包
COPY --from=builder /install /usr/local

WORKDIR /app

# 应用代码（.dockerignore 已排除 venv / node_modules / dist / logs 等）
COPY backend/ ./backend/
COPY gateway/ ./gateway/
COPY monitor/ ./monitor/

# backend 内部以 `backend` 为包根导入（config / graph / db 等顶层模块），
# 同时 gateway.main / monitor.main 以 /app 为根导入，故两者都要在 PYTHONPATH 中。
ENV PYTHONPATH=/app:/app/backend

# 以非 root 运行，降低容器逃逸风险
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# 默认启动 gateway（compose 中各服务会用 command 覆盖）
EXPOSE 8000
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
