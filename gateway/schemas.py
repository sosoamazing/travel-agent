"""API 网关对外请求/响应数据模型（Pydantic）。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """POST /chat 请求体。"""
    user_query: str = Field(..., description="用户本轮输入")
    session_id: Optional[str] = Field(None, description="会话 ID；为空则自动新建会话")
    user_id: Optional[str] = Field("default_user", description="用户 ID（网关会以 JWT 覆盖）")
    intent: Optional[str] = Field(None, description="用户意图（可发可不发）：planning/information/conversation/feedback")


class ChatResponse(BaseModel):
    """POST /chat 响应体：立即返回任务 ID，后台异步执行。"""
    task_id: str


class SessionCreate(BaseModel):
    """创建会话请求体。"""
    user_id: Optional[str] = Field("default_user")
    title: Optional[str] = None


class SessionCreateResponse(BaseModel):
    session_id: str


class MessageCreate(BaseModel):
    """追加一条消息请求体。"""
    message_type: str = Field(..., description="user | ai")
    content: str
    user_id: Optional[str] = Field("default_user")


# ── 认证 ──────────────────────────────────────────────

class RegisterRequest(BaseModel):
    """注册请求体。"""
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(..., min_length=6, max_length=128)


class LoginRequest(BaseModel):
    """登录请求体。"""
    username: str = Field(..., min_length=1, max_length=32)
    password: str = Field(..., min_length=1, max_length=128)


class TokenResponse(BaseModel):
    """登录成功返回的令牌。"""
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str


class UserResponse(BaseModel):
    """用户信息。"""
    id: int
    username: str
    role: str


class AdminCreateRequest(BaseModel):
    """创建管理员请求体（与注册同规则校验）。"""
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(..., min_length=6, max_length=128)
