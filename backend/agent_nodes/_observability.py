"""观测追踪：任务 / 节点 / LLM / MCP 调用聚合为「平均值」后持久化到 PostgreSQL。

设计要点：
- 执行期间用 contextvars + 内存累加器按维度聚合，end_task 时批量落库。
- 版本号：每次任务记录当前代码版本（AGENT_VERSION，来自 git 短 commit / 环境变量）。
- 聚合维度：LLM 按 (node × agent × model) 存平均 token / 平均耗时；MCP 按 (node × server × tool) 存平均耗时。
- contextvars 保证 asyncio.gather 多城并发时各条调用仍归属正确的节点。
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from config.settings import AGENT_VERSION
from ._obs_storage import get_obs_storage

logger = logging.getLogger(__name__)

# 当前任务 id
_current_task_id: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "obs_current_task_id", default=None
)
# 当前节点名
_current_node_name: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "obs_current_node_name", default=None
)

# 每个 task 的内存累加器（单进程单事件循环线程，key=task_id 安全）
_accumulators: Dict[str, Dict[str, Any]] = {}


def _acc(task_id: str) -> Dict[str, Any]:
    acc = _accumulators.get(task_id)
    if acc is None:
        acc = {
            "version": AGENT_VERSION,
            "user_id": "", "session_id": "", "user_query": "",
            "nodes": {},
            "llm": {},
            "mcp": {},
        }
        _accumulators[task_id] = acc
    return acc


# ──────────────────────────────────────────────────────────
# 任务生命周期
# ──────────────────────────────────────────────────────────

async def start_task(user_id: str = "", session_id: str = "", user_query: str = "",
                     version: Optional[str] = None) -> str:
    """开始一个任务，返回 task_id。"""
    task_id = uuid.uuid4().hex
    ver = version or AGENT_VERSION
    await get_obs_storage().start_task(task_id, ver, user_id, session_id, user_query)
    acc = _acc(task_id)
    acc["version"] = ver
    acc["user_id"] = user_id
    acc["session_id"] = session_id
    acc["user_query"] = user_query
    _current_task_id.set(task_id)
    return task_id


async def end_task(status: str = "ok", error: Optional[str] = None):
    """结束当前任务：把内存累加结果求平均后批量落库，并补全任务汇总。"""
    task_id = _current_task_id.get()
    if not task_id:
        return
    acc = _accumulators.get(task_id)
    if acc is None:
        return

    node_rows: List[Dict[str, Any]] = []
    node_count = 0
    for name, n in acc["nodes"].items():
        inv = int(n["invocations"])
        node_count += inv
        node_rows.append({
            "node": name,
            "invocations": inv,
            "duration_ms_avg": round(n["dur_sum"] / inv, 2) if inv else 0.0,
            "status": n["status"],
            "error": n["error"],
        })

    llm_rows: List[Dict[str, Any]] = []
    llm_call_count = 0
    total_input = 0
    total_output = 0
    total_cached = 0
    llm_dur = 0.0
    for (node, agent, model), v in acc["llm"].items():
        calls = int(v["calls"])
        llm_call_count += calls
        total_input += int(v["in_sum"])
        total_output += int(v["out_sum"])
        total_cached += int(v["cached_sum"])
        llm_dur += float(v["dur_sum"])
        llm_rows.append({
            "node": node, "agent": agent, "model": model,
            "calls": calls,
            "input_tokens_avg": round(v["in_sum"] / calls, 2) if calls else 0.0,
            "output_tokens_avg": round(v["out_sum"] / calls, 2) if calls else 0.0,
            "cached_tokens_avg": round(v["cached_sum"] / calls, 2) if calls else 0.0,
            "duration_ms_avg": round(v["dur_sum"] / calls, 2) if calls else 0.0,
            "error_count": int(v["error_count"]),
            "error": v["error"],
        })

    mcp_rows: List[Dict[str, Any]] = []
    tool_call_count = 0
    tool_dur = 0.0
    for (node, server, tool), v in acc["mcp"].items():
        calls = int(v["calls"])
        tool_call_count += calls
        tool_dur += float(v["dur_sum"])
        mcp_rows.append({
            "node": node, "server": server, "tool": tool,
            "calls": calls,
            "duration_ms_avg": round(v["dur_sum"] / calls, 2) if calls else 0.0,
            "error_count": int(v["error_count"]),
            "retries": int(v["retries"]),
            "error": v["error"],
        })

    summary = {
        "node_count": node_count,
        "llm_call_count": llm_call_count,
        "tool_call_count": tool_call_count,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "cached_input_tokens": total_cached,
        "llm_duration_ms": round(llm_dur, 2),
        "tool_duration_ms": round(tool_dur, 2),
    }

    storage = get_obs_storage()
    await storage.insert_node_metrics(task_id, node_rows)
    await storage.insert_llm_metrics(task_id, llm_rows)
    await storage.insert_mcp_metrics(task_id, mcp_rows)
    await storage.end_task(task_id, status, error, summary)

    _accumulators.pop(task_id, None)


def reset_observability():
    """清空当前执行上下文（每个任务开始前调用）。"""
    _current_task_id.set(None)
    _current_node_name.set(None)


def get_current_llm_id() -> Optional[str]:
    """兼容 shim：聚合模型下不再需要 LLM span id，始终返回 None。"""
    return None


# ──────────────────────────────────────────────────────────
# 中间层上报接口（LLM / MCP）
# ──────────────────────────────────────────────────────────

def record_llm(agent: str, model: str, input_tokens: int, output_tokens: int,
               duration_ms: float, status: str = "ok", error: Optional[str] = None,
               cached_input_tokens: int = 0, output: str = "") -> str:
    """累加一次 LLM 调用到内存聚合器（不再逐条落库）。

    output 参数保留以兼容调用方，但聚合模型下不再存储输出内容。
    """
    task_id = _current_task_id.get()
    if not task_id:
        return ""
    acc = _accumulators.get(task_id)
    if acc is None:
        return ""
    node = _current_node_name.get() or ""
    key = (node, agent, model)
    v = acc["llm"].setdefault(key, {
        "calls": 0, "in_sum": 0, "out_sum": 0, "cached_sum": 0,
        "dur_sum": 0.0, "error_count": 0, "error": None,
    })
    v["calls"] += 1
    v["in_sum"] += int(input_tokens or 0)
    v["out_sum"] += int(output_tokens or 0)
    v["cached_sum"] += int(cached_input_tokens or 0)
    v["dur_sum"] += float(duration_ms or 0)
    if status == "error":
        v["error_count"] += 1
        if v["error"] is None:
            v["error"] = error
    return ""


def record_mcp(server: str, tool: str, duration_ms: float, status: str = "ok",
               retries: int = 0, error: Optional[str] = None, result: str = "") -> str:
    """累加一次 MCP 工具调用到内存聚合器。"""
    task_id = _current_task_id.get()
    if not task_id:
        return ""
    acc = _accumulators.get(task_id)
    if acc is None:
        return ""
    node = _current_node_name.get() or ""
    key = (node, server, tool)
    v = acc["mcp"].setdefault(key, {
        "calls": 0, "dur_sum": 0.0, "error_count": 0, "retries": 0, "error": None,
    })
    v["calls"] += 1
    v["dur_sum"] += float(duration_ms or 0)
    v["retries"] += int(retries or 0)
    if status == "error":
        v["error_count"] += 1
        if v["error"] is None:
            v["error"] = error
    return ""


# ──────────────────────────────────────────────────────────
# 作用域 / 装饰器
# ──────────────────────────────────────────────────────────

def _finish_node(name: str, start: float, status: str, error: Optional[str] = None):
    task_id = _current_task_id.get()
    if not task_id:
        return
    acc = _accumulators.get(task_id)
    if acc is None:
        return
    dur = (time.perf_counter() - start) * 1000
    n = acc["nodes"].setdefault(name, {
        "invocations": 0, "dur_sum": 0.0, "status": "ok", "error": None,
    })
    n["invocations"] += 1
    n["dur_sum"] += dur
    if status == "error":
        n["status"] = "error"
        if n["error"] is None:
            n["error"] = error


@contextlib.contextmanager
def node_scope(name: str):
    """为并发子图/子任务建立独立节点观测上下文，并把节点耗时累加进内存聚合器。"""
    token = _current_node_name.set(name)
    start = time.perf_counter()
    logger.info(f"▶️ [{name}] 开始")
    try:
        yield
    except Exception as e:
        _finish_node(name, start, "error", str(e))
        logger.error(f"❌ [{name}] 失败（{(time.perf_counter() - start) * 1000:.1f}ms）: {e}", exc_info=True)
        raise
    else:
        _finish_node(name, start, "ok")
        logger.info(f"✅ [{name}] 完成，耗时 {(time.perf_counter() - start) * 1000:.1f}ms")
    finally:
        _current_node_name.reset(token)


@contextlib.contextmanager
def correction_scope(parent_llm_span_id: Optional[str]):
    """兼容 shim：聚合模型下无需 correction 树，直接透传。"""
    yield


def node(name: str):
    """节点装饰器：设置当前节点上下文 + 记录节点起止耗时 + 异常兜底上报。"""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(state, *args, **kwargs):
            token = _current_node_name.set(name)
            start = time.perf_counter()
            logger.info(f"▶️ [{name}] 开始")
            try:
                result = await fn(state, *args, **kwargs)
                _finish_node(name, start, "ok")
                logger.info(f"✅ [{name}] 完成，耗时 {(time.perf_counter() - start) * 1000:.1f}ms")
                return result
            except Exception as e:
                _finish_node(name, start, "error", str(e))
                logger.error(f"❌ [{name}] 失败（{(time.perf_counter() - start) * 1000:.1f}ms）: {e}", exc_info=True)
                raise
            finally:
                _current_node_name.reset(token)
        return wrapper
    return deco


# ──────────────────────────────────────────────────────────
# 读时组装：聚合表 → 任务 json
# ──────────────────────────────────────────────────────────

async def build_task_json(task_id: str) -> Dict[str, Any]:
    """从 DB 读取聚合表并组装成任务 json（summary + nodes[]，含版本号）。"""
    storage = get_obs_storage()
    task = await storage.get_task(task_id)
    node_rows = await storage.get_node_metrics(task_id)
    llm_rows = await storage.get_llm_metrics(task_id)
    mcp_rows = await storage.get_mcp_metrics(task_id)

    empty_summary = {
        "node_count": 0, "llm_call_count": 0, "tool_call_count": 0,
        "total_input_tokens": 0, "total_output_tokens": 0, "total_tokens": 0,
        "cached_input_tokens": 0, "cache_hit_rate": None,
        "llm_duration_ms": 0.0, "tool_duration_ms": 0.0, "wall_clock_ms": 0.0,
        "version": "",
    }
    if task is None:
        return {"summary": empty_summary, "nodes": []}

    total_input = int(task.get("total_input_tokens") or 0)
    total_output = int(task.get("total_output_tokens") or 0)
    total_cached = int(task.get("cached_input_tokens") or 0)
    summary = {
        "node_count": int(task.get("node_count") or 0),
        "llm_call_count": int(task.get("llm_call_count") or 0),
        "tool_call_count": int(task.get("tool_call_count") or 0),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "cached_input_tokens": total_cached,
        "cache_hit_rate": round(total_cached / total_input, 4) if total_input > 0 else None,
        "llm_duration_ms": float(task.get("llm_duration_ms") or 0),
        "tool_duration_ms": float(task.get("tool_duration_ms") or 0),
        "wall_clock_ms": float(task.get("duration_ms") or 0),
        "version": task.get("version") or "",
    }

    node_by_name: Dict[str, Dict[str, Any]] = {}
    for n in node_rows:
        node_by_name[n["node"]] = {
            "node": n["node"],
            "duration_ms": float(n.get("duration_ms_avg") or 0),
            "status": n.get("status") or "ok",
            "error": n.get("error"),
            "llm": [],
            "tools": [],
        }

    for l in llm_rows:
        node = node_by_name.get(l["node"])
        if node is None:
            continue
        node["llm"].append({
            "agent": l.get("agent"),
            "model": l.get("model"),
            "calls": int(l.get("calls") or 0),
            "input_tokens_avg": float(l.get("input_tokens_avg") or 0),
            "output_tokens_avg": float(l.get("output_tokens_avg") or 0),
            "cached_tokens_avg": float(l.get("cached_tokens_avg") or 0),
            "duration_ms_avg": float(l.get("duration_ms_avg") or 0),
            "error_count": int(l.get("error_count") or 0),
        })

    for m in mcp_rows:
        node = node_by_name.get(m["node"])
        if node is None:
            continue
        node["tools"].append({
            "server": m.get("server"),
            "tool": m.get("tool"),
            "calls": int(m.get("calls") or 0),
            "duration_ms_avg": float(m.get("duration_ms_avg") or 0),
            "retries": int(m.get("retries") or 0),
            "error_count": int(m.get("error_count") or 0),
        })

    nodes = sorted(node_by_name.values(), key=lambda x: x["node"] or "")
    return {"summary": summary, "nodes": nodes}
