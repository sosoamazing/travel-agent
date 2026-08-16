"""观测持久化层：聚合平均模型（节点 × Agent/模型 × 工具）。

表结构：
- obs_tasks         任务父表（每任务一行，含 version 与汇总指标）
- obs_node_metrics  节点聚合（每 task×node 一行：平均耗时 / 调用次数 / 状态）
- obs_llm_metrics   LLM 聚合（每 task×node×agent×model 一行：平均 token / 平均耗时）
- obs_mcp_metrics   MCP 聚合（每 task×node×server×tool 一行：平均耗时 / 重试）

写入策略：
- start_task 先插 running 占位行，end_task 时 UPDATE 补全汇总指标。
- LLM / MCP / 节点在任务执行期间由 _observability 内存累加，end_task 时批量落库。

连接管理：
- 每次操作从 psycopg3 异步连接池 async_db_connection() 获取，上下文退出自动提交/归还。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, cast

from db import async_db_connection


class ObsStorage:
    """观测数据 PostgreSQL 存储（聚合平均模型）。"""

    def __init__(self):
        self._init_lock = asyncio.Lock()
        self._initialized = False

    # ── 初始化 ──────────────────────────────────────────────

    async def _ensure_init(self):
        """懒加载建表/建索引（幂等，asyncio.Lock 守卫）。"""
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            try:
                async with async_db_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("""
                            CREATE TABLE IF NOT EXISTS obs_tasks (
                                task_id            TEXT PRIMARY KEY,
                                version            TEXT NOT NULL DEFAULT '',
                                user_id            TEXT,
                                session_id         TEXT,
                                user_query         TEXT,
                                status             TEXT NOT NULL DEFAULT 'running',
                                error              TEXT,
                                start_ts           DOUBLE PRECISION NOT NULL,
                                end_ts             DOUBLE PRECISION,
                                duration_ms        DOUBLE PRECISION,
                                node_count         INTEGER NOT NULL DEFAULT 0,
                                llm_call_count     INTEGER NOT NULL DEFAULT 0,
                                tool_call_count    INTEGER NOT NULL DEFAULT 0,
                                total_input_tokens INTEGER NOT NULL DEFAULT 0,
                                total_output_tokens INTEGER NOT NULL DEFAULT 0,
                                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                                llm_duration_ms    DOUBLE PRECISION NOT NULL DEFAULT 0,
                                tool_duration_ms   DOUBLE PRECISION NOT NULL DEFAULT 0
                            )
                        """)
                        await cur.execute("""
                            CREATE TABLE IF NOT EXISTS obs_node_metrics (
                                id              SERIAL PRIMARY KEY,
                                task_id         TEXT NOT NULL,
                                node            TEXT NOT NULL,
                                invocations     INTEGER NOT NULL DEFAULT 1,
                                duration_ms_avg DOUBLE PRECISION NOT NULL DEFAULT 0,
                                status          TEXT NOT NULL DEFAULT 'ok',
                                error           TEXT
                            )
                        """)
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_node_task ON obs_node_metrics(task_id)")
                        await cur.execute("""
                            CREATE TABLE IF NOT EXISTS obs_llm_metrics (
                                id                SERIAL PRIMARY KEY,
                                task_id           TEXT NOT NULL,
                                node              TEXT NOT NULL,
                                agent             TEXT,
                                model             TEXT,
                                calls             INTEGER NOT NULL DEFAULT 0,
                                input_tokens_avg  DOUBLE PRECISION NOT NULL DEFAULT 0,
                                output_tokens_avg DOUBLE PRECISION NOT NULL DEFAULT 0,
                                cached_tokens_avg DOUBLE PRECISION NOT NULL DEFAULT 0,
                                duration_ms_avg   DOUBLE PRECISION NOT NULL DEFAULT 0,
                                error_count       INTEGER NOT NULL DEFAULT 0,
                                error             TEXT
                            )
                        """)
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_llm_task ON obs_llm_metrics(task_id)")
                        await cur.execute("""
                            CREATE TABLE IF NOT EXISTS obs_mcp_metrics (
                                id              SERIAL PRIMARY KEY,
                                task_id         TEXT NOT NULL,
                                node            TEXT NOT NULL,
                                server          TEXT,
                                tool            TEXT,
                                calls           INTEGER NOT NULL DEFAULT 0,
                                duration_ms_avg DOUBLE PRECISION NOT NULL DEFAULT 0,
                                error_count     INTEGER NOT NULL DEFAULT 0,
                                retries         INTEGER NOT NULL DEFAULT 0,
                                error           TEXT
                            )
                        """)
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_mcp_task ON obs_mcp_metrics(task_id)")
                print("✅ 观测数据库初始化完成")
            except Exception as e:
                print(f"❌ 观测数据库初始化失败: {e}")
                import traceback
                traceback.print_exc()
                raise
            self._initialized = True

    # ── 写 ──────────────────────────────────────────────────

    async def start_task(self, task_id: str, version: str, user_id: str,
                         session_id: str, user_query: str) -> None:
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO obs_tasks
                       (task_id, version, user_id, session_id, user_query, status, start_ts)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (task_id, version, user_id, session_id, user_query, "running", time.time()),
                )

    async def end_task(self, task_id: str, status: str, error: Optional[str],
                       summary: Dict[str, Any]) -> None:
        await self._ensure_init()
        # SELECT start_ts + UPDATE 汇总：两条须在同一事务块内
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                end_ts = time.time()
                await cur.execute("SELECT start_ts FROM obs_tasks WHERE task_id = %s", (task_id,))
                row = await cur.fetchone()
                duration_ms = None
                if row:
                    duration_ms = round((end_ts - row["start_ts"]) * 1000, 2)
                await cur.execute(
                    """UPDATE obs_tasks SET
                         status=%s, error=%s, end_ts=%s, duration_ms=%s,
                         node_count=%s, llm_call_count=%s, tool_call_count=%s,
                         total_input_tokens=%s, total_output_tokens=%s, cached_input_tokens=%s,
                         llm_duration_ms=%s, tool_duration_ms=%s
                       WHERE task_id=%s""",
                    (status, error, end_ts, duration_ms,
                     int(summary.get("node_count", 0)),
                     int(summary.get("llm_call_count", 0)),
                     int(summary.get("tool_call_count", 0)),
                     int(summary.get("total_input_tokens", 0)),
                     int(summary.get("total_output_tokens", 0)),
                     int(summary.get("cached_input_tokens", 0)),
                     float(summary.get("llm_duration_ms", 0)),
                     float(summary.get("tool_duration_ms", 0)),
                     task_id),
                )

    async def insert_node_metrics(self, task_id: str, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                for r in rows:
                    await cur.execute(
                        """INSERT INTO obs_node_metrics
                           (task_id, node, invocations, duration_ms_avg, status, error)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (task_id, r["node"], int(r.get("invocations", 1)),
                         float(r.get("duration_ms_avg", 0)), r.get("status", "ok"), r.get("error")),
                    )

    async def insert_llm_metrics(self, task_id: str, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                for r in rows:
                    await cur.execute(
                        """INSERT INTO obs_llm_metrics
                           (task_id, node, agent, model, calls, input_tokens_avg,
                            output_tokens_avg, cached_tokens_avg, duration_ms_avg, error_count, error)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (task_id, r["node"], r.get("agent"), r.get("model"),
                         int(r.get("calls", 0)),
                         float(r.get("input_tokens_avg", 0)),
                         float(r.get("output_tokens_avg", 0)),
                         float(r.get("cached_tokens_avg", 0)),
                         float(r.get("duration_ms_avg", 0)),
                         int(r.get("error_count", 0)),
                         r.get("error")),
                    )

    async def insert_mcp_metrics(self, task_id: str, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                for r in rows:
                    await cur.execute(
                        """INSERT INTO obs_mcp_metrics
                           (task_id, node, server, tool, calls, duration_ms_avg,
                            error_count, retries, error)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (task_id, r["node"], r.get("server"), r.get("tool"),
                         int(r.get("calls", 0)),
                         float(r.get("duration_ms_avg", 0)),
                         int(r.get("error_count", 0)),
                         int(r.get("retries", 0)),
                         r.get("error")),
                    )

    # ── 读 ──────────────────────────────────────────────────

    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT * FROM obs_tasks WHERE task_id = %s", (task_id,))
                row = await cur.fetchone()
        return dict(row) if row else None

    async def get_node_metrics(self, task_id: str) -> List[Dict[str, Any]]:
        return await self._read_all("obs_node_metrics", task_id)

    async def get_llm_metrics(self, task_id: str) -> List[Dict[str, Any]]:
        return await self._read_all("obs_llm_metrics", task_id)

    async def get_mcp_metrics(self, task_id: str) -> List[Dict[str, Any]]:
        return await self._read_all("obs_mcp_metrics", task_id)

    async def _read_all(self, table: str, task_id: str) -> List[Dict[str, Any]]:
        # table 名由内部硬编码传入，非外部输入，无注入风险
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(cast(Any, f"SELECT * FROM {table} WHERE task_id = %s"), (task_id,))
                rows = [dict(r) for r in await cur.fetchall()]
        return rows


_obs_storage: Optional[ObsStorage] = None


def get_obs_storage() -> ObsStorage:
    """获取全局观测存储单例。"""
    global _obs_storage
    if _obs_storage is None:
        _obs_storage = ObsStorage()
    return _obs_storage
