"""backend 内部服务：把业务服务 TravelService 包装成 HTTP 接口（供 gateway 调用）。

职责：
- 持有业务服务单例（get_service()），复用现有 core/service.py 的全部能力
- 暴露 /internal/* 接口；session / task 归属校验在此层完成
- SSE 流式透传任务执行过程（节点进度 + LLM token + 最终结果）

安全说明：
- 本服务不做 JWT 鉴权（由 gateway 完成），仅按 user_id 做资源归属校验。
- 生产环境应部署在内网、不对外暴露；如需更强隔离可在 .env 增加内部令牌。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager

# Windows 中文控制台默认 GBK 编码，无法编码 emoji（✅/⚠️ 等）会导致 print 抛
# UnicodeEncodeError，使服务启动崩溃。这里把 stdout/stderr 强制切到 UTF-8。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
from typing import Any, AsyncIterator, Dict, List, Optional

# Windows 上 psycopg3 的异步实现依赖 SelectorEventLoop（需要 add_reader），而 Python
# 默认使用 ProactorEventLoop（不支持 add_reader），会导致 LangGraph Postgres checkpointer
# 初始化失败。必须在 uvicorn 调用 asyncio.run() 之前设置。
if sys.platform == "win32":
    import selectors as _selectors

    class _SelectorLoopPolicy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self):
            return asyncio.SelectorEventLoop(_selectors.SelectSelector())

    asyncio.set_event_loop_policy(_SelectorLoopPolicy())

    try:
        import uvicorn.loops.asyncio as _uv_asyncio

        _uv_asyncio.asyncio_loop_factory = (
            lambda use_subprocess=False: asyncio.SelectorEventLoop
        )
    except Exception:
        pass

# 保证无论从哪个 cwd 启动，都能以 backend 为包根导入 core / config 等
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from auth import (
    authenticate_user,
    create_access_token,
    create_admin_user,
    delete_admin,
    get_user_by_username,
    is_admin_role,
    list_admins,
    register_user,
)
from core.service import TaskRecord, get_service


# ──────────────────────────────────────────────────────────
# 内部请求模型
# ──────────────────────────────────────────────────────────

class _ChatReq(BaseModel):
    user_query: str
    session_id: Optional[str] = None
    user_id: str = "default_user"
    intent: Optional[str] = None


class _SessionReq(BaseModel):
    user_id: str = "default_user"
    title: Optional[str] = None


class _MessageReq(BaseModel):
    message_type: str = Field(..., description="user | ai")
    content: str
    user_id: str = "default_user"


class _ResumeReq(BaseModel):
    user_id: str = "default_user"


class _RegisterReq(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(..., min_length=6, max_length=128)


class _LoginReq(BaseModel):
    username: str = Field(..., min_length=1, max_length=32)
    password: str = Field(..., min_length=1, max_length=128)


class _AdminCreateReq(BaseModel):
    """创建管理员请求（校验规则与注册相同）。"""
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(..., min_length=6, max_length=128)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时预热业务层、初始化 LangGraph Postgres checkpointer，并预热 DB 连接池。"""
    # 预热 DB 连接池（原为懒加载，首个请求才建池；提前建好避免首个请求冷启动慢）
    try:
        from db import _ensure_async_pool
        await _ensure_async_pool()
    except Exception as e:
        print(f"⚠️ [DB] 连接池预热失败（{e}）")
    service = get_service()
    await service.setup()
    yield
    await service.close()


app = FastAPI(title="Travel Agent Backend Internal", version="1.0.0", lifespan=lifespan)


# ──────────────────────────────────────────────────────────
# 归属校验辅助
# ──────────────────────────────────────────────────────────

async def _require_session_owner(session_id: str, user_id: str) -> None:
    """校验会话存在且属于指定 user_id。"""
    from db import async_db_connection
    async with async_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT user_id FROM chat_sessions WHERE session_id = %s", (session_id,))
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if row["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")


def _get_owned_task(task_id: str, user_id: str, is_admin: bool = False) -> TaskRecord:
    """按 task_id 取任务记录并校验访问权限。

    放行规则（obs/trace 仅管理员可访问）：
    - 调用方为管理员（is_admin=True）且任务归属用户在白名单（OBS_ADMIN_USER_IDS）内 → 放行；
    - 否则要求任务归属用户与 user_id 一致。
    """
    from config.settings import OBS_ADMIN_USER_IDS
    record = get_service().tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="task not found")
    if is_admin and record.user_id in OBS_ADMIN_USER_IDS:
        return record
    if record.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")
    return record


async def _authorize_obs_access(task_id: str, user_id: str, is_admin: bool) -> None:
    """校验观测详情访问权限（仅管理员可访问，管理员可查看白名单内用户任务）。

    与 _get_owned_task 的区别：内存 tasks（get_service().tasks）重启后会被清空，
    而 obs 数据本身持久化在 obs_tasks 表。因此内存查不到时回退查 obs_tasks 表，
    保证 backend 重启后管理员仍能查询历史任务的观测详情。
    """
    from config.settings import OBS_ADMIN_USER_IDS
    # 1) 优先内存 TaskRecord（最新状态）
    record = get_service().tasks.get(task_id)
    if record is not None:
        owner = record.user_id
    else:
        # 2) 回退 obs_tasks 表（backend 重启后内存清空，观测数据仍持久化在表里）
        from agent_nodes._obs_storage import get_obs_storage
        obs_row = await get_obs_storage().get_task(task_id)
        if obs_row is None:
            raise HTTPException(status_code=404, detail="task not found")
        owner = obs_row.get("user_id") or ""
    # 权限判断：管理员可看白名单内用户任务；否则仅本人
    if is_admin and owner in OBS_ADMIN_USER_IDS:
        return
    if owner != user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")


# ──────────────────────────────────────────────────────────
# 认证（内部接口，供 gateway 转发）
# ──────────────────────────────────────────────────────────

@app.post("/internal/auth/register")
async def register(req: _RegisterReq) -> Dict[str, Any]:
    return await register_user(req.username, req.password)


@app.post("/internal/auth/login")
async def login(req: _LoginReq) -> Dict[str, Any]:
    user = await authenticate_user(req.username, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {
        "access_token": create_access_token(user["username"], user["role"]),
        "token_type": "bearer",
        "username": user["username"],
        "role": user["role"],
    }


@app.get("/internal/auth/me")
async def me(username: str = Query(...)) -> Dict[str, Any]:
    user = await get_user_by_username(username)
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


# ──────────────────────────────────────────────────────────
# 管理员管理（admin / superadmin；鉴权由调用方 gateway 负责）
# ──────────────────────────────────────────────────────────

@app.post("/internal/auth/admin/login")
async def admin_login(req: _LoginReq) -> Dict[str, Any]:
    """管理员登录：仅 admin / superadmin 角色可成功登录。"""
    user = await authenticate_user(req.username, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not is_admin_role(user["role"]):
        raise HTTPException(status_code=403, detail="非管理员账号")
    return {
        "access_token": create_access_token(user["username"], user["role"]),
        "token_type": "bearer",
        "username": user["username"],
        "role": user["role"],
    }


@app.post("/internal/auth/admin/create")
async def admin_create(req: _AdminCreateReq) -> Dict[str, Any]:
    """创建管理员账号（role=admin），仅应由超级管理员调用。"""
    return await create_admin_user(req.username, req.password)


@app.get("/internal/auth/admin/list")
async def admin_list() -> List[Dict[str, Any]]:
    """查看全部管理员（admin + superadmin）。"""
    return await list_admins()


@app.delete("/internal/auth/admin/{username}")
async def admin_delete(username: str) -> Dict[str, Any]:
    """删除管理员账号；superadmin 不会被删除（此时返回 404）。"""
    deleted = await delete_admin(username)
    if not deleted:
        raise HTTPException(status_code=404, detail="管理员不存在或无权删除")
    return {"deleted": username}


# ──────────────────────────────────────────────────────────
# 健康检查
# ──────────────────────────────────────────────────────────

@app.get("/internal/health")
async def health() -> Dict[str, Any]:
    return await get_service().health()


# ──────────────────────────────────────────────────────────
# 对话任务
# ──────────────────────────────────────────────────────────

@app.post("/internal/chat")
async def chat(req: _ChatReq) -> Dict[str, Any]:
    """提交对话：立即返回 task_id，后台异步执行 LangGraph 工作流。"""
    if req.session_id:
        await _require_session_owner(req.session_id, req.user_id)
    try:
        task_id = await get_service().submit_chat(
            user_query=req.user_query,
            session_id=req.session_id,
            user_id=req.user_id,
            intent=req.intent,
        )
    except RuntimeError as e:
        # 同会话有进行中的任务 → 409，前端提示等待
        raise HTTPException(status_code=409, detail=str(e))
    return {"task_id": task_id}


@app.get("/internal/tasks/{task_id}")
async def get_task(task_id: str, user_id: str = Query("default_user")) -> Dict[str, Any]:
    """轮询任务状态 + 进度 + 最终结果。

    读路径：内存 TaskRecord → Redis Hash → obs_tasks（含 result_payload）。
    """
    data = await get_service().get_task_view(task_id)
    if data is None:
        raise HTTPException(status_code=404, detail="task not found")
    if (data.get("user_id") or "") != user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")
    return data


@app.get("/internal/tasks/{task_id}/events")
async def get_task_events(
    task_id: str,
    user_id: str = Query("default_user"),
    since_ts: float = Query(0.0),
    include_payload: bool = Query(False),
) -> Dict[str, Any]:
    """增量拉取某任务 start_ts > since_ts 的 span（user_id 只做归属校验）。"""
    data = await get_service().get_task_view(task_id)
    if data is None:
        raise HTTPException(status_code=404, detail="task not found")
    if (data.get("user_id") or "") != user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")
    return await get_service().get_task_events(
        task_id, since_ts=since_ts, include_payload=include_payload,
    )


@app.post("/internal/tasks/{task_id}/resume")
async def resume_task(task_id: str, req: _ResumeReq) -> Dict[str, Any]:
    """断点续跑：同一 task_id（=thread_id）从最近 checkpoint 恢复执行。"""
    _get_owned_task(task_id, req.user_id)
    try:
        task_id = await get_service().resume_task(task_id)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"task_id": task_id, "status": "running"}


@app.get("/internal/tasks/{task_id}/stream")
async def stream_task(task_id: str, user_id: str = Query("default_user")) -> StreamingResponse:
    """SSE 流式输出：节点进度 + LLM token + 最终结果。"""
    record = _get_owned_task(task_id, user_id)

    async def event_generator() -> AsyncIterator[str]:
        try:
            while True:
                item = await record.queue.get()
                if item is None:  # 结束哨兵
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"
        except (GeneratorExit, asyncio.CancelledError):
            return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ──────────────────────────────────────────────────────────
# 观测追踪
# ──────────────────────────────────────────────────────────

@app.get("/internal/obs/{task_id}")
async def obs(task_id: str, user_id: str = Query("default_user"),
              is_admin: bool = Query(False)) -> Dict[str, Any]:
    """查询某业务 task 对应的观测追踪详情（仅管理员可访问）。"""
    await _authorize_obs_access(task_id, user_id, is_admin)
    data = await get_service().get_obs(task_id)
    if data is None:
        raise HTTPException(status_code=404, detail="observation not found")
    return data


@app.get("/internal/obs/{task_id}/trace")
async def obs_trace(task_id: str, user_id: str = Query("default_user"),
                    is_admin: bool = Query(False)) -> Dict[str, Any]:
    """查询某业务 task 对应的完整 span 树 trace（格式 A 点分路径 + 格式 B 大 JSON；仅管理员可访问）。"""
    await _authorize_obs_access(task_id, user_id, is_admin)
    data = await get_service().get_obs_trace(task_id)
    if data is None:
        raise HTTPException(status_code=404, detail="observation not found")
    return data


# ──────────────────────────────────────────────────────────
# 会话 / 聊天历史
# ──────────────────────────────────────────────────────────

@app.get("/internal/sessions")
async def list_sessions(
    user_id: str = Query("default_user"),
    limit: int = Query(50, ge=1, le=200),
) -> List[Dict[str, Any]]:
    return await get_service().list_sessions(user_id=user_id, limit=limit)


@app.post("/internal/sessions")
async def create_session(req: _SessionReq) -> Dict[str, Any]:
    session_id = await get_service().create_session(user_id=req.user_id, title=req.title)
    return {"session_id": session_id}


@app.get("/internal/sessions/{session_id}/messages")
async def get_messages(session_id: str, user_id: str = Query("default_user")) -> List[Dict[str, Any]]:
    await _require_session_owner(session_id, user_id)
    return await get_service().get_messages(session_id)


@app.post("/internal/sessions/{session_id}/messages")
async def add_message(session_id: str, req: _MessageReq) -> Dict[str, Any]:
    await _require_session_owner(session_id, req.user_id)
    message_id = await get_service().add_message(
        session_id=session_id,
        message_type=req.message_type,
        content=req.content,
        user_id=req.user_id,
    )
    return {"id": message_id}


@app.delete("/internal/sessions/{session_id}")
async def delete_session(session_id: str, user_id: str = Query("default_user")) -> Dict[str, Any]:
    """删除会话（含全部消息），校验归属。"""
    await _require_session_owner(session_id, user_id)
    await get_service().delete_session(session_id)
    return {"deleted": session_id}


if __name__ == "__main__":
    import uvicorn

    # Windows 需要 loop="none" 以使用本文件顶层设置的 SelectorEventLoop 策略
    # 仅绑定 127.0.0.1（本机）：backend 只允许本机 gateway 访问，避免外部直连 8001
    # 伪造 user_id / is_admin 参数绕过网关 JWT 鉴权（两层鉴权依赖“backend 不可外部直达”）。
    uvicorn.run("server:app", host="127.0.0.1", port=8001, loop="none", reload=False)
