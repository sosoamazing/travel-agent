"""API 网关服务：对外提供 REST / SSE 接口，JWT 鉴权后转发到 backend 内部服务。

职责：
- JWT 本地鉴权（与 backend 共享 JWT_SECRET）
- 转发业务请求到 backend（/internal/*）
- SSE 流式透传任务执行过程（节点进度 + LLM token + 最终结果）

运行（在项目目录 travel-agent/travel-agent 下）：
    uvicorn gateway.main:app --host 0.0.0.0 --port 8000

配置（.env，与 backend 共享同一份）：
- BACKEND_URL：backend 内部服务地址，默认 http://127.0.0.1:8001
- MONITOR_URL：monitor 监控服务地址，默认 http://127.0.0.1:8002
- JWT_SECRET：与 backend 一致的签名密钥
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

# Windows 中文控制台默认 GBK 编码，无法编码 emoji 会导致 print 抛 UnicodeEncodeError。
# 统一把 stdout/stderr 切到 UTF-8，避免启动/运行期崩溃。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

# 保证无论从哪个 cwd 启动，都能以项目目录为根导入 gateway 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gateway.schemas import (
    AdminCreateRequest,
    ChatRequest,
    ChatResponse,
    LoginRequest,
    MessageCreate,
    RegisterRequest,
    SessionCreate,
    SessionCreateResponse,
    TokenResponse,
    UserResponse,
)

# ── 配置 ──────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=True)

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8001").rstrip("/")
MONITOR_URL = os.getenv("MONITOR_URL", "http://127.0.0.1:8002").rstrip("/")
JWT_SECRET = os.getenv("JWT_SECRET", "travel-agent-dev-secret-change-me")
JWT_ALGORITHM = "HS256"

_http_bearer = HTTPBearer(auto_error=False)

_client: Optional[httpx.AsyncClient] = None
_monitor_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, _monitor_client
    _client = httpx.AsyncClient(
        base_url=BACKEND_URL,
        timeout=httpx.Timeout(600.0, connect=10.0),
    )
    # 只读监控服务客户端（代理后由管理员鉴权）
    _monitor_client = httpx.AsyncClient(
        base_url=MONITOR_URL,
        timeout=httpx.Timeout(60.0, connect=10.0),
    )
    yield
    if _client is not None:
        await _client.aclose()
    if _monitor_client is not None:
        await _monitor_client.aclose()


app = FastAPI(title="Travel Agent Gateway", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 鉴权 / 错误处理 ────────────────────────────────────

def _require_client() -> httpx.AsyncClient:
    if _client is None:
        raise HTTPException(status_code=503, detail="backend 客户端未初始化")
    return _client


def _require_monitor_client() -> httpx.AsyncClient:
    if _monitor_client is None:
        raise HTTPException(status_code=503, detail="monitor 客户端未初始化")
    return _monitor_client


def _raise_backend_error(r: httpx.Response) -> None:
    """把 backend 的非 2xx 响应转成 HTTPException。"""
    detail: Any = r.text
    try:
        data = r.json()
        detail = data.get("detail", r.text)
    except Exception:
        pass
    raise HTTPException(status_code=r.status_code, detail=detail)


def _decode_token(token: str) -> Optional[Dict[str, Any]]:
    """本地解码 JWT；解析失败返回 None。"""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_http_bearer),
) -> str:
    """本地解码 JWT，返回用户名（不额外调用 backend）。"""
    if credentials is None:
        raise HTTPException(status_code=401, detail="未提供认证令牌")
    payload = _decode_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status_code=401, detail="认证令牌无效或已过期")
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="认证令牌无效或已过期")
    return username


async def get_current_admin(
    credentials: HTTPAuthorizationCredentials = Depends(_http_bearer),
) -> str:
    """管理员鉴权：要求角色为 admin / superadmin，返回用户名。"""
    if credentials is None:
        raise HTTPException(status_code=401, detail="未提供认证令牌")
    payload = _decode_token(credentials.credentials)
    if payload is None or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="认证令牌无效或已过期")
    if payload.get("role") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return payload["sub"]


async def get_current_superadmin(
    credentials: HTTPAuthorizationCredentials = Depends(_http_bearer),
) -> str:
    """超级管理员鉴权：要求角色为 superadmin，返回用户名。"""
    if credentials is None:
        raise HTTPException(status_code=401, detail="未提供认证令牌")
    payload = _decode_token(credentials.credentials)
    if payload is None or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="认证令牌无效或已过期")
    if payload.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="需要超级管理员权限")
    return payload["sub"]


# ── 认证路由（转发） ───────────────────────────────────

@app.post("/auth/register", response_model=UserResponse)
async def register(req: RegisterRequest) -> UserResponse:
    r = await _require_client().post("/internal/auth/register", json=req.model_dump())
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.post("/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest) -> TokenResponse:
    r = await _require_client().post("/internal/auth/login", json=req.model_dump())
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.get("/auth/me", response_model=UserResponse)
async def me(username: str = Depends(get_current_user)) -> UserResponse:
    r = await _require_client().get("/internal/auth/me", params={"username": username})
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


# ── 管理员认证 / 用户管理（转发） ──────────────────────

@app.post("/auth/admin/login", response_model=TokenResponse)
async def admin_login(req: LoginRequest) -> TokenResponse:
    r = await _require_client().post("/internal/auth/admin/login", json=req.model_dump())
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.get("/auth/admin/users", response_model=List[Dict[str, Any]])
async def admin_list_users(
    _: str = Depends(get_current_superadmin),
) -> List[Dict[str, Any]]:
    r = await _require_client().get("/internal/auth/admin/list")
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.post("/auth/admin/users", response_model=UserResponse)
async def admin_create_user(
    req: AdminCreateRequest,
    _: str = Depends(get_current_superadmin),
) -> UserResponse:
    r = await _require_client().post(
        "/internal/auth/admin/create", json=req.model_dump()
    )
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.delete("/auth/admin/users/{username}", response_model=Dict[str, Any])
async def admin_delete_user(
    username: str,
    _: str = Depends(get_current_superadmin),
) -> Dict[str, Any]:
    r = await _require_client().delete(f"/internal/auth/admin/{username}")
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


# ── 健康检查（公开） ───────────────────────────────────

@app.get("/health")
async def health() -> Dict[str, Any]:
    r = await _require_client().get("/internal/health")
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


# ── 监控代理（管理员只读，转发到 monitor） ───────────────

@app.get("/admin/report")
async def admin_report(
    version: Optional[str] = Query(None),
    _: str = Depends(get_current_admin),
) -> Dict[str, Any]:
    r = await _require_monitor_client().get("/report", params={"version": version})
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.get("/admin/tasks")
async def admin_tasks(
    version: Optional[str] = Query(None),
    _: str = Depends(get_current_admin),
    limit: int = Query(50, ge=1, le=1000),
) -> List[Dict[str, Any]]:
    r = await _require_monitor_client().get(
        "/tasks", params={"limit": limit, "version": version}
    )
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.get("/admin/versions")
async def admin_versions(_: str = Depends(get_current_admin)) -> List[str]:
    r = await _require_monitor_client().get("/versions")
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.get("/admin/releases")
async def admin_releases(
    _: str = Depends(get_current_admin),
    limit: int = Query(50, ge=1, le=200),
) -> List[Dict[str, Any]]:
    r = await _require_monitor_client().get("/releases", params={"limit": limit})
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.get("/admin/report/text")
async def admin_report_text(_: str = Depends(get_current_admin)) -> Response:
    r = await _require_monitor_client().get("/report/text")
    if r.status_code >= 400:
        _raise_backend_error(r)
    return Response(content=r.text, media_type="text/plain")


# ── 对话任务 ───────────────────────────────────────────

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, username: str = Depends(get_current_user)) -> ChatResponse:
    r = await _require_client().post(
        "/internal/chat",
        json={"user_query": req.user_query, "session_id": req.session_id, "user_id": username, "intent": req.intent},
    )
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.get("/tasks/{task_id}")
async def get_task(task_id: str, username: str = Depends(get_current_user)) -> Dict[str, Any]:
    r = await _require_client().get(f"/internal/tasks/{task_id}", params={"user_id": username})
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.get("/tasks/{task_id}/events")
async def get_task_events(
    task_id: str,
    username: str = Depends(get_current_user),
    since_ts: float = Query(0.0),
    include_payload: bool = Query(False),
) -> Dict[str, Any]:
    r = await _require_client().get(
        f"/internal/tasks/{task_id}/events",
        params={"user_id": username, "since_ts": since_ts, "include_payload": include_payload},
    )
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str, username: str = Depends(get_current_user)) -> Dict[str, Any]:
    r = await _require_client().post(
        f"/internal/tasks/{task_id}/resume", json={"user_id": username}
    )
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str, username: str = Depends(get_current_user)) -> StreamingResponse:
    # 先校验归属（非流式），失败直接返回错误
    check = await _require_client().get(
        f"/internal/tasks/{task_id}", params={"user_id": username}
    )
    if check.status_code >= 400:
        _raise_backend_error(check)

    stream_url = f"{BACKEND_URL}/internal/tasks/{task_id}/stream?user_id={username}"

    async def event_generator() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
            async with client.stream("GET", stream_url) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 观测追踪（仅管理员可访问；管理员可查看白名单内测试用户任务）──

@app.get("/obs/{task_id}")
async def obs(task_id: str, _: str = Depends(get_current_admin)) -> Dict[str, Any]:
    r = await _require_client().get(
        f"/internal/obs/{task_id}",
        params={"is_admin": "true", "user_id": "admin"},
    )
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.get("/obs/{task_id}/trace")
async def obs_trace(task_id: str, _: str = Depends(get_current_admin)) -> Dict[str, Any]:
    r = await _require_client().get(
        f"/internal/obs/{task_id}/trace",
        params={"is_admin": "true", "user_id": "admin"},
    )
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


# ── 会话 / 聊天历史 ────────────────────────────────────

@app.get("/sessions")
async def list_sessions(
    username: str = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=200),
) -> List[Dict[str, Any]]:
    r = await _require_client().get(
        "/internal/sessions", params={"user_id": username, "limit": limit}
    )
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.post("/sessions", response_model=SessionCreateResponse)
async def create_session(
    req: SessionCreate,
    username: str = Depends(get_current_user),
) -> SessionCreateResponse:
    r = await _require_client().post(
        "/internal/sessions", json={"user_id": username, "title": req.title}
    )
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.get("/sessions/{session_id}/messages")
async def get_messages(
    session_id: str,
    username: str = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    r = await _require_client().get(
        f"/internal/sessions/{session_id}/messages", params={"user_id": username}
    )
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.post("/sessions/{session_id}/messages")
async def add_message(
    session_id: str,
    req: MessageCreate,
    username: str = Depends(get_current_user),
) -> Dict[str, Any]:
    r = await _require_client().post(
        f"/internal/sessions/{session_id}/messages",
        json={"message_type": req.message_type, "content": req.content, "user_id": username},
    )
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str, username: str = Depends(get_current_user)) -> Dict[str, Any]:
    r = await _require_client().delete(f"/internal/sessions/{session_id}", params={"user_id": username})
    if r.status_code >= 400:
        _raise_backend_error(r)
    return r.json()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("gateway.main:app", host="0.0.0.0", port=8000, reload=False)
