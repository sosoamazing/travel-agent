"""观测持久化层：通用 span 树模型（节点 / LLM / MCP 逐调用 span）。

表结构（统一模式）：
- obs_tasks        任务父表（task_id=uuid 主键，任务一开始即插入占位，end 时 UPDATE 补全）
- obs_node_spans   节点 span（span_id=uuid 主键，parent_id 引用父 span_id，根级 NULL）
- obs_llm_spans    LLM span（span_id=uuid 主键，parent_id 引用父 span_id）
- obs_mcp_spans    MCP span（span_id=uuid 主键，parent_id 引用父 span_id）

统一「开始占位 + 结束更新」模式：
- 调用开始 INSERT 一行（start_ts + result_kind='running'），span_id 由调用方（_observability）生成 uuid
- 调用结束 UPDATE 该行（end_ts + 最终状态/指标），按 span_id 定位
- duration_ms = end_ts - start_ts，不冗余存储
- seq 由 start_ts 推导，不冗余存储

通用层级：
- span_id（uuid）全局唯一（跨 node/llm/mcp 表不冲突），parent_id 引用父 span_id，
  支持任意层级（task → node → llm/mcp；未来矫正嵌套、子规划无需改表结构）。
- node span 的 parent_id 为 NULL（根级），LLM/MCP 以 span 栈顶为 parent。

连接管理：
- 每次操作从 psycopg3 异步连接池 async_db_connection() 获取，上下文退出自动提交/归还。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, cast

from db import async_db_connection


class ObsStorage:
    """观测数据 PostgreSQL 存储（span 树模型）。"""

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
                                task_id         TEXT PRIMARY KEY,
                                user_query      TEXT,
                                user_id         TEXT,
                                session_id      TEXT,
                                version         TEXT,
                                intent          TEXT,
                                query_type      TEXT,
                                result_kind     TEXT NOT NULL DEFAULT 'running',
                                result          TEXT NOT NULL DEFAULT 'running',
                                error_what      TEXT,
                                client_duration_ms DOUBLE PRECISION,
                                start_ts        DOUBLE PRECISION NOT NULL,
                                end_ts          DOUBLE PRECISION
                            )
                        """)
                        await cur.execute("""
                            CREATE TABLE IF NOT EXISTS obs_node_spans (
                                span_id         TEXT PRIMARY KEY,
                                task_id         TEXT NOT NULL REFERENCES obs_tasks(task_id),
                                parent_id       TEXT,                    -- 父 span_id（uuid，通用层级；根级 NULL）
                                node            TEXT NOT NULL,
                                result_kind     TEXT NOT NULL DEFAULT 'running',
                                result          TEXT NOT NULL DEFAULT 'running',
                                error_what      TEXT,
                                start_ts        DOUBLE PRECISION NOT NULL,
                                end_ts          DOUBLE PRECISION
                            )
                        """)
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_node_spans_task ON obs_node_spans(task_id)")
                        await cur.execute("""
                            CREATE TABLE IF NOT EXISTS obs_llm_spans (
                                span_id         TEXT PRIMARY KEY,
                                task_id         TEXT NOT NULL REFERENCES obs_tasks(task_id),
                                parent_id       TEXT,                    -- 父 span_id（uuid，通用层级；span 栈顶）
                                node            TEXT NOT NULL,
                                agent           TEXT NOT NULL,
                                input_tokens    INTEGER DEFAULT 0,
                                output_tokens   INTEGER DEFAULT 0,
                                cached_tokens   INTEGER DEFAULT 0,
                                output          TEXT,
                                result_kind     TEXT NOT NULL DEFAULT 'running',
                                result          TEXT NOT NULL DEFAULT 'running',
                                error_what      TEXT,
                                start_ts        DOUBLE PRECISION NOT NULL,
                                end_ts          DOUBLE PRECISION
                            )
                        """)
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_llm_spans_task ON obs_llm_spans(task_id)")
                        await cur.execute("""
                            CREATE TABLE IF NOT EXISTS obs_mcp_spans (
                                span_id         TEXT PRIMARY KEY,
                                task_id         TEXT NOT NULL REFERENCES obs_tasks(task_id),
                                parent_id       TEXT,                    -- 父 span_id（uuid，通用层级；span 栈顶）
                                node            TEXT NOT NULL,
                                server          TEXT,
                                tool            TEXT,
                                retries         INTEGER DEFAULT 0,
                                result_kind     TEXT NOT NULL DEFAULT 'running',
                                result          TEXT NOT NULL DEFAULT 'running',
                                error_what      TEXT,
                                start_ts        DOUBLE PRECISION NOT NULL,
                                end_ts          DOUBLE PRECISION
                            )
                        """)
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_mcp_spans_task ON obs_mcp_spans(task_id)")
                print("✅ 观测数据库初始化完成（span 树模型）")
            except Exception as e:
                print(f"❌ 观测数据库初始化失败: {e}")
                import traceback
                traceback.print_exc()
                raise
            self._initialized = True

    # ── 任务生命周期 ──────────────────────────────────────────

    async def start_task(self, task_id: str, version: str, user_id: str,
                         session_id: str, user_query: str,
                         intent: Optional[str] = None) -> None:
        """任务开始：前置插入 obs_tasks 占位行（task_id=uuid 主键，无自增 id）。

        task_id 与业务 TaskRecord.task_id / LangGraph checkpoint thread_id 一致。
        """
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO obs_tasks
                       (task_id, version, user_id, session_id, user_query, intent, result_kind, result, start_ts)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (task_id) DO NOTHING""",
                    (task_id, version, user_id, session_id, user_query, intent or "",
                     "running", "running", time.time()),
                )

    async def end_task(self, task_id: str, result_kind: str, result: str,
                       error_what: Optional[str], summary: Dict[str, Any]) -> None:
        """任务结束：UPDATE 补全 end_ts / 结果状态。

        注：span 模型下 obs_tasks 不存聚合指标（那些由 trace 合并时从 span 表
        动态计算）。summary 参数保留以兼容调用方，但不再写入。
        """
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE obs_tasks SET result_kind=%s, result=%s, error_what=%s, end_ts=%s WHERE task_id=%s",
                    (result_kind, result, error_what, time.time(), task_id),
                )

    async def update_client_duration(self, task_id: str,
                                     client_duration_ms: Optional[float]) -> None:
        """回写客户端端到端耗时（load_test 压测脚本测量，秒 → 毫秒）。"""
        if client_duration_ms is None:
            return
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE obs_tasks SET client_duration_ms=%s WHERE task_id=%s",
                    (round(float(client_duration_ms), 2), task_id),
                )

    async def update_task_query_type(self, task_id: str, query_type: str) -> None:
        """记录 classify 节点实际分类结果（obs_tasks.query_type）。"""
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE obs_tasks SET query_type=%s WHERE task_id=%s",
                    (query_type, task_id),
                )

    # ── 节点 span ────────────────────────────────────────────

    async def start_node_span(self, task_id: str, span_id: str, node: str,
                              parent_id: Optional[str] = None) -> None:
        """节点调用开始：插入占位行（running），span_id 由调用方生成（uuid，全局唯一）。

        parent_id: 父 span_id（通用层级；根级节点为 None）。
        """
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO obs_node_spans (span_id, task_id, parent_id, node, result_kind, result, start_ts)
                       VALUES (%s,%s,%s,%s,'running','running',%s)""",
                    (span_id, task_id, parent_id, node, time.time()),
                )

    async def end_node_span(self, span_id: str, result_kind: str, result: str,
                            error_what: Optional[str] = None) -> None:
        """节点调用结束：UPDATE 补全 end_ts / 结果状态（按 span_id 定位）。"""
        if not span_id:
            return
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE obs_node_spans SET end_ts=%s, result_kind=%s, result=%s, error_what=%s WHERE span_id=%s",
                    (time.time(), result_kind, result, error_what, span_id),
                )

    async def get_node_spans(self, task_id: str) -> List[Dict[str, Any]]:
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT span_id, task_id, parent_id, node, result_kind, result, error_what, start_ts, end_ts "
                    "FROM obs_node_spans WHERE task_id=%s ORDER BY start_ts ASC",
                    (task_id,),
                )
                return [dict(r) for r in await cur.fetchall()]

    # ── LLM span ─────────────────────────────────────────────

    async def start_llm_span(self, task_id: str, span_id: str, node: str, agent: str,
                             parent_id: Optional[str] = None) -> None:
        """LLM 调用开始：插入占位行（running），span_id 由调用方生成（uuid，全局唯一）。

        流式调用同样只在此占位，流式过程中不写库，结束才 UPDATE。
        parent_id: 父 span_id（span 栈；通常为 node span_id，矫正时为父 llm span_id）。
        """
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO obs_llm_spans (span_id, task_id, parent_id, node, agent, result_kind, result, start_ts)
                       VALUES (%s,%s,%s,%s,%s,'running','running',%s)""",
                    (span_id, task_id, parent_id, node, agent, time.time()),
                )

    async def end_llm_span(self, span_id: str, result_kind: str, result: str,
                           error_what: Optional[str] = None,
                           input_tokens: int = 0, output_tokens: int = 0,
                           cached_tokens: int = 0, output: str = "") -> None:
        """LLM 调用结束：UPDATE 补全 end_ts / token / output / 结果状态（按 span_id 定位）。"""
        if not span_id:
            return
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """UPDATE obs_llm_spans SET
                         end_ts=%s, result_kind=%s, result=%s, error_what=%s,
                         input_tokens=%s, output_tokens=%s, cached_tokens=%s, output=%s
                       WHERE span_id=%s""",
                    (time.time(), result_kind, result, error_what,
                     int(input_tokens or 0), int(output_tokens or 0), int(cached_tokens or 0),
                     output, span_id),
                )

    async def get_llm_spans(self, task_id: str) -> List[Dict[str, Any]]:
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT span_id, task_id, node, agent, input_tokens, output_tokens, cached_tokens,
                              output, result_kind, result, error_what, parent_id, start_ts, end_ts
                       FROM obs_llm_spans WHERE task_id=%s ORDER BY start_ts ASC""",
                    (task_id,),
                )
                return [dict(r) for r in await cur.fetchall()]

    # ── MCP span ─────────────────────────────────────────────

    async def start_mcp_span(self, task_id: str, span_id: str, node: str, server: str, tool: str,
                             parent_id: Optional[str] = None) -> None:
        """MCP 调用开始：插入占位行（running），span_id 由调用方生成（uuid，全局唯一）。"""
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO obs_mcp_spans (span_id, task_id, parent_id, node, server, tool, result_kind, result, start_ts)
                       VALUES (%s,%s,%s,%s,%s,%s,'running','running',%s)""",
                    (span_id, task_id, parent_id, node, server, tool, time.time()),
                )

    async def end_mcp_span(self, span_id: str, result_kind: str, result: str,
                           error_what: Optional[str] = None, retries: int = 0) -> None:
        """MCP 调用结束：UPDATE 补全 end_ts / retries / 结果状态（按 span_id 定位）。"""
        if not span_id:
            return
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE obs_mcp_spans SET end_ts=%s, result_kind=%s, result=%s, error_what=%s, retries=%s WHERE span_id=%s",
                    (time.time(), result_kind, result, error_what, int(retries or 0), span_id),
                )

    async def get_mcp_spans(self, task_id: str) -> List[Dict[str, Any]]:
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT span_id, task_id, node, server, tool, retries, result_kind, result, error_what, parent_id, start_ts, end_ts
                       FROM obs_mcp_spans WHERE task_id=%s ORDER BY start_ts ASC""",
                    (task_id,),
                )
                return [dict(r) for r in await cur.fetchall()]

    # ── 任务读取 ─────────────────────────────────────────────

    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT * FROM obs_tasks WHERE task_id = %s", (task_id,))
                row = await cur.fetchone()
        return dict(row) if row else None


_obs_storage: Optional[ObsStorage] = None


def get_obs_storage() -> ObsStorage:
    """获取全局观测存储单例。"""
    global _obs_storage
    if _obs_storage is None:
        _obs_storage = ObsStorage()
    return _obs_storage
