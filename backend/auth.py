"""JWT 认证模块：用户注册/登录、密码哈希、令牌签发与校验。

设计说明：
- 用户表 users 建在现有 PostgreSQL 上，与 chat_sessions/chat_messages 同库。
- 会话/消息沿用 user_id TEXT 字段，直接用「用户名」作为 user_id（用户名唯一），
  从而无需改动 chat_history_manager 的既有表结构。
- 密码使用 bcrypt 哈希（CPU 密集，异步层用 asyncio.to_thread 包装）；令牌使用 PyJWT（HS256），
  有效期与密钥可通过 .env 覆盖。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config.settings import (
    JWT_ALGORITHM,
    JWT_EXPIRE_MINUTES,
    JWT_SECRET,
    SUPERADMIN_PASSWORD,
    SUPERADMIN_USERNAME,
)
from db import async_db_connection

# HTTPBearer(auto_error=False)：缺 header 时不自动抛错，由 get_current_user 统一返回 401
_http_bearer = HTTPBearer(auto_error=False)

_users_table_lock = None  # asyncio.Lock，首次使用时懒创建（须在事件循环内）
_users_table_ready = False

# ──────────────────────────────────────────────────────────
# 角色定义
# ──────────────────────────────────────────────────────────

ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLE_SUPERADMIN = "superadmin"


def is_admin_role(role: str) -> bool:
    """是否具备管理员权限（admin / superadmin 均为管理员）。"""
    return role in (ROLE_ADMIN, ROLE_SUPERADMIN)


def is_superadmin_role(role: str) -> bool:
    """是否为超级管理员（唯一可创建/删除管理员的角色）。"""
    return role == ROLE_SUPERADMIN


# ──────────────────────────────────────────────────────────
# 用户表
# ──────────────────────────────────────────────────────────

async def _ensure_users_table() -> None:
    """幂等建表（首次访问时创建，避免 import 阶段触碰数据库连接）。"""
    global _users_table_ready, _users_table_lock
    if _users_table_ready:
        return
    if _users_table_lock is None:
        _users_table_lock = asyncio.Lock()
    async with _users_table_lock:
        if _users_table_ready:
            return
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        username TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """)
                # 迁移：为存量 users 表补充 role 列（幂等，不影响既有数据与 user_id 逻辑）
                await cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user'")
            # 首次启动自动初始化超级管理员（与建表共用同一连接/事务，由 async_db_connection 退出时提交）
            await _seed_superadmin(conn)
        _users_table_ready = True


async def _seed_superadmin(conn) -> None:
    """首次启动自动初始化超级管理员（幂等）。

    - 已存在任意 superadmin 角色用户 → 直接跳过；
    - SUPERADMIN_USERNAME 已被占用但非 superadmin → 仅打印警告，不写入；
    - 否则插入超级管理员（密码取自 .env / 默认值）。
    """
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM users WHERE role = %s LIMIT 1", (ROLE_SUPERADMIN,))
        if await cur.fetchone():
            return
        await cur.execute("SELECT 1 FROM users WHERE username = %s", (SUPERADMIN_USERNAME,))
        if await cur.fetchone():
            print(f"⚠️ 超级管理员用户名「{SUPERADMIN_USERNAME}」已存在，跳过初始化")
            return
        await cur.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (%s, %s, %s, %s)",
            (
                SUPERADMIN_USERNAME,
                await asyncio.to_thread(hash_password, SUPERADMIN_PASSWORD),
                ROLE_SUPERADMIN,
                datetime.now().isoformat(),
            ),
        )
        print(f"✅ 已初始化超级管理员「{SUPERADMIN_USERNAME}」(role={ROLE_SUPERADMIN})")


async def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """按用户名查询用户（不存在返回 None）。"""
    await _ensure_users_table()
    async with async_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, username, password_hash, role, created_at FROM users WHERE username = %s",
                (username,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def register_user(username: str, password: str) -> Dict[str, Any]:
    """注册新用户（用户名唯一）。用户名已存在时抛 409。"""
    await _ensure_users_table()
    username = username.strip()
    now = datetime.now().isoformat()
    async with async_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
            if await cur.fetchone():
                raise HTTPException(status_code=409, detail="用户名已存在")
            await cur.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
                (username, await asyncio.to_thread(hash_password, password), ROLE_USER, now),
            )
            row = await cur.fetchone()
            assert row is not None, "INSERT ... RETURNING id 未返回行"
            user_id = row["id"]
    return {"id": user_id, "username": username, "role": ROLE_USER}


async def create_admin_user(username: str, password: str) -> Dict[str, Any]:
    """创建管理员账号（role=admin）。用户名已存在时抛 409。

    仅允许超级管理员调用；superadmin 本身不可通过此函数创建（防止提权）。
    """
    await _ensure_users_table()
    username = username.strip()
    now = datetime.now().isoformat()
    async with async_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
            if await cur.fetchone():
                raise HTTPException(status_code=409, detail="用户名已存在")
            await cur.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
                (username, await asyncio.to_thread(hash_password, password), ROLE_ADMIN, now),
            )
            row = await cur.fetchone()
            assert row is not None, "INSERT ... RETURNING id 未返回行"
            admin_id = row["id"]
    return {"id": admin_id, "username": username, "role": ROLE_ADMIN}


# ──────────────────────────────────────────────────────────
# 密码哈希
# ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


async def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """校验用户名/密码，成功返回用户信息，失败返回 None。"""
    user = await get_user_by_username(username.strip())
    if user and await asyncio.to_thread(verify_password, password, user["password_hash"]):
        return {"id": user["id"], "username": user["username"], "role": user["role"]}
    return None


# ──────────────────────────────────────────────────────────
# JWT 签发 / 校验
# ──────────────────────────────────────────────────────────

def create_access_token(username: str, role: str = ROLE_USER) -> str:
    """签发 JWT，payload 携带角色（user / admin / superadmin）。"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {"sub": username, "role": role, "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """解析令牌，返回完整 payload（含 sub / role）；无效/过期返回 None。"""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


# ──────────────────────────────────────────────────────────
# FastAPI 鉴权依赖
# ──────────────────────────────────────────────────────────

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_http_bearer),
) -> str:
    """从 Authorization: Bearer <token> 解析当前用户，返回用户名。"""
    if credentials is None:
        raise HTTPException(status_code=401, detail="未提供认证令牌")
    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status_code=401, detail="认证令牌无效或已过期")
    return payload["sub"]


# ──────────────────────────────────────────────────────────
# 管理员管理（仅超级管理员可调用）
# ──────────────────────────────────────────────────────────

async def list_admins() -> List[Dict[str, Any]]:
    """列出全部管理员（admin + superadmin），按 id 升序。"""
    await _ensure_users_table()
    async with async_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, username, role, created_at FROM users WHERE role IN (%s, %s) ORDER BY id",
                (ROLE_ADMIN, ROLE_SUPERADMIN),
            )
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def delete_admin(username: str) -> bool:
    """删除管理员账号（仅限 role=admin）。

    通过 role='admin' 条件天然保证 superadmin 不可被删除。
    返回是否实际删除了记录。
    """
    await _ensure_users_table()
    async with async_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM users WHERE username = %s AND role = %s RETURNING id",
                (username.strip(), ROLE_ADMIN),
            )
            row = await cur.fetchone()
    return row is not None
