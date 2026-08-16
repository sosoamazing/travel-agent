"""LangGraph 断点续跑：PostgreSQL checkpointer 生命周期管理。

使用异步版 `langgraph.checkpoint.postgres.aio.AsyncPostgresSaver`（基于 psycopg3 异步），
与 FastAPI 的 async 执行一致（service 用 astream_events 跑图）。

⚠️ Windows 事件循环要求：
    psycopg3 的异步实现依赖 SelectorEventLoop（需要 add_reader），而 Python 在
    Windows 上默认使用 ProactorEventLoop（不支持 add_reader），会报：
    "Psycopg cannot use the 'ProactorEventLoop' to run in async mode"。
    因此 server.py 在顶层把事件循环策略切换为 SelectorEventLoop（仅 Windows）。

thread_id 约定：
    - 以业务 task_id 作为 thread_id，每个任务一条独立 checkpoint 流。
    - 同一 thread_id 以 input=None 再次 invoke 时，LangGraph 会跳过已成功节点、
      从最近一次 checkpoint 继续执行，实现崩溃后的断点续跑。

依赖（requirements.txt 已声明）：
    - langgraph-checkpoint-postgres
    - psycopg[binary,pool]
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DATABASE

logger = logging.getLogger(__name__)

# checkpoint 专用连接池大小（.env 可覆盖，独立于业务 psycopg3 异步连接池）
CP_POOL_MINCONN = int(os.getenv("CP_POOL_MINCONN", "2"))
CP_POOL_MAXCONN = int(os.getenv("CP_POOL_MAXCONN", "10"))

_pool = None  # 长生命周期连接池，进程退出前 close


def build_checkpoint_dsn() -> str:
    """构造 psycopg3 兼容的 libpq DSN。"""
    return f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"


async def create_checkpointer():
    """创建并初始化 AsyncPostgresSaver（幂等建表 checkpoints/checkpoint_blobs/checkpoint_writes）。

    返回 checkpointer 实例；依赖缺失或连接失败会抛异常，由调用方决定是否降级。
    """
    global _pool
    # 延迟导入：只有真正启用 checkpoint 时才需要这两个依赖
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    _pool = AsyncConnectionPool(
        conninfo=build_checkpoint_dsn(),
        min_size=CP_POOL_MINCONN,
        max_size=CP_POOL_MAXCONN,
        kwargs={"autocommit": True},
        open=False,
    )
    await _pool.open()
    checkpointer = AsyncPostgresSaver(_pool)
    await checkpointer.setup()
    logger.info(
        f"✅ [checkpoint] AsyncPostgresSaver 就绪（pool {CP_POOL_MINCONN}~{CP_POOL_MAXCONN}, "
        f"db={PG_HOST}:{PG_PORT}/{PG_DATABASE}）"
    )
    return checkpointer


async def close_checkpointer():
    """关闭 checkpoint 连接池（进程退出前调用，幂等）。"""
    global _pool
    if _pool is None:
        return
    try:
        await _pool.close()
        logger.info("🔌 [checkpoint] 连接池已关闭")
    except Exception as e:
        logger.warning(f"关闭 checkpoint 连接池失败: {e}")
    finally:
        _pool = None
