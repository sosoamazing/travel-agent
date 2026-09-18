"""观测追踪：span 树模型 + span 栈（栈顶作 parent）+ result 双列。

设计要点：
- 任务开始：`start_task(task_id, ...)` 前置插入 obs_tasks（task_id=业务 uuid，无自增 id）。
- 节点 / LLM / MCP：start/end 都走幂等 upsert（乱序可接受）；已有 output/input 不被空占位覆盖。
- span 栈（contextvars）：每个任务一个栈，栈顶即当前父 span。LLM/MCP 以栈顶为 parent，
  嵌套调用（如矫正）自动成为父 span 的子。
- result 双列：`result_kind`（running/ok/error）+ `result`（具体原因：ok/degraded/timeout/401/...）。
  `result_kind` 由 `result` 推导（`_derive_kind`）。
- duration_ms = end_ts - start_ts，不冗余存储；seq 由 start_ts 推导。
- 本模块使用 psycopg3 异步连接池（async_db_connection），须在异步上下文调用。
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

# 当前任务 id（业务 uuid）
_current_task_id: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "obs_current_task_id", default=None
)
# 当前节点名
_current_node_name: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "obs_current_node_name", default=None
)
# span 栈：当前任务的活动 span_id（uuid）列表（栈顶 = 当前父 span）。
# 任务开始时初始化为 []，节点/LLM/MCP 创建 span 时 push，结束 pop。
# 任何 span 创建时以栈顶为 parent（生命周期包含即父子，OTel 标准模型）。
_span_stack: "contextvars.ContextVar[List[str]]" = contextvars.ContextVar(
    "obs_span_stack", default=[]
)


# ──────────────────────────────────────────────────────────
# 工具
# ──────────────────────────────────────────────────────────

# 错误类 result → result_kind='error'；降级/成功 → 'ok'
_ERROR_RESULTS = {"timeout", "401", "auth_failed", "rate_limited", "parse_error", "connection_error", "error"}


def _derive_kind(result: str) -> str:
    """由细粒度 result 推导粗粒度 result_kind。"""
    if result == "running":
        return "running"
    if result in _ERROR_RESULTS:
        return "error"
    return "ok"  # ok / degraded 均属成功


# ──────────────────────────────────────────────────────────
# 任务生命周期
# ──────────────────────────────────────────────────────────

async def start_task(task_id: str, user_id: str = "", session_id: str = "",
                     user_query: str = "", intent: Optional[str] = None,
                     version: Optional[str] = None) -> str:
    """任务开始：前置插入 obs_tasks 占位行（task_id=业务 uuid），返回 task_id。"""
    ver = version or AGENT_VERSION
    await get_obs_storage().start_task(task_id, ver, user_id, session_id, user_query, intent=intent)
    _current_task_id.set(task_id)
    _span_stack.set([])
    return task_id


async def end_task(result: str = "ok", error_what: Optional[str] = None):
    """结束当前任务：UPDATE obs_tasks 补全 end_ts / result。"""
    task_id = _current_task_id.get()
    if not task_id:
        return
    await get_obs_storage().end_task(task_id, _derive_kind(result), result, error_what, summary={})
    _current_task_id.set(None)
    _current_node_name.set(None)
    _span_stack.set([])


def reset_observability():
    """清空当前执行上下文（每个任务开始前调用）。"""
    _current_task_id.set(None)
    _current_node_name.set(None)
    _span_stack.set([])


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
# 节点 span（async 成对：start_node / end_node / set_span_result）
# ──────────────────────────────────────────────────────────

async def start_node(name: str) -> str:
    """节点调用开始：生成 uuid span_id，幂等 upsert 占位行，push 到 span 栈。"""
    task_id = _current_task_id.get()
    if not task_id:
        return ""
    _current_node_name.set(name)
    span_id = uuid.uuid4().hex
    await get_obs_storage().start_node_span(task_id, span_id, name, parent_id=_parent_id())
    _span_stack.set(_span_stack.get() + [span_id])
    return span_id


async def end_node(span_id: str, result: str = "ok", error_what: Optional[str] = None):
    """节点调用结束：幂等 upsert 补全 end_ts / result，并从 span 栈 pop。"""
    if span_id:
        await get_obs_storage().end_node_span(
            span_id, _derive_kind(result), result, error_what,
            task_id=_current_task_id.get() or "",
        )
        _pop_if_top(span_id)


async def set_span_result(result: str, error_what: Optional[str] = None):
    """设置当前 node span 的结果状态（降级标记或错误），供节点内部软失败时调用。

    软失败（降级继续）：`await set_span_result("degraded", "xxx失败，降级: ...")`
    —— 节点继续执行，result_kind 由 `_derive_kind` 推导为 ok。
    """
    task_id = _current_task_id.get()
    if not task_id:
        return
    stack = _span_stack.get()
    if not stack:
        return
    # 栈顶通常是当前 node span；若嵌套在 llm/mcp 内，向上找最近的 node span id
    await get_obs_storage().end_node_span(
        stack[-1], _derive_kind(result), result, error_what, task_id=task_id,
    )


# ──────────────────────────────────────────────────────────
# LLM span（async 成对：start_llm / end_llm）
# ──────────────────────────────────────────────────────────

async def start_llm(agent: str) -> str:
    """LLM 调用开始：生成 uuid span_id，幂等 upsert 占位行，以 span 栈顶为 parent。"""
    task_id = _current_task_id.get()
    if not task_id:
        return ""
    node = _current_node_name.get() or ""
    span_id = uuid.uuid4().hex
    await get_obs_storage().start_llm_span(task_id, span_id, node, agent, parent_id=_parent_id())
    _span_stack.set(_span_stack.get() + [span_id])
    return span_id


async def end_llm(span_id: str, result: str = "ok", error_what: Optional[str] = None,
                  input_tokens: int = 0, output_tokens: int = 0,
                  cached_tokens: int = 0, output: str = "",
                  prompt_id: Optional[str] = None,
                  prompt_version: Optional[str] = None,
                  input_text: Optional[str] = None,
                  input_truncated: bool = False) -> None:
    """LLM 调用结束：upsert span 补全 token / output / 版本指针 / payload，并从栈 pop。

    乱序可接受：end 先到也能插入完整行；已有 output/input 时，空值不会覆盖。
    """
    if span_id:
        await get_obs_storage().end_llm_span(
            span_id, _derive_kind(result), result, error_what,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cached_tokens=cached_tokens, output=output,
            task_id=_current_task_id.get() or "",
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            input_text=input_text,
            input_truncated=input_truncated,
        )
        _pop_if_top(span_id)


# ──────────────────────────────────────────────────────────
# MCP span（async 成对：start_mcp / end_mcp）
# ──────────────────────────────────────────────────────────

async def start_mcp(server: str, tool: str) -> str:
    """MCP 调用开始：生成 uuid span_id，插入 mcp span 占位行，以 span 栈顶为 parent，push 到栈。"""
    task_id = _current_task_id.get()
    if not task_id:
        return ""
    node = _current_node_name.get() or ""
    span_id = uuid.uuid4().hex
    await get_obs_storage().start_mcp_span(task_id, span_id, node, server, tool, parent_id=_parent_id())
    _span_stack.set(_span_stack.get() + [span_id])
    return span_id


async def end_mcp(span_id: str, result: str = "ok", error_what: Optional[str] = None,
                  retries: int = 0) -> None:
    """MCP 调用结束：幂等 upsert 补全 end_ts / retries / result，并从栈 pop。"""
    if span_id:
        await get_obs_storage().end_mcp_span(
            span_id, _derive_kind(result), result, error_what, retries=retries,
            task_id=_current_task_id.get() or "",
        )
        _pop_if_top(span_id)


# ──────────────────────────────────────────────────────────
# 节点装饰器 / 作用域（async 封装，供各 agent 节点使用）
# ──────────────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def node_scope(name: str):
    """为并发子图/子任务建立独立节点观测上下文（async context manager）。

    用法：async with node_scope("city_plan"): ...
    """
    task_id = _current_task_id.get()
    span_id = ""
    if task_id:
        span_id = uuid.uuid4().hex
        await get_obs_storage().start_node_span(task_id, span_id, name, parent_id=_parent_id())
        _span_stack.set(_span_stack.get() + [span_id])
    prev_node = _current_node_name.get()
    _current_node_name.set(name)
    try:
        yield
    except Exception as e:
        if span_id:
            await get_obs_storage().end_node_span(
                span_id, "error", "error", str(e), task_id=task_id or "",
            )
            _pop_if_top(span_id)
        raise
    else:
        if span_id:
            await get_obs_storage().end_node_span(
                span_id, "ok", "ok", None, task_id=task_id or "",
            )
            _pop_if_top(span_id)
    finally:
        if prev_node:
            _current_node_name.set(prev_node)
        else:
            _current_node_name.set(None)


def node(name: str):
    """节点装饰器：设置当前节点上下文 + 记录 node span（开始占位 / 结束补全 + span 栈管理）。"""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(state, *args, **kwargs):
            task_id = _current_task_id.get()
            span_id = ""
            if task_id:
                span_id = uuid.uuid4().hex
                await get_obs_storage().start_node_span(task_id, span_id, name, parent_id=_parent_id())
                _span_stack.set(_span_stack.get() + [span_id])
            prev_node = _current_node_name.get()
            _current_node_name.set(name)
            try:
                result = await fn(state, *args, **kwargs)
                if span_id:
                    await get_obs_storage().end_node_span(
                        span_id, "ok", "ok", None, task_id=task_id or "",
                    )
                    _pop_if_top(span_id)
                return result
            except Exception as e:
                if span_id:
                    await get_obs_storage().end_node_span(
                        span_id, "error", "error", str(e), task_id=task_id or "",
                    )
                    _pop_if_top(span_id)
                raise
            finally:
                if prev_node:
                    _current_node_name.set(prev_node)
                else:
                    _current_node_name.set(None)
        return wrapper
    return deco


def _parent_id() -> Optional[str]:
    """返回当前 span 栈顶 span_id（uuid）；栈空返回 None。"""
    stack = _span_stack.get()
    return stack[-1] if stack else None


def _pop_if_top(span_id: str):
    """若 span_id 在栈顶则 pop（避免多层异常导致栈错乱）。"""
    stack = _span_stack.get()
    if stack and stack[-1] == span_id:
        _span_stack.set(stack[:-1])


# ──────────────────────────────────────────────────────────
# 读时组装：obs_tasks + 三张 span 表 → 任务 trace JSON
# ──────────────────────────────────────────────────────────

async def build_task_json(task_id: str) -> Dict[str, Any]:
    """从 DB 读取任务 + 三张 span 表，按 span 树（parent_id）组装任务大 JSON（格式 B）。

    返回结构：
    {
      "task_id": str,
      "summary": { node_count / llm_call_count / tool_call_count / tokens / duration_ms ... },
      "nodes": [
        { "node": str, "span_id": str, "parent_id": str,
          "result_kind": str, "result": str, "error_what": str,
          "duration_ms": float,
          "llm": [ {agent, model(反查), input_tokens, output_tokens, cached_tokens, output,
                    duration_ms, result_kind, result, error_what, parent_id} ],
          "tools": [ {server, tool, duration_ms, retries, result_kind, result, error_what, parent_id} ] },
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
        "result_kind": "unknown", "version": "",
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
            "span_id": n.get("span_id"),
            "parent_id": n.get("parent_id"),
            "result_kind": n.get("result_kind") or "ok",
            "result": n.get("result") or "ok",
            "error_what": n.get("error_what"),
            "duration_ms": _dur_ms(n),
            "llm": [_llm_item(l) for l in child_llm],
            "tools": [_mcp_item(m) for m in child_mcp],
        })

    node_out.sort(key=lambda x: x["duration_ms"], reverse=True)

    summary = _build_summary(task, nodes, llms, mcps,
                             total_in, total_out, total_cached, llm_dur, tool_dur)
    model_map = _build_model_map(llms)

    return {"task_id": task_id, "summary": summary, "nodes": node_out, "model_map": model_map}


async def build_trace_json(task_id: str) -> Dict[str, Any]:
    """组装完整 span 树 trace（格式 A：点分路径 + 格式 B：大 JSON）。

    从三张 span 表按 parent_id 递归组装成层级树；每个 span 携带 `path`
    （点分路径，如 `task.node.llm`），用于精确定位一次调用。

    返回结构：
    {
      "task_id": str,
      "summary": {...},          # 同 build_task_json
      "trace": [ {node/agent/tool 统一字段, span_id, parent_id, path, children: [...]} ],
      "nodes": [...],            # 格式 B 大 JSON（同 build_task_json）
      "model_map": {...}
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
        "result_kind": "unknown", "version": "",
    }
    if task is None:
        return {"task_id": task_id, "summary": empty, "trace": [], "nodes": [], "model_map": {}}

    # 统一 span 记录：span_id → span（含 span_type / 名称 / 结果 / 耗时 / 专属字段）
    by_id: Dict[str, Dict[str, Any]] = {}
    roots: List[Dict[str, Any]] = []
    _ingest_spans(by_id, nodes, "node")
    _ingest_spans(by_id, llms, "llm")
    _ingest_spans(by_id, mcps, "mcp")

    # 按 parent_id 挂子；parent_id 为空或指向不存在父的为根
    for sid, sp in by_id.items():
        pid = sp.get("parent_id")
        if pid and pid in by_id:
            by_id[pid].setdefault("children", []).append(sp)
        else:
            roots.append(sp)

    def _walk(sp: Dict[str, Any], parent_path: str) -> Dict[str, Any]:
        """递归生成节点（点分路径 + children），按 start_ts 排序。"""
        stype = sp.get("span_type")
        # 名称：node→node、llm→agent、mcp→tool（点分路径示例：task.node.agent / task.node.tool）
        if stype == "llm":
            label = sp.get("agent") or sp.get("span_id")
        elif stype == "mcp":
            label = sp.get("tool") or sp.get("span_id")
        else:
            label = sp.get("node") or sp.get("span_id")
        path = f"{parent_path}.{label}" if parent_path else str(label)
        out = {
            "span_id": sp.get("span_id"),
            "span_type": stype,
            "name": label,
            "path": path,
            "parent_id": sp.get("parent_id"),
            "result_kind": sp.get("result_kind") or "ok",
            "result": sp.get("result") or "ok",
            "error_what": sp.get("error_what"),
            "duration_ms": _dur_ms(sp),
            "start_ts": sp.get("start_ts"),
            "end_ts": sp.get("end_ts"),
        }
        if stype == "llm":
            out["agent"] = sp.get("agent")
            out["input_tokens"] = int(sp.get("input_tokens") or 0)
            out["output_tokens"] = int(sp.get("output_tokens") or 0)
            out["cached_tokens"] = int(sp.get("cached_tokens") or 0)
            out["output"] = sp.get("output")
            out["prompt_id"] = sp.get("prompt_id")
            out["prompt_version"] = sp.get("prompt_version")
        elif stype == "mcp":
            out["server"] = sp.get("server")
            out["tool"] = sp.get("tool")
            out["retries"] = int(sp.get("retries") or 0)
        children = sorted(sp.get("children", []), key=lambda c: (float(c.get("start_ts") or 0)))
        out["children"] = [_walk(c, path) for c in children]
        return out

    roots.sort(key=lambda r: (float(r.get("start_ts") or 0)))
    trace = [_walk(r, "") for r in roots]

    # 复用 build_task_json 的大 JSON + summary
    big = await build_task_json(task_id)
    return {
        "task_id": task_id,
        "summary": big["summary"],
        "trace": trace,
        "nodes": big["nodes"],
        "model_map": big["model_map"],
    }


def _ingest_spans(by_id: Dict[str, Dict[str, Any]], rows: List[Dict[str, Any]],
                  span_type: str) -> None:
    """把一行 span 记录并入 by_id 字典（统一键名 + 补 span_type）。"""
    for r in rows:
        if not r.get("span_id"):
            continue
        item = dict(r)
        item["span_type"] = span_type
        item["children"] = item.get("children", [])
        by_id[r["span_id"]] = item


def _build_summary(task: Dict[str, Any], nodes: List[Dict[str, Any]],
                   llms: List[Dict[str, Any]], mcps: List[Dict[str, Any]],
                   total_in: int, total_out: int, total_cached: int,
                   llm_dur: float, tool_dur: float) -> Dict[str, Any]:
    return {
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
        "result_kind": task.get("result_kind") or "unknown",
        "result": task.get("result") or "",
        "error_what": task.get("error_what"),
        "version": task.get("version") or "",
        "intent": task.get("intent") or "",
        "query_type": task.get("query_type") or "",
    }


def _build_model_map(llms: List[Dict[str, Any]]) -> Dict[str, str]:
    """模型名按 agent 从配置反查。"""
    from config.settings import get_agent_model, QWEN3_MODEL, DS_FLASH_MODEL
    model_map: Dict[str, str] = {}
    for l in llms:
        agent = l.get("agent") or ""
        if agent and agent not in model_map:
            model_map[agent] = get_agent_model(agent, QWEN3_MODEL if agent not in ("summarizer", "json_fix", "hotel_price") else DS_FLASH_MODEL)
    return model_map


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
        "span_id": l.get("span_id"),
        "parent_id": l.get("parent_id"),
        "input_tokens": int(l.get("input_tokens") or 0),
        "output_tokens": int(l.get("output_tokens") or 0),
        "cached_tokens": int(l.get("cached_tokens") or 0),
        "output": l.get("output"),
        "prompt_id": l.get("prompt_id"),
        "prompt_version": l.get("prompt_version"),
        "duration_ms": _dur_ms(l),
        "result_kind": l.get("result_kind") or "ok",
        "result": l.get("result") or "ok",
        "error_what": l.get("error_what"),
    }


def _mcp_item(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "server": m.get("server"),
        "tool": m.get("tool"),
        "span_id": m.get("span_id"),
        "parent_id": m.get("parent_id"),
        "duration_ms": _dur_ms(m),
        "retries": int(m.get("retries") or 0),
        "result_kind": m.get("result_kind") or "ok",
        "result": m.get("result") or "ok",
        "error_what": m.get("error_what"),
    }
