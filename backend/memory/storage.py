"""PostgreSQL 持久化层：working_memory（工作记忆快照）与 trip_episodes（情景记忆事件）两张表。"""
import asyncio
import json
from typing import Any, Dict, List, Optional, cast

from db import async_db_connection
from memory.base import WorkingMemorySnapshot, TripEpisode, _now


class MemoryStorage:
    """记忆系统 PostgreSQL 存储层（双表）。"""

    def __init__(self):
        self._init_lock = asyncio.Lock()
        self._initialized = False

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
                            CREATE TABLE IF NOT EXISTS working_memory (
                                id SERIAL PRIMARY KEY,
                                session_id TEXT NOT NULL,
                                user_id TEXT NOT NULL,
                                snapshot TEXT NOT NULL,          -- JSON 快照（3.2 节结构）
                                ttl_seconds INTEGER DEFAULT 86400,
                                created_at TEXT NOT NULL,
                                expires_at TEXT NOT NULL,
                                UNIQUE(session_id, user_id)
                            )
                        """)
                        await cur.execute("""
                            CREATE TABLE IF NOT EXISTS trip_episodes (
                                id SERIAL PRIMARY KEY,
                                user_id TEXT NOT NULL,
                                session_id TEXT NOT NULL,
                                origin TEXT,
                                destination TEXT NOT NULL,
                                start_date TEXT,
                                end_date TEXT,
                                nights INTEGER,
                                total_budget DOUBLE PRECISION,
                                total_spent DOUBLE PRECISION,
                                transport_mode TEXT,
                                transport_cost DOUBLE PRECISION,
                                hotels TEXT,          -- JSON [{name, price, area}]
                                attractions TEXT,     -- JSON [{name, city}]
                                feedback TEXT,
                                satisfaction INTEGER, -- 0-5
                                summary TEXT,
                                created_at TEXT NOT NULL
                            )
                        """)
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_episodes_user_dest ON trip_episodes(user_id, destination)")
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_episodes_user_created ON trip_episodes(user_id, created_at)")
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_wm_session ON working_memory(session_id)")
                print("✅ 记忆数据库初始化完成")
            except Exception as e:
                print(f"❌ 记忆数据库初始化失败: {e}")
                import traceback
                traceback.print_exc()
                raise
            self._initialized = True

    # ═══════════════ 工作记忆 ═══════════════

    async def save_snapshot(self, snapshot: WorkingMemorySnapshot):
        """保存（或覆盖）指定会话的工作记忆快照。"""
        await self._ensure_init()
        from datetime import datetime, timedelta
        data = snapshot.to_dict()
        created = _now()
        expires = (datetime.now() + timedelta(seconds=snapshot.ttl_seconds)).isoformat()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO working_memory (session_id, user_id, snapshot, ttl_seconds, created_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT(session_id, user_id)
                    DO UPDATE SET snapshot = excluded.snapshot,
                                  ttl_seconds = excluded.ttl_seconds,
                                  created_at = excluded.created_at,
                                  expires_at = excluded.expires_at
                """, (
                    snapshot.session_id, snapshot.user_id, json.dumps(data, ensure_ascii=False),
                    snapshot.ttl_seconds, created, expires,
                ))
        print(f"🧠 [工作记忆] 快照已保存: session={snapshot.session_id}, "
              f"城数={len(snapshot.cities)}, locked_spent={snapshot.locked_spent:.0f}")

    async def load_snapshot(self, session_id: str, user_id: str = "default_user") -> Optional[WorkingMemorySnapshot]:
        """加载会话的工作记忆快照；不存在或已过期返回 None。"""
        await self._ensure_init()
        try:
            async with async_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT snapshot FROM working_memory WHERE session_id = %s AND user_id = %s",
                        (session_id, user_id),
                    )
                    row = await cur.fetchone()
            if not row:
                return None
            snapshot = WorkingMemorySnapshot.from_dict(json.loads(row["snapshot"]))
            if snapshot.is_expired():
                print(f"🧠 [工作记忆] 快照已过期(TTL {snapshot.ttl_seconds}s)，忽略: {session_id}")
                return None
            return snapshot
        except Exception as e:
            print(f"⚠️ [工作记忆] 加载快照失败: {e}")
            return None

    async def delete_snapshot(self, session_id: str, user_id: str = "default_user"):
        """删除会话的工作记忆（规划完成且被用户采纳后调用）。"""
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM working_memory WHERE session_id = %s AND user_id = %s",
                    (session_id, user_id),
                )
        print(f"🧠 [工作记忆] 快照已清除: {session_id}")

    # ═══════════════ 情景记忆 ═══════════════

    async def save_episode(self, episode: TripEpisode) -> int:
        """保存一条情景记忆，返回 episode id。"""
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO trip_episodes (
                        user_id, session_id, origin, destination, start_date, end_date, nights,
                        total_budget, total_spent, transport_mode, transport_cost,
                        hotels, attractions, feedback, satisfaction, summary, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    episode.user_id, episode.session_id, episode.origin, episode.destination,
                    episode.start_date, episode.end_date, episode.nights,
                    episode.total_budget, episode.total_spent, episode.transport_mode, episode.transport_cost,
                    json.dumps(episode.hotels, ensure_ascii=False),
                    json.dumps(episode.attractions, ensure_ascii=False),
                    episode.feedback, episode.satisfaction, episode.summary,
                    episode.created_at,
                ))
                row = await cur.fetchone()
        assert row is not None, "INSERT ... RETURNING id 未返回行"
        episode_id = row["id"]
        print(f"📅 [情景记忆] 已保存行程: {episode.origin}→{episode.destination} "
              f"{episode.nights}晚 花费{episode.total_spent:.0f} (id={episode_id})")
        return episode_id

    async def list_episodes(self, user_id: str = "default_user", limit: int = 50) -> List[Dict[str, Any]]:
        """按时间倒序返回用户的历史行程。"""
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT * FROM trip_episodes
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (user_id, limit))
                rows = await cur.fetchall()
        return [self._episode_row_to_dict(r) for r in rows]

    async def search_episodes(
        self,
        user_id: str = "default_user",
        destination: str = "",
        origin: str = "",
        min_budget: float = 0,
        max_budget: float = 0,
        limit: int = 2,
    ) -> List[Dict[str, Any]]:
        """按目的地 > 预算区间 > 出发地的优先级检索历史行程。

        返回空表：无匹配。
        """
        await self._ensure_init()
        clauses = ["user_id = %s"]
        params: List[Any] = [user_id]

        if destination:
            clauses.append("destination = %s")
            params.append(destination)
        if origin:
            clauses.append("origin = %s")
            params.append(origin)
        if min_budget > 0:
            clauses.append("total_budget >= %s")
            params.append(min_budget)
        if max_budget > 0:
            clauses.append("total_budget <= %s")
            params.append(max_budget)

        sql = f"SELECT * FROM trip_episodes WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                # sql 仅由固定 clause 片段拼接（参数全部走 %s），无用户输入注入
                await cur.execute(cast(Any, sql), params)
                rows = await cur.fetchall()
        return [self._episode_row_to_dict(r) for r in rows]

    async def update_episode_feedback(self, episode_id: int, feedback: str, satisfaction: Optional[int]):
        """更新某条历史行程的反馈与满意度。"""
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE trip_episodes SET feedback = %s, satisfaction = %s WHERE id = %s",
                    (feedback, satisfaction, episode_id),
                )
        print(f"📅 [情景记忆] 已更新行程反馈: id={episode_id} satisfaction={satisfaction}")

    @staticmethod
    def _episode_row_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
        d = dict(row)
        for key in ("hotels", "attractions"):
            try:
                d[key] = json.loads(d[key]) if d.get(key) else []
            except Exception:
                d[key] = []
        return d


_memory_storage: Optional[MemoryStorage] = None


def get_memory_storage() -> MemoryStorage:
    """获取全局记忆存储实例。"""
    global _memory_storage
    if _memory_storage is None:
        _memory_storage = MemoryStorage()
    return _memory_storage
