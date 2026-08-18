"""观测追踪：span 树模型（节点 / LLM / MCP 逐调用 span，开始占位 + 结束更新）。

设计要点：
- 任务开始：`start_task(task_id, ...)` 前置插入 obs_tasks（task_id=业务 uuid，无自增 id）。
- 节点 / LLM / MCP：调用开始先 INSERT 占位行（status=running，拿到 span 行 id），
  调用结束 UPDATE 补全 end_ts / 状态 / 指标。
- duration_ms = end_ts - start_ts，不冗余存储；seq 由 start_ts 推导。
- contextvars 保证 asyncio.gather 多城并发时各 span 归属正确的节点。
- 本模块使用 psycopg3 异步连接池（async_db_connection），须在异步上下文调用。
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import logging
import time
from typing import Any, Dict, Optional

from config.settings import AGENT_VERSION
from ._obs_storage import get_obs_storage

logger = logging.getLogger(__name__)

# 当前任务 id（业务 uuid）
_current_task_id: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "obs_current_task_id", default=None
)
# 当前节点名
_current_node_name: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "obs_current_node_name", default=None
)


# ──────────────────────────────────────────────────────────
# 任务生命周期
# ──────────────────────────────────────────────────────────

async def start_task(task_id: str, user_id: str = "", session_id: str = "",
                     user_query: str = "", intent: Optional[str] = None,
                     version: Optional[str] = None) -> str:
    """任务开始：前置插入 obs_tasks 占位行（task_id=业务 uuid），返回 task_id。

    调用方（core/service.py）传入业务 task_id（与 TaskRecord.task_id / checkpoint
    thread_id 一致），本函数不再自行生成。
    """
    ver = version or AGENT_VERSION
    await get_obs_storage().start_task(task_id, ver, user_id, session_id, user_query, intent=intent)
    _current_task_id.set(task_id)
    return task_id


async def end_task(status: str = "ok", error: Optional[str] = None):
    """结束当前任务：UPDATE obs_tasks 补全 end_ts / 状态。"""
    task_id = _current_task_id.get()
    if not task_id:
        return
    await get_obs_storage().end_task(task_id, status, error, summary={})
    _current_task_id.set(None)
    _current_node_name.set(None)


def reset_observability():
    """清空当前执行上下文（每个任务开始前调用）。"""
    _current_task_id.set(None)
    _current_node_name.set(None)


async def record_query_type(query_type: str):
    """记录 classify 节点的实际分类结果（写入 obs_tasks.query_type，供与标注意图对比）。"""
    task_id = _current_task_id.get()
    if not task_id:
        return
    try:
        await get_obs_storage().update_task_query_type(task_id, query_type)
    except Exception as e:
        logger.warning(f"⚠️ 记录 query_type 失败: {e}")


# ──────────────────────────────────────────────────────────
# 节点 span（async 成对：start_node / end_node）
# ──────────────────────────────────────────────────────────

async def start_node(name: str) -> int:
    """节点调用开始：插入 node span 占位行，返回该 span 行 id。"""
    task_id = _current_task_id.get()
    if not task_id:
        return 0
    _current_node_name.set(name)
    span_id = await get_obs_storage().start_node_span(task_id, name)
    return span_id


async def end_node(span_id: int, status: str = "ok", error: Optional[str] = None):
    """节点调用结束：UPDATE node span 补全 end_ts / 状态。"""
    if span_id:
        await get_obs_storage().end_node_span(span_id, status, error)


# ──────────────────────────────────────────────────────────
# LLM span（async 成对：start_llm / end_llm）
# ──────────────────────────────────────────────────────────

async def start_llm(agent: str) -> int:
    """LLM 调用开始：插入 llm span 占位行，返回该 span 行 id。

    流式调用同样只在此占位，流式过程中不写库，结束才 end_llm。
    """
    task_id = _current_task_id.get()
    if not task_id:
        return 0
    node = _current_node_name.get() or ""
    span_id = await get_obs_storage().start_llm_span(task_id, node, agent)
    return span_id


async def end_llm(span_id: int, status: str = "ok", error: Optional[str] = None,
                  input_tokens: int = 0, output_tokens: int = 0,
                  cached_tokens: int = 0, output: str = "") -> None:
    """LLM 调用结束：UPDATE llm span 补全 end_ts / token / output / 状态。"""
    if span_id:
        await get_obs_storage().end_llm_span(
            span_id, status, error,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cached_tokens=cached_tokens, output=output,
        )


# ──────────────────────────────────────────────────────────
# MCP span（async 成对：start_mcp / end_mcp）
# ──────────────────────────────────────────────────────────

async def start_mcp(server: str, tool: str) -> int:
    """MCP 调用开始：插入 mcp span 占位行，返回该 span 行 id。"""
    task_id = _current_task_id.get()
    if not task_id:
        return 0
    node = _current_node_name.get() or ""
    span_id = await get_obs_storage().start_mcp_span(task_id, node, server, tool)
    return span_id


async def end_mcp(span_id: int, status: str = "ok", error: Optional[str] = None,
                  retries: int = 0) -> None:
    """MCP 调用结束：UPDATE mcp span 补全 end_ts / retries / 状态。"""
    if span_id:
        await get_obs_storage().end_mcp_span(span_id, status, error, retries=retries)


# ──────────────────────────────────────────────────────────
# 节点装饰器 / 作用域（async 封装，供各 agent 节点使用）
# ──────────────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def node_scope(name: str):
    """为并发子图/子任务建立独立节点观测上下文（async context manager）。

    用法：async with node_scope("city_plan"): ...
    """
    task_id = _current_task_id.get()
    span_id = 0
    if task_id:
        span_id = await get_obs_storage().start_node_span(task_id, name)
    prev_node = _current_node_name.get()
    _current_node_name.set(name)
    try:
        yield
    except Exception as e:
        if span_id:
            await get_obs_storage().end_node_span(span_id, "error", str(e))
        raise
    else:
        if span_id:
            await get_obs_storage().end_node_span(span_id, "ok", None)
    finally:
        if prev_node:
            _current_node_name.set(prev_node)
        else:
            _current_node_name.set(None)


def node(name: str):
    """节点装饰器：设置当前节点上下文 + 记录 node span（开始占位 / 结束补全）。"""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(state, *args, **kwargs):
            task_id = _current_task_id.get()
            span_id = 0
            if task_id:
                span_id = await get_obs_storage().start_node_span(task_id, name)
            prev_node = _current_node_name.get()
            _current_node_name.set(name)
            try:
                result = await fn(state, *args, **kwargs)
                if span_id:
                    await get_obs_storage().end_node_span(span_id, "ok", None)
                return result
            except Exception as e:
                if span_id:
                    await get_obs_storage().end_node_span(span_id, "error", str(e))
                raise
            finally:
                if prev_node:
                    _current_node_name.set(prev_node)
                else:
                    _current_node_name.set(None)
        return wrapper
    return deco


# ──────────────────────────────────────────────────────────
# 读时组装：obs_tasks + 三张 span 表 → 任务 trace JSON
# ──────────────────────────────────────────────────────────

async def build_task_json(task_id: str) -> Dict[str, Any]:
    """从 DB 读取任务 + 三张 span 表，组装成任务 trace JSON。

    返回结构：
    {
      "task_id": str,
      "summary": { node_count / llm_call_count / tool_call_count / tokens / duration_ms ... },
      "nodes": [
        { "node": str, "status": str, "error": str, "duration_ms": float,
          "llm": [ {agent, model(反查), input_tokens, output_tokens, cached_tokens, output, duration_ms, status} ],
          "tools": [ {server, tool, duration_ms, retries, status} ] },
        ...
      ],
      "model_map": { agent: model }   # 模型名按 agent 从配置反查
    }
    """
    storage = get_obs_storage()
    task = await storage.get_task(task_id)
    nodes = await storage.get_node_spans(task_id)
    llms = await storage.get_llm_spans(task_id)
    mcps = await storage.get_mcp_spans(task_id)

    empty = {
        "node_count": 0, "llm_call_count": 0, "tool_call_count": 0,
        "total_input_tokens": 0, "total_output_tokens": 0, "total_tokens": 0,
        "cached_input_tokens": 0, "cache_hit_rate": None,
        "llm_duration_ms": 0.0, "tool_duration_ms": 0.0, "wall_clock_ms": 0.0,
        "status": "unknown", "version": "",
    }
    if task is None:
        return {"task_id": task_id, "summary": empty, "nodes": [], "model_map": {}}

    # 按 node 分组 llm/mcp
    llm_by_node: Dict[str, list] = {}
    mcp_by_node: Dict[str, list] = {}
    for l in llms:
        llm_by_node.setdefault(l.get("node") or "", []).append(l)
    for m in mcps:
        mcp_by_node.setdefault(m.get("node") or "", []).append(m)

    node_out = []
    total_in = total_out = total_cached = 0
    llm_dur = tool_dur = 0.0
    for n in nodes:
        name = n.get("node") or "?"
        child_llm = llm_by_node.get(name, [])
        child_mcp = mcp_by_node.get(name, [])
        for l in child_llm:
            total_in += int(l.get("input_tokens") or 0)
            total_out += int(l.get("output_tokens") or 0)
            total_cached += int(l.get("cached_tokens") or 0)
            llm_dur += _dur_ms(l)
        for m in child_mcp:
            tool_dur += _dur_ms(m)
        node_out.append({
            "node": name,
            "status": n.get("status") or "ok",
            "error": n.get("error"),
            "duration_ms": _dur_ms(n),
            "llm": [_llm_item(l) for l in child_llm],
            "tools": [_mcp_item(m) for m in child_mcp],
        })

    node_out.sort(key=lambda x: x["duration_ms"], reverse=True)

    # 模型名反查
    from config.settings import get_agent_model, QWEN3_MODEL, DS_FLASH_MODEL
    model_map = {}
    for l in llms:
        agent = l.get("agent") or ""
        if agent and agent not in model_map:
            model_map[agent] = get_agent_model(agent, QWEN3_MODEL if agent not in ("summarizer", "json_fix", "hotel_price") else DS_FLASH_MODEL)

    summary = {
        "node_count": len(nodes),
        "llm_call_count": len(llms),
        "tool_call_count": len(mcps),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_tokens": total_in + total_out,
        "cached_input_tokens": total_cached,
        "cache_hit_rate": round(total_cached / total_in, 4) if total_in > 0 else None,
        "llm_duration_ms": round(llm_dur, 2),
        "tool_duration_ms": round(tool_dur, 2),
        "wall_clock_ms": _dur_ms(task),
        "status": task.get("status") or "unknown",
        "version": task.get("version") or "",
        "intent": task.get("intent") or "",
        "query_type": task.get("query_type") or "",
        "error": task.get("error"),
    }

    return {"task_id": task_id, "summary": summary, "nodes": node_out, "model_map": model_map}


def _dur_ms(row: Dict[str, Any]) -> float:
    """从 start_ts/end_ts 推导耗时（毫秒）；缺失返回 0。"""
    try:
        s = float(row.get("start_ts") or 0)
        e = float(row.get("end_ts") or 0)
        if s and e:
            return round((e - s) * 1000, 2)
    except Exception:
        pass
    return 0.0


def _llm_item(l: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "agent": l.get("agent"),
        "input_tokens": int(l.get("input_tokens") or 0),
        "output_tokens": int(l.get("output_tokens") or 0),
        "cached_tokens": int(l.get("cached_tokens") or 0),
        "output": l.get("output"),
        "duration_ms": _dur_ms(l),
        "status": l.get("status") or "ok",
        "error": l.get("error"),
    }


def _mcp_item(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "server": m.get("server"),
        "tool": m.get("tool"),
        "duration_ms": _dur_ms(m),
        "retries": int(m.get("retries") or 0),
        "status": m.get("status") or "ok",
        "error": m.get("error"),
    }
