"""任务运行态外置：内存 TaskRecord 旁路双写 Redis Hash。

Redis 不可用时静默降级，不打断任务。Key：task:status:{task_id}
进行中不设 TTL；结束后 EXPIRE TASK_REDIS_TTL_SEC。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from config.settings import REDIS_URL, TASK_REDIS_TTL_SEC

logger = logging.getLogger(__name__)

_redis = None
_redis_failed = False


def _json_dump(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


async def _client():
    """懒连 Redis；失败一次后本进程不再重试（避免每次状态更新打日志）。"""
    global _redis, _redis_failed
    if _redis_failed or not REDIS_URL:
        return None
    if _redis is not None:
        return _redis
    try:
        from redis.asyncio import Redis
        _redis = Redis.from_url(REDIS_URL, decode_responses=True)
        await _redis.ping()
        logger.info("✅ [task-state] Redis 已连接：%s", REDIS_URL)
        return _redis
    except Exception as e:
        _redis_failed = True
        _redis = None
        logger.warning("⚠️ [task-state] Redis 不可用，降级内存+PG：%s", e)
        return None


def _key(task_id: str) -> str:
    return f"task:status:{task_id}"


def snapshot_from_record(record: Any) -> Dict[str, str]:
    """把 TaskRecord 压成 Redis Hash 字符串字段。"""
    return {
        "task_id": record.task_id or "",
        "status": record.status or "",
        "current_node": record.current_node or "",
        "progress_message": record.progress_message or "",
        "result": _json_dump(record.result),
        "error": record.error or "",
        "created_at": str(record.created_at or ""),
        "started_at": str(record.started_at or ""),
        "finished_at": str(record.finished_at or ""),
        "user_id": record.user_id or "",
        "session_id": record.session_id or "",
        "user_query": record.user_query or "",
        "obs_task_id": record.obs_task_id or "",
    }


def hash_to_view(data: Dict[str, str]) -> Dict[str, Any]:
    """Redis Hash → 与 TaskRecord.snapshot() 对齐的任务视图。"""
    result = data.get("result") or ""
    parsed = None
    if result:
        try:
            parsed = json.loads(result)
        except Exception:
            parsed = result
    def _f(name: str) -> Optional[float]:
        raw = data.get(name) or ""
        if not raw:
            return None
        try:
            return float(raw)
        except Exception:
            return None
    return {
        "task_id": data.get("task_id") or "",
        "status": data.get("status") or "pending",
        "current_node": data.get("current_node") or None,
        "progress_message": data.get("progress_message") or None,
        "result": parsed,
        "error": data.get("error") or None,
        "created_at": _f("created_at"),
        "started_at": _f("started_at"),
        "finished_at": _f("finished_at"),
        "user_id": data.get("user_id") or "",
        "session_id": data.get("session_id") or "",
        "user_query": data.get("user_query") or "",
    }


async def upsert_record(record: Any, expire: bool = False) -> None:
    """把内存 TaskRecord 写入 Redis。expire=True 表示任务已结束，打 TTL。

    终态调用方应先落 PG（result_payload / obs 终态），再调本函数，避免 Redis 比库新、重启后回填出旧结果。
    """
    await upsert_view(snapshot_from_record(record), expire=expire)


async def upsert_view(mapping: Dict[str, str], expire: bool = False) -> None:
    """按 Hash 字段写入 Redis（供 PG 回填 / 终态回写）。"""
    task_id = mapping.get("task_id") or ""
    if not task_id:
        return
    r = await _client()
    if r is None:
        return
    try:
        key = _key(task_id)
        await r.hset(key, mapping=mapping)
        status = mapping.get("status") or ""
        if expire or status in ("succeeded", "failed"):
            await r.expire(key, TASK_REDIS_TTL_SEC)
        else:
            await r.persist(key)
    except Exception as e:
        logger.warning("⚠️ [task-state] 写入失败 task_id=%s: %s", task_id, e)


async def get_view(task_id: str) -> Optional[Dict[str, Any]]:
    r = await _client()
    if r is None:
        return None
    try:
        data = await r.hgetall(_key(task_id))
        if not data:
            return None
        return hash_to_view(data)
    except Exception as e:
        logger.warning("⚠️ [task-state] 读取失败 task_id=%s: %s", task_id, e)
        return None


async def ping() -> Dict[str, Any]:
    """供 /health。未配置 Redis 视为 skipped 而非失败。"""
    if not REDIS_URL:
        return {"ok": True, "skipped": True}
    r = await _client()
    if r is None:
        return {"ok": False, "error": "redis unavailable"}
    try:
        await r.ping()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def close() -> None:
    global _redis, _redis_failed
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception:
            pass
    _redis = None
    _redis_failed = False
