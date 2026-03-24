"""
对话历史管理模块
- SQLite 数据库持久化
- 支持多用户、多会话
- 会话管理
"""
import sqlite3
import json
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional
from pathlib import Path
from dataclasses import dataclass, asdict
from config.settings import PROJECT_ROOT


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
    
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (PROJECT_ROOT / "data" / "chat_history.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._current_user_id = "default_user"
        self._init_database()
    
    def _get_connection(self):
        """获取数据库连接"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn
    
    def _init_database(self):
        """初始化数据库表"""
        # 检查数据库是否存在但表结构有问题
        needs_recreate = False
        if self.db_path.exists():
            try:
                test_conn = sqlite3.connect(str(self.db_path))
                test_cursor = test_conn.cursor()
                test_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chat_sessions'")
                if not test_cursor.fetchone():
                    needs_recreate = True
                test_conn.close()
            except Exception:
                needs_recreate = True
        
        # 如果需要重新创建，删除旧数据库
        if needs_recreate:
            print(f"⚠️ 检测到数据库结构问题，删除旧数据库: {self.db_path}")
            try:
                self.db_path.unlink()
            except Exception as e:
                print(f"❌ 删除旧数据库失败: {e}")
        
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            # 创建会话表
            cursor.execute("""
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
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    metadata TEXT,
                    FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id)
                )
            """)
            
            # 创建索引（单独创建）
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON chat_sessions(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON chat_sessions(updated_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_id ON chat_messages(session_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_id ON chat_messages(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON chat_messages(timestamp)")
            
            conn.commit()
            print("✅ 对话历史数据库初始化完成")
        except Exception as e:
            print(f"❌ 数据库初始化失败: {e}")
            import traceback
            traceback.print_exc()
        finally:
            conn.close()
    
    def create_session(self, user_id: Optional[str] = None, title: Optional[str] = None) -> str:
        """创建新会话"""
        user_id = user_id or self._current_user_id
        now = datetime.now().isoformat()
        
        if not title:
            title = f"对话 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        
        session_id = f"{user_id}_{int(datetime.now().timestamp())}"
        
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at, message_count)
                VALUES (?, ?, ?, ?, ?, 0)
            """, (session_id, user_id, title, now, now))
            conn.commit()
            print(f"✅ 新会话已创建: {session_id}")
            return session_id
        finally:
            conn.close()
    
    def add_message(self, session_id: str, message_type: str, content: str, 
                   user_id: Optional[str] = None, metadata: Optional[Dict] = None) -> int:
        """添加一条消息"""
        user_id = user_id or self._current_user_id
        now = datetime.now().isoformat()
        metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
        
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            # 插入消息
            cursor.execute("""
                INSERT INTO chat_messages (session_id, user_id, message_type, content, timestamp, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (session_id, user_id, message_type, content, now, metadata_json))
            
            message_id = cursor.lastrowid
            
            # 更新会话的更新时间和消息计数
            cursor.execute("""
                UPDATE chat_sessions 
                SET updated_at = ?, message_count = message_count + 1
                WHERE session_id = ?
            """, (now, session_id))
            
            conn.commit()
            print(f"✅ 消息已保存: session={session_id}, type={message_type}, id={message_id}")
            return message_id
        finally:
            conn.close()
    
    def get_session_messages(self, session_id: str) -> List[ChatMessage]:
        """获取会话的所有消息"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT session_id, user_id, message_type, content, timestamp, metadata
                FROM chat_messages
                WHERE session_id = ?
                ORDER BY timestamp ASC
            """, (session_id,))
            
            rows = cursor.fetchall()
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
        finally:
            conn.close()
    
    def get_user_sessions(self, user_id: Optional[str] = None, limit: int = 50) -> List[ChatSession]:
        """获取用户的所有会话，按更新时间倒序"""
        user_id = user_id or self._current_user_id
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT session_id, user_id, title, created_at, updated_at, message_count
                FROM chat_sessions
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
            """, (user_id, limit))
            
            rows = cursor.fetchall()
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
        finally:
            conn.close()
    
    def update_session_title(self, session_id: str, title: str):
        """更新会话标题"""
        now = datetime.now().isoformat()
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE chat_sessions 
                SET title = ?, updated_at = ?
                WHERE session_id = ?
            """, (title, now, session_id))
            conn.commit()
        finally:
            conn.close()
    
    def delete_session(self, session_id: str):
        """删除会话及其所有消息"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            # 先删除消息
            cursor.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            
            # 再删除会话
            cursor.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
            
            conn.commit()
            print(f"✅ 会话已删除: {session_id}")
        finally:
            conn.close()
    
    def get_last_session(self, user_id: Optional[str] = None) -> Optional[ChatSession]:
        """获取用户最近的一个会话"""
        sessions = self.get_user_sessions(user_id, limit=1)
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
