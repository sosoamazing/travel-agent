"""观测持久化层：通用 span 树模型（节点 / LLM / MCP 逐调用 span）。

表结构（统一模式）：
- obs_tasks        任务父表（task_id=uuid 主键，任务一开始即插入占位，end 时 UPDATE 补全）
- obs_node_spans   节点 span（span_id=uuid 主键，parent_id 引用父 span_id，根级 NULL）
- obs_llm_spans    LLM span（span_id=uuid 主键，parent_id 引用父 span_id）
- obs_mcp_spans    MCP span（span_id=uuid 主键，parent_id 引用父 span_id）

统一「开始占位 + 结束补全」模式（幂等 upsert，乱序可接受）：
- 调用开始 / 结束都走 INSERT ... ON CONFLICT DO UPDATE，span_id 由调用方生成 uuid
- 已有 output / payload.input 时，空值或 running 占位不得覆盖
- duration_ms = end_ts - start_ts，不冗余存储
- seq 由 start_ts 推导，不冗余存储

通用层级：
- span_id（uuid）全局唯一（跨 node/llm/mcp 表不冲突），parent_id 引用父 span_id，
  支持任意层级（task → node → llm/mcp；未来矫正嵌套、子规划无需改表结构）。
- node span 的 parent_id 为 NULL（根级），LLM/MCP 以 span 栈顶为 parent。

写缓冲（关键路径去阻塞）：
- 问题：原实现每个 span 的 start/end 各一次独立 DB 往返，一次复杂规划 100+ 次
  同步 await，全部串在业务关键路径上。
- 方案：span 的 INSERT/UPDATE 不再立即写库，而是按 (task_id → 顺序操作队列) 入缓冲，
  由后台协程批量 flush（攒够 N 条 / 定时 100ms / 任务结束强制），单连接内顺序
  executemany，把 100+ 次连接往返压成个位数。
- 正确性（树完整性）：
   1. 同一 span 的 start/end 都走 `INSERT ... ON CONFLICT DO UPDATE`（幂等 upsert）。
      缓冲或未来队列乱序可接受：end 先到会直接插完整行，晚到的 start 只补 identity 字段。
   2. 覆盖保护：已有 `output` / payload.`input` 时，空值或 `running` 占位不得盖掉终态。
   3. 树靠 parent_id 关联，与写入时刻无关。
   4. `obs_tasks` 的 start/end 不进缓冲，保持即时同步写：它是 monitor 判定
      「任务是否完成」的锚点。`end_task` 在写终态前会强制 flush 该任务的全部 span。

连接管理：
- flush 走 psycopg3 异步连接池 async_db_connection()，一次连接批量写多条。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple, cast

from db import async_db_connection

logger = logging.getLogger(__name__)

# 缓冲 flush 阈值：单任务累计待写操作数达到此值即触发一次即时 flush
_FLUSH_BATCH_SIZE = 32
# 后台 flush 轮询间隔（秒）：兜底把低于阈值的零散操作及时落库
_FLUSH_INTERVAL_SEC = 0.1

# 一条待写操作：(sql, params)。按 task_id 分组、按入队顺序保存。
_Op = Tuple[str, tuple]


class _SpanWriteBuffer:
    """按 task_id 分组的 span 写缓冲：入队 INSERT/UPDATE 操作，后台批量 flush。

    - 每个 task_id 对应一个有序操作列表，保证同一 span 的 INSERT 先于 UPDATE。
    - flush 时按顺序在单连接内逐条 execute（一次连接、一次提交），替代逐操作建连。
    - flush_task(task_id) 供任务结束前强制落库该任务全部 span，确保树完整。
    """

    def __init__(self):
        self._pending: Dict[str, List[_Op]] = {}
        self._lock = asyncio.Lock()
        self._flusher: Optional[asyncio.Task] = None
        self._closed = False

    async def enqueue(self, task_id: str, sql: str, params: tuple) -> None:
        """把一条 span 写操作按 task_id 顺序入队；达到批量阈值则即时 flush 该任务。"""
        should_flush = False
        async with self._lock:
            ops = self._pending.setdefault(task_id or "", [])
            ops.append((sql, params))
            if len(ops) >= _FLUSH_BATCH_SIZE:
                should_flush = True
            self._ensure_flusher_locked()
        if should_flush:
            await self.flush_task(task_id)

    def _ensure_flusher_locked(self) -> None:
        """懒启动后台 flush 协程（须在事件循环内、持锁调用）。"""
        if self._flusher is None and not self._closed:
            self._flusher = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        """后台定时 flush 全部任务的零散待写操作，直到关闭。"""
        try:
            while not self._closed:
                await asyncio.sleep(_FLUSH_INTERVAL_SEC)
                await self.flush_all()
        except asyncio.CancelledError:
            pass

    async def _drain_task_locked(self, task_id: str) -> List[_Op]:
        """取出并清空某任务的待写操作（持锁调用）。"""
        return self._pending.pop(task_id, [])

    async def flush_task(self, task_id: str) -> None:
        """强制 flush 单个任务的全部待写操作（顺序执行，单连接单提交）。

        供 end_task 在写任务终态前调用——保证 monitor 看到终态时该树已完整落库。
        """
        async with self._lock:
            ops = self._pending.pop(task_id or "", [])
        await self._write_ops(ops)

    async def flush_all(self) -> None:
        """flush 所有任务的待写操作（后台轮询 + 进程退出兜底）。"""
        async with self._lock:
            all_ops: List[_Op] = []
            for ops in self._pending.values():
                all_ops.extend(ops)
            self._pending.clear()
        await self._write_ops(all_ops)

    @staticmethod
    async def _write_ops(ops: List[_Op]) -> None:
        """在单个连接内按顺序执行一批写操作（一次提交）。失败不抛，避免观测拖垮业务。"""
        if not ops:
            return
        try:
            async with async_db_connection() as conn:
                async with conn.cursor() as cur:
                    for sql, params in ops:
                        await cur.execute(cast(Any, sql), params)
        except Exception as e:
            logger.warning(f"⚠️ [obs] span 批量 flush 失败（丢弃 {len(ops)} 条，不影响业务）: {e}")

    async def close(self) -> None:
        """进程退出：停止后台协程并把剩余操作全部落库（幂等）。"""
        self._closed = True
        if self._flusher is not None:
            self._flusher.cancel()
            try:
                await self._flusher
            except (asyncio.CancelledError, Exception):
                pass
            self._flusher = None
        await self.flush_all()


class ObsStorage:
    """观测数据 PostgreSQL 存储（span 树模型 + 写缓冲）。"""

    def __init__(self):
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self._buffer = _SpanWriteBuffer()

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
                        await cur.execute("DROP INDEX IF EXISTS idx_node_spans_task")
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_node_spans_task_ts ON obs_node_spans(task_id, start_ts)")
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
                                prompt_id       TEXT,
                                prompt_version  TEXT,
                                result_kind     TEXT NOT NULL DEFAULT 'running',
                                result          TEXT NOT NULL DEFAULT 'running',
                                error_what      TEXT,
                                start_ts        DOUBLE PRECISION NOT NULL,
                                end_ts          DOUBLE PRECISION
                            )
                        """)
                        await cur.execute("DROP INDEX IF EXISTS idx_llm_spans_task")
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_llm_spans_task_ts ON obs_llm_spans(task_id, start_ts)")
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
                        await cur.execute("DROP INDEX IF EXISTS idx_mcp_spans_task")
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_mcp_spans_task_ts ON obs_mcp_spans(task_id, start_ts)")
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_obs_tasks_user_ts ON obs_tasks(user_id, start_ts DESC)")
                        # 旧库补列（CREATE TABLE IF NOT EXISTS 不会改已有表）
                        await cur.execute("ALTER TABLE obs_tasks ADD COLUMN IF NOT EXISTS result_payload TEXT")
                        await cur.execute("ALTER TABLE obs_llm_spans ADD COLUMN IF NOT EXISTS prompt_id TEXT")
                        await cur.execute("ALTER TABLE obs_llm_spans ADD COLUMN IF NOT EXISTS prompt_version TEXT")
                        await cur.execute("""
                            CREATE TABLE IF NOT EXISTS prompt_versions (
                                prompt_id     TEXT NOT NULL,
                                version       TEXT NOT NULL,
                                content       TEXT NOT NULL,
                                content_hash  TEXT NOT NULL,
                                created_at    DOUBLE PRECISION NOT NULL,
                                PRIMARY KEY (prompt_id, version)
                            )
                        """)
                        await cur.execute("""
                            CREATE TABLE IF NOT EXISTS obs_llm_payloads (
                                span_id     TEXT PRIMARY KEY,
                                task_id     TEXT NOT NULL REFERENCES obs_tasks(task_id),
                                input       TEXT,
                                truncated   BOOLEAN NOT NULL DEFAULT FALSE,
                                start_ts    DOUBLE PRECISION NOT NULL
                            )
                        """)
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_llm_payloads_task_ts ON obs_llm_payloads(task_id, start_ts)")
                        try:
                            from config.prompt_registry import all_prompts
                            now = time.time()
                            for pid, (body, ver) in all_prompts().items():
                                await cur.execute(
                                    """INSERT INTO prompt_versions (prompt_id, version, content, content_hash, created_at)
                                       VALUES (%s,%s,%s,%s,%s)
                                       ON CONFLICT (prompt_id, version) DO NOTHING""",
                                    (pid, ver, body, ver, now),
                                )
                        except Exception as pe:
                            logger.warning("⚠️ 提示词版本目录预热失败: %s", pe)
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

        ⚠️ 完整性保证：写任务终态前，先强制 flush 该任务缓冲中的全部 span，
        确保 monitor 一旦看到任务处于终态（ok/error），其 span 树已 100% 落库。
        """
        await self._ensure_init()
        # 关键顺序：先落库该任务全部 span，再写 obs_tasks 终态（终态是可见性锚点）
        await self._buffer.flush_task(task_id)
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
        await self._buffer.enqueue(
            task_id,
            """INSERT INTO obs_node_spans (span_id, task_id, parent_id, node, result_kind, result, start_ts)
               VALUES (%s,%s,%s,%s,'running','running',%s)
               ON CONFLICT (span_id) DO UPDATE SET
                 parent_id = COALESCE(obs_node_spans.parent_id, EXCLUDED.parent_id),
                 node = CASE WHEN obs_node_spans.node IS NULL OR obs_node_spans.node = ''
                             THEN EXCLUDED.node ELSE obs_node_spans.node END""",
            (span_id, task_id, parent_id, node, time.time()),
        )

    async def end_node_span(self, span_id: str, result_kind: str, result: str,
                            error_what: Optional[str] = None,
                            task_id: str = "") -> None:
        """节点调用结束：幂等 upsert 补全 end_ts / 结果；空 running 不覆盖已有终态。"""
        if not span_id:
            return
        await self._ensure_init()
        now = time.time()
        await self._buffer.enqueue(
            task_id,
            """INSERT INTO obs_node_spans (span_id, task_id, parent_id, node, result_kind, result, error_what, start_ts, end_ts)
               VALUES (%s,%s,NULL,'',%s,%s,%s,%s,%s)
               ON CONFLICT (span_id) DO UPDATE SET
                 end_ts = COALESCE(EXCLUDED.end_ts, obs_node_spans.end_ts),
                 result_kind = CASE
                   WHEN EXCLUDED.result_kind = 'running' AND obs_node_spans.result_kind <> 'running'
                   THEN obs_node_spans.result_kind ELSE EXCLUDED.result_kind END,
                 result = CASE
                   WHEN EXCLUDED.result IN ('running', '') AND obs_node_spans.result NOT IN ('running', '')
                   THEN obs_node_spans.result ELSE EXCLUDED.result END,
                 error_what = COALESCE(EXCLUDED.error_what, obs_node_spans.error_what)""",
            (span_id, task_id, result_kind, result, error_what, now, now),
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
        """LLM 调用开始：幂等 upsert 占位行（running）。结束补全走 end_llm_span。"""
        await self._ensure_init()
        await self._buffer.enqueue(
            task_id,
            """INSERT INTO obs_llm_spans (span_id, task_id, parent_id, node, agent, result_kind, result, start_ts)
               VALUES (%s,%s,%s,%s,%s,'running','running',%s)
               ON CONFLICT (span_id) DO UPDATE SET
                 parent_id = COALESCE(obs_llm_spans.parent_id, EXCLUDED.parent_id),
                 node = CASE WHEN obs_llm_spans.node IS NULL OR obs_llm_spans.node = ''
                             THEN EXCLUDED.node ELSE obs_llm_spans.node END,
                 agent = CASE WHEN obs_llm_spans.agent IS NULL OR obs_llm_spans.agent = ''
                              THEN EXCLUDED.agent ELSE obs_llm_spans.agent END""",
            (span_id, task_id, parent_id, node, agent, time.time()),
        )

    async def end_llm_span(self, span_id: str, result_kind: str, result: str,
                           error_what: Optional[str] = None,
                           input_tokens: int = 0, output_tokens: int = 0,
                           cached_tokens: int = 0, output: str = "",
                           task_id: str = "",
                           prompt_id: Optional[str] = None,
                           prompt_version: Optional[str] = None,
                           input_text: Optional[str] = None,
                           input_truncated: bool = False) -> None:
        """LLM 调用结束：幂等 upsert token / output / 版本指针 / payload。"""
        if not span_id:
            return
        await self._ensure_init()
        now = time.time()
        await self._buffer.enqueue(
            task_id,
            """INSERT INTO obs_llm_spans (
                 span_id, task_id, parent_id, node, agent,
                 input_tokens, output_tokens, cached_tokens, output,
                 prompt_id, prompt_version, result_kind, result, error_what, start_ts, end_ts
               ) VALUES (%s,%s,NULL,'','',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (span_id) DO UPDATE SET
                 end_ts = COALESCE(EXCLUDED.end_ts, obs_llm_spans.end_ts),
                 result_kind = CASE
                   WHEN EXCLUDED.result_kind = 'running' AND obs_llm_spans.result_kind <> 'running'
                   THEN obs_llm_spans.result_kind ELSE EXCLUDED.result_kind END,
                 result = CASE
                   WHEN EXCLUDED.result IN ('running', '') AND obs_llm_spans.result NOT IN ('running', '')
                   THEN obs_llm_spans.result ELSE EXCLUDED.result END,
                 error_what = COALESCE(EXCLUDED.error_what, obs_llm_spans.error_what),
                 input_tokens = GREATEST(COALESCE(obs_llm_spans.input_tokens, 0), EXCLUDED.input_tokens),
                 output_tokens = GREATEST(COALESCE(obs_llm_spans.output_tokens, 0), EXCLUDED.output_tokens),
                 cached_tokens = GREATEST(COALESCE(obs_llm_spans.cached_tokens, 0), EXCLUDED.cached_tokens),
                 output = CASE
                   WHEN obs_llm_spans.output IS NOT NULL AND obs_llm_spans.output <> ''
                        AND (EXCLUDED.output IS NULL OR EXCLUDED.output = '')
                   THEN obs_llm_spans.output
                   ELSE COALESCE(NULLIF(EXCLUDED.output, ''), obs_llm_spans.output) END,
                 prompt_id = COALESCE(obs_llm_spans.prompt_id, EXCLUDED.prompt_id),
                 prompt_version = COALESCE(obs_llm_spans.prompt_version, EXCLUDED.prompt_version)""",
            (span_id, task_id,
             int(input_tokens or 0), int(output_tokens or 0), int(cached_tokens or 0),
             output, prompt_id, prompt_version, result_kind, result, error_what, now, now),
        )
        if input_text is not None:
            await self._buffer.enqueue(
                task_id,
                """INSERT INTO obs_llm_payloads (span_id, task_id, input, truncated, start_ts)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (span_id) DO UPDATE SET
                     input = CASE
                       WHEN obs_llm_payloads.input IS NOT NULL AND obs_llm_payloads.input <> ''
                            AND (EXCLUDED.input IS NULL OR EXCLUDED.input = '')
                       THEN obs_llm_payloads.input
                       ELSE COALESCE(NULLIF(EXCLUDED.input, ''), obs_llm_payloads.input) END,
                     truncated = CASE
                       WHEN obs_llm_payloads.input IS NOT NULL AND obs_llm_payloads.input <> ''
                            AND (EXCLUDED.input IS NULL OR EXCLUDED.input = '')
                       THEN obs_llm_payloads.truncated ELSE EXCLUDED.truncated END""",
                (span_id, task_id, input_text, bool(input_truncated), now),
            )

    async def get_llm_spans(self, task_id: str) -> List[Dict[str, Any]]:
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT span_id, task_id, node, agent, input_tokens, output_tokens, cached_tokens,
                              output, prompt_id, prompt_version, result_kind, result, error_what, parent_id, start_ts, end_ts
                       FROM obs_llm_spans WHERE task_id=%s ORDER BY start_ts ASC""",
                    (task_id,),
                )
                return [dict(r) for r in await cur.fetchall()]

    # ── MCP span ─────────────────────────────────────────────

    async def start_mcp_span(self, task_id: str, span_id: str, node: str, server: str, tool: str,
                             parent_id: Optional[str] = None) -> None:
        """MCP 调用开始：插入占位行（running），span_id 由调用方生成（uuid，全局唯一）。"""
        await self._ensure_init()
        await self._buffer.enqueue(
            task_id,
            """INSERT INTO obs_mcp_spans (span_id, task_id, parent_id, node, server, tool, result_kind, result, start_ts)
               VALUES (%s,%s,%s,%s,%s,%s,'running','running',%s)
               ON CONFLICT (span_id) DO UPDATE SET
                 parent_id = COALESCE(obs_mcp_spans.parent_id, EXCLUDED.parent_id),
                 node = CASE WHEN obs_mcp_spans.node IS NULL OR obs_mcp_spans.node = ''
                             THEN EXCLUDED.node ELSE obs_mcp_spans.node END,
                 server = COALESCE(obs_mcp_spans.server, EXCLUDED.server),
                 tool = COALESCE(obs_mcp_spans.tool, EXCLUDED.tool)""",
            (span_id, task_id, parent_id, node, server, tool, time.time()),
        )

    async def end_mcp_span(self, span_id: str, result_kind: str, result: str,
                           error_what: Optional[str] = None, retries: int = 0,
                           task_id: str = "") -> None:
        """MCP 调用结束：幂等 upsert 补全 end_ts / retries / 结果。"""
        if not span_id:
            return
        await self._ensure_init()
        now = time.time()
        await self._buffer.enqueue(
            task_id,
            """INSERT INTO obs_mcp_spans (span_id, task_id, parent_id, node, server, tool, retries, result_kind, result, error_what, start_ts, end_ts)
               VALUES (%s,%s,NULL,'','','',%s,%s,%s,%s,%s,%s)
               ON CONFLICT (span_id) DO UPDATE SET
                 end_ts = COALESCE(EXCLUDED.end_ts, obs_mcp_spans.end_ts),
                 result_kind = CASE
                   WHEN EXCLUDED.result_kind = 'running' AND obs_mcp_spans.result_kind <> 'running'
                   THEN obs_mcp_spans.result_kind ELSE EXCLUDED.result_kind END,
                 result = CASE
                   WHEN EXCLUDED.result IN ('running', '') AND obs_mcp_spans.result NOT IN ('running', '')
                   THEN obs_mcp_spans.result ELSE EXCLUDED.result END,
                 error_what = COALESCE(EXCLUDED.error_what, obs_mcp_spans.error_what),
                 retries = GREATEST(COALESCE(obs_mcp_spans.retries, 0), EXCLUDED.retries)""",
            (span_id, task_id, int(retries or 0), result_kind, result, error_what, now, now),
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

    async def update_task_result_payload(self, task_id: str, payload: Optional[str]) -> None:
        """任务结束时把业务结果 JSON 落到 obs_tasks.result_payload（Redis miss 回退用）。"""
        if not task_id:
            return
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE obs_tasks SET result_payload=%s WHERE task_id=%s",
                    (payload, task_id),
                )

    async def upsert_prompt_version(self, prompt_id: str, version: str,
                                    content: str, content_hash: str) -> None:
        """模板目录：仅在未见过的 (prompt_id, version) 时插入。"""
        if not prompt_id or not version:
            return
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO prompt_versions (prompt_id, version, content, content_hash, created_at)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (prompt_id, version) DO NOTHING""",
                    (prompt_id, version, content, content_hash, time.time()),
                )

    async def get_task_events(self, task_id: str, since_ts: float = 0.0,
                              include_payload: bool = False) -> List[Dict[str, Any]]:
        """增量拉取某任务 start_ts > since_ts 的 span（node/llm/mcp 合并排序）。

        默认不带大 TEXT input；include_payload=True 时按 span_id 附上渲染后的 input。
        """
        await self._ensure_init()
        events: List[Dict[str, Any]] = []
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT span_id, task_id, parent_id, node, result_kind, result, error_what, start_ts, end_ts
                       FROM obs_node_spans WHERE task_id=%s AND start_ts > %s""",
                    (task_id, since_ts),
                )
                for r in await cur.fetchall():
                    item = dict(r)
                    item["span_type"] = "node"
                    events.append(item)
                await cur.execute(
                    """SELECT span_id, task_id, parent_id, node, agent, input_tokens, output_tokens, cached_tokens,
                              output, prompt_id, prompt_version, result_kind, result, error_what, start_ts, end_ts
                       FROM obs_llm_spans WHERE task_id=%s AND start_ts > %s""",
                    (task_id, since_ts),
                )
                for r in await cur.fetchall():
                    item = dict(r)
                    item["span_type"] = "llm"
                    events.append(item)
                await cur.execute(
                    """SELECT span_id, task_id, parent_id, node, server, tool, retries, result_kind, result, error_what, start_ts, end_ts
                       FROM obs_mcp_spans WHERE task_id=%s AND start_ts > %s""",
                    (task_id, since_ts),
                )
                for r in await cur.fetchall():
                    item = dict(r)
                    item["span_type"] = "mcp"
                    events.append(item)
                if include_payload:
                    llm_ids = [ev["span_id"] for ev in events if ev.get("span_type") == "llm" and ev.get("span_id")]
                    payloads: Dict[str, Dict[str, Any]] = {}
                    if llm_ids:
                        placeholders = ",".join(["%s"] * len(llm_ids))
                        await cur.execute(
                            f"SELECT span_id, input, truncated FROM obs_llm_payloads WHERE span_id IN ({placeholders})",
                            tuple(llm_ids),
                        )
                        payloads = {row["span_id"]: dict(row) for row in await cur.fetchall()}
                    for ev in events:
                        if ev.get("span_type") == "llm" and ev.get("span_id") in payloads:
                            ev["input"] = payloads[ev["span_id"]].get("input")
                            ev["input_truncated"] = payloads[ev["span_id"]].get("truncated")
        events.sort(key=lambda e: (float(e.get("start_ts") or 0), str(e.get("span_id") or "")))
        return events

    # ── 写缓冲生命周期 ────────────────────────────────────────

    async def flush_task(self, task_id: str) -> None:
        """强制把某任务缓冲中的全部 span 落库（供需要立即读一致视图的场景）。"""
        await self._buffer.flush_task(task_id)

    async def close(self) -> None:
        """进程退出：停止后台 flush 协程并把缓冲中剩余 span 全部落库（幂等）。"""
        await self._buffer.close()


_obs_storage: Optional[ObsStorage] = None


def get_obs_storage() -> ObsStorage:
    """获取全局观测存储单例。"""
    global _obs_storage
    if _obs_storage is None:
        _obs_storage = ObsStorage()
    return _obs_storage
