"""PostgreSQL 连接工具（psycopg3 异步单模）。

异步层（psycopg3 AsyncConnectionPool + dict_row）：
    - 全仓唯一 DB 访问路径：async_db_connection()（dict_row，row["col"] 可用，%s 占位符可用）
    - min_size/max_size 通过 .env 配置（默认 2/10）
    - 供全部业务层（memory / chat / obs / auth / typecode）使用

健康检查：
    - check_pg3_db_connectivity() 做一次真实 SELECT 1 探活（供 /health 端点）
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, Dict

from config.settings import (
    PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DATABASE,
)

# ═══════════════════════════════════════════════════════════
# 配置（.env 可覆盖）
# ═══════════════════════════════════════════════════════════
import os
PG_POOL_MINCONN = int(os.getenv("PG_POOL_MINCONN", "2"))
PG_POOL_MAXCONN = int(os.getenv("PG_POOL_MAXCONN", "10"))

# psycopg3 异步连接池单例（懒加载，asyncio.Lock 守卫）
_async_pg3_pool = None
_async_pg3_pool_lock = None  # asyncio.Lock，首次使用时懒创建（须在事件循环内）


async def _ensure_async_pool():
    """懒加载 psycopg3 AsyncConnectionPool（asyncio.Lock 守卫，须在事件循环内调用）。

    kwargs={"row_factory": dict_row} 让结果行支持 row["col"] 访问。
    """
    global _async_pg3_pool, _async_pg3_pool_lock
    if _async_pg3_pool is not None:
        return _async_pg3_pool
    import asyncio
    if _async_pg3_pool_lock is None:
        _async_pg3_pool_lock = asyncio.Lock()
    async with _async_pg3_pool_lock:
        if _async_pg3_pool is not None:
            return _async_pg3_pool
        from psycopg_pool import AsyncConnectionPool
        from psycopg.rows import dict_row

        _async_pg3_pool = AsyncConnectionPool(
            conninfo=f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}",
            min_size=PG_POOL_MINCONN,
            max_size=PG_POOL_MAXCONN,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        await _async_pg3_pool.open()
        print(
            f"✅ [DB] psycopg3 异步连接池已初始化：min_size={PG_POOL_MINCONN}, "
            f"max_size={PG_POOL_MAXCONN}, db={PG_HOST}:{PG_PORT}/{PG_DATABASE}"
        )
        return _async_pg3_pool


@asynccontextmanager
async def async_db_connection():
    """psycopg3 异步连接上下文管理器（dict_row，row["col"] 可用）。

    语义：正常退出提交，异常回滚，finally 归还连接池（避免 aborted 连接泄漏回池）。

    ⚠️ 实现说明：psycopg_pool 的 `connection()` 是 @asynccontextmanager（不可 await），
    且对池取出的连接调用 `conn.close()` 会真正关闭 socket 而不是归还池。
    因此这里用底层 API：`pool.getconn()` 取连 + `pool.putconn(conn)` 归还。

    用法：
        async with async_db_connection() as conn:
            cur = await conn.execute("SELECT %s AS tid", (tid,))
            row = await cur.fetchone()
            row["tid"]
    """
    pool = await _ensure_async_pool()
    conn = await pool.getconn()
    try:
        yield conn
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await pool.putconn(conn)  # 归还池（pool 会检测 INERROR/断开连接并自动替换）


async def check_pg3_db_connectivity(timeout_sec: float = 3.0) -> Dict[str, Any]:
    """真实探活：SELECT 1，返回 {ok, latency_ms, error}。"""
    import asyncio
    t0 = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_sec):
            async with async_db_connection() as conn:
                cur = await conn.execute("SELECT 1 AS ping")
                row = await cur.fetchone()
                if not row or row["ping"] != 1:
                    raise RuntimeError("psycopg3 SELECT 1 返回异常")
    except Exception as e:
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
            "error": str(e),
        }
    return {
        "ok": True,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
    }


async def shutdown_async_pool():
    """显式关闭 psycopg3 异步连接池（进程退出前调用）。幂等。

    ⚠️ psycopg_pool 必须在其被打开的同一事件循环内关闭
    （跨 loop close 会抛 CancelledError），因此本函数为 async，必须在事件循环内 await 调用。
    """
    global _async_pg3_pool, _async_pg3_pool_lock
    if _async_pg3_pool is None:
        return
    try:
        await _async_pg3_pool.close()
        print("🔌 [DB] psycopg3 异步连接池已关闭")
    except Exception:
        pass
    _async_pg3_pool = None
    _async_pg3_pool_lock = None


__all__ = [
    "async_db_connection",
    "check_pg3_db_connectivity",
    "shutdown_async_pool",
]
