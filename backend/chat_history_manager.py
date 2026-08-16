"""
对话历史管理模块
- PostgreSQL 数据库持久化
- 支持多用户、多会话
- 会话管理
"""
import asyncio
import json
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from db import async_db_connection


@dataclass
class ChatMessage:
    """单条聊天消息"""
    session_id: str
    user_id: str
    message_type: str  # "user" or "ai"
    content: str
    timestamp: str
    metadata: Optional[str] = None  # JSON 格式的元数据


@dataclass
class ChatSession:
    """聊天会话"""
    session_id: str
    user_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


class ChatHistoryManager:
    """对话历史管理器"""

    def __init__(self):
        self._current_user_id = "default_user"
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def _ensure_init(self):
        """初始化数据库表（懒加载，asyncio.Lock 守卫）。"""
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            try:
                async with async_db_connection() as conn:
                    async with conn.cursor() as cur:
                        # 创建会话表
                        await cur.execute("""
                            CREATE TABLE IF NOT EXISTS chat_sessions (
                                session_id TEXT PRIMARY KEY,
                                user_id TEXT NOT NULL,
                                title TEXT NOT NULL,
                                created_at TEXT NOT NULL,
                                updated_at TEXT NOT NULL,
                                message_count INTEGER DEFAULT 0
                            )
                        """)

                        # 创建消息表
                        await cur.execute("""
                            CREATE TABLE IF NOT EXISTS chat_messages (
                                id SERIAL PRIMARY KEY,
                                session_id TEXT NOT NULL,
                                user_id TEXT NOT NULL,
                                message_type TEXT NOT NULL,
                                content TEXT NOT NULL,
                                timestamp TEXT NOT NULL,
                                metadata TEXT,
                                FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id)
                            )
                        """)

                        # 创建索引
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON chat_sessions(user_id)")
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON chat_sessions(updated_at)")
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_id ON chat_messages(session_id)")
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_id ON chat_messages(user_id)")
                        await cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON chat_messages(timestamp)")
                print("✅ 对话历史数据库初始化完成")
            except Exception as e:
                print(f"❌ 数据库初始化失败: {e}")
                import traceback
                traceback.print_exc()
                raise
            self._initialized = True

    async def create_session(self, user_id: Optional[str] = None, title: Optional[str] = None) -> str:
        """创建新会话"""
        await self._ensure_init()
        user_id = user_id or self._current_user_id
        now = datetime.now().isoformat()

        if not title:
            title = f"对话 {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        session_id = f"{user_id}_{int(datetime.now().timestamp())}"

        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at, message_count)
                    VALUES (%s, %s, %s, %s, %s, 0)
                """, (session_id, user_id, title, now, now))
        print(f"✅ 新会话已创建: {session_id}")
        return session_id

    async def add_message(self, session_id: str, message_type: str, content: str,
                          user_id: Optional[str] = None, metadata: Optional[Dict] = None) -> int:
        """添加一条消息"""
        await self._ensure_init()
        user_id = user_id or self._current_user_id
        now = datetime.now().isoformat()
        metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata else None

        # 插入消息 + 更新会话计数/时间：两条写操作必须同一事务块内
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                # 插入消息并返回自增 id
                await cur.execute("""
                    INSERT INTO chat_messages (session_id, user_id, message_type, content, timestamp, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (session_id, user_id, message_type, content, now, metadata_json))

                row = await cur.fetchone()
                assert row is not None, "INSERT ... RETURNING id 未返回行"
                message_id = row["id"]

                # 更新会话的更新时间和消息计数
                await cur.execute("""
                    UPDATE chat_sessions
                    SET updated_at = %s, message_count = message_count + 1
                    WHERE session_id = %s
                """, (now, session_id))

        print(f"✅ 消息已保存: session={session_id}, type={message_type}, id={message_id}")
        return message_id

    async def get_session_messages(self, session_id: str) -> List[ChatMessage]:
        """获取会话的所有消息"""
        await self._ensure_init()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT session_id, user_id, message_type, content, timestamp, metadata
                    FROM chat_messages
                    WHERE session_id = %s
                    ORDER BY timestamp ASC
                """, (session_id,))

                rows = await cur.fetchall()

        messages = [
            ChatMessage(
                session_id=row["session_id"],
                user_id=row["user_id"],
                message_type=row["message_type"],
                content=row["content"],
                timestamp=row["timestamp"],
                metadata=row["metadata"]
            )
            for row in rows
        ]
        print(f"📥 从会话 {session_id} 加载了 {len(messages)} 条消息")
        return messages

    async def get_user_sessions(self, user_id: Optional[str] = None, limit: int = 50) -> List[ChatSession]:
        """获取用户的所有会话，按更新时间倒序"""
        await self._ensure_init()
        user_id = user_id or self._current_user_id
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT session_id, user_id, title, created_at, updated_at, message_count
                    FROM chat_sessions
                    WHERE user_id = %s
                    ORDER BY updated_at DESC
                    LIMIT %s
                """, (user_id, limit))

                rows = await cur.fetchall()

        sessions = [
            ChatSession(
                session_id=row["session_id"],
                user_id=row["user_id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                message_count=row["message_count"]
            )
            for row in rows
        ]
        print(f"📊 为用户 {user_id} 找到了 {len(sessions)} 个会话")
        return sessions

    async def update_session_title(self, session_id: str, title: str):
        """更新会话标题"""
        await self._ensure_init()
        now = datetime.now().isoformat()
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE chat_sessions
                    SET title = %s, updated_at = %s
                    WHERE session_id = %s
                """, (title, now, session_id))

    async def delete_session(self, session_id: str):
        """删除会话及其所有消息"""
        await self._ensure_init()
        # 删除消息 + 删除会话：同一事务块内
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                # 先删除消息
                await cur.execute("DELETE FROM chat_messages WHERE session_id = %s", (session_id,))

                # 再删除会话
                await cur.execute("DELETE FROM chat_sessions WHERE session_id = %s", (session_id,))

        print(f"✅ 会话已删除: {session_id}")

    async def get_last_session(self, user_id: Optional[str] = None) -> Optional[ChatSession]:
        """获取用户最近的一个会话"""
        sessions = await self.get_user_sessions(user_id, limit=1)
        return sessions[0] if sessions else None

    def messages_to_streamlit_format(self, messages: List[ChatMessage]) -> List[Dict]:
        """将消息转换为 Streamlit 格式"""
        return [
            {
                "role": "user" if msg.message_type == "user" else "assistant",
                "content": msg.content
            }
            for msg in messages
        ]


_chat_history_manager = None


def get_chat_history_manager() -> ChatHistoryManager:
    """获取全局对话历史管理器实例"""
    global _chat_history_manager
    if _chat_history_manager is None:
        _chat_history_manager = ChatHistoryManager()
    return _chat_history_manager
