"""信息查询节点：information_query_node / simple_rag_search_node。

从原 workflow_nodes.py 拆分而来。
"""
from typing import Dict, Any, List, Tuple
import json
import asyncio
import logging

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from tools.rag_tool import query_travel_knowledge
from tools.registry.gaode import _unwrap_gaode
from ._common import _LLM, _call_mcp_tool, _stream_reply, _now_str
from ._observability import node
from .city_planning import _extract_attractions

logger = logging.getLogger(__name__)

# 工具调用上限（information_query 和 simple_rag_search 共用）
_MAX_TOOL_CALLS = 7

# 跨会话历史行程问答的触发关键词
_HISTORY_HINT_PATTERNS = ("上次", "以前", "之前", "以前去", "上次去", "上次住", "上次玩")


def _looks_like_history_query(query: str) -> bool:
    """判断查询是否在询问自己的历史行程（如"我上次去成都住哪了"）。"""
    return any(k in query for k in _HISTORY_HINT_PATTERNS)


# ──────────────────────────────────────────────────────────
# 共用：单个工具调用执行
# ──────────────────────────────────────────────────────────

async def _run_info_tool_call(call: Dict) -> Tuple[str, str]:
    """执行单个信息查询工具调用。

    Returns: (tool_name, result_str)
    """
    tool = call.get("tool", "")
    params = call.get("params", {}) or {}

    try:
        if tool == "weather" and params.get("city"):
            return tool, await _call_mcp_tool("gaode_weather", city=params["city"])

        elif tool == "rag" and params.get("query"):
            return tool, await query_travel_knowledge(params["query"])

        elif tool == "poi":
            return tool, await _call_mcp_tool(
                "gaode_poi_search_lite",
                keywords=params.get("keywords", ""),
                city=params.get("city", ""),
            )

        elif tool == "distance" and params.get("from_city") and params.get("to_city"):
            from_raw, to_raw = await asyncio.gather(
                _call_mcp_tool("gaode_geo", address=params["from_city"]),
                _call_mcp_tool("gaode_geo", address=params["to_city"]),
            )
            from_loc = to_loc = ""
            try:
                fd = json.loads(from_raw) if isinstance(from_raw, str) else from_raw
                td = json.loads(to_raw) if isinstance(to_raw, str) else to_raw
                fd = _unwrap_gaode(fd)  # 兼容 {"return": [{...}]} 新结构
                td = _unwrap_gaode(td)
                from_loc = fd.get("location", "") if isinstance(fd, dict) else ""
                to_loc = td.get("location", "") if isinstance(td, dict) else ""
            except Exception:
                pass
            if from_loc and to_loc:
                dist_raw = await _call_mcp_tool("gaode_driving", origin=from_loc, destination=to_loc)
                dist_data = json.loads(dist_raw) if isinstance(dist_raw, str) else dist_raw
                return tool, json.dumps({
                    "from": params["from_city"], "to": params["to_city"],
                    "route": dist_data,
                }, ensure_ascii=False)
            return tool, json.dumps({"error": "无法解析坐标"}, ensure_ascii=False)

        elif tool == "ip_location":
            return tool, await _call_mcp_tool("gaode_ip_location")

        elif tool == "lucky_day":
            return tool, await _call_mcp_tool("lucky_day", date=params.get("date", ""))

    except Exception as e:
        return tool, json.dumps({"error": str(e), "tool": tool}, ensure_ascii=False)
    return tool, ""


async def _llm_select_tools(user_query: str, system_hint: str) -> List[Dict]:
    """让 LLM 通过 bind_tools 选择工具（最多 _MAX_TOOL_CALLS 个）"""
    from tools.registry import get_info_query_tool_schemas

    llm = _LLM(agent="info_query", temperature=0.0)
    tool_schemas = get_info_query_tool_schemas()
    llm_with_tools = llm.bind_tools(tool_schemas)

    resp = await llm_with_tools.ainvoke([
        SystemMessage(content=f"{system_hint}\n\n当前时间：{_now_str()}"),
        HumanMessage(content=user_query),
    ])

    tool_calls: List[Dict] = []
    if hasattr(resp, "tool_calls") and resp.tool_calls:
        for tc in resp.tool_calls[:_MAX_TOOL_CALLS]:
            tool_calls.append({
                "tool": tc.get("name", ""),
                "params": tc.get("args", {}) or {},
            })
    return tool_calls


# ──────────────────────────────────────────────────────────
# 节点 1b：通用信息查询（天气/距离/概况等，非旅行规划）
# ──────────────────────────────────────────────────────────

@node("information_query")
async def information_query_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """处理天气、距离、城市概况等通用信息查询：LLM bind_tools 选工具（最多7个），并发执行"""
    user_query = state.get("user_query", "") or ""
    user_id = state.get("user_id") or "default_user"

    # ── Step 0: 记忆系统 - 跨会话历史行程问答（"上次去X住哪/花了多少"）──
    # 命中历史关键词且检索到历史行程时直接回答，不触发工具调用
    if _looks_like_history_query(user_query):
        try:
            from memory import get_memory_manager
            memory = get_memory_manager()
            episodes = await memory.search_episodes(user_id=user_id, limit=3)
            if episodes:
                history_text = memory.format_episodes_for_prompt(episodes)
                reply = await _stream_reply(
                    "你是友好的旅游助手。用户询问自己之前的历史行程。"
                    "请基于以下历史行程记录，用亲切、简洁的中文回答。"
                    "若记录中缺少用户问的信息（如具体酒店名），如实说明未记录，不要编造。",
                    f"用户查询：{user_query}\n\n历史行程记录：\n{history_text}",
                    agent="info_query",
                )
                logger.info("🧠 [情景记忆] 历史行程问答命中，直接回答")
                return {
                    "final_answer": reply,
                    "is_complete": True,
                    "messages": [AIMessage(content=reply)],
                }
        except Exception as e:
            logger.warning(f"🧠 [情景记忆] 历史查询检索失败，走常规流程: {e}")

    # ── Step 1: LLM bind_tools 选择工具 ──
    tool_calls = await _llm_select_tools(
        user_query,
        f"你是信息查询助手，根据用户查询选择合适的工具。最多选{_MAX_TOOL_CALLS}个，按需选择。"
    )

    # ── Step 2: 工具执行（并发） ──
    results: List[str] = []
    if tool_calls:
        logger.info(f"ℹ️ [信息查询] bind_tools 选中: {[c['tool'] for c in tool_calls]}")
        pairs = await asyncio.gather(*[_run_info_tool_call(c) for c in tool_calls])
        results = [r for _, r in pairs]
    else:
        # 兜底：直接 RAG
        tool_calls = [{"tool": "rag", "params": {"query": user_query}}]
        _, rag_result = await _run_info_tool_call(tool_calls[0])
        results = [rag_result]

    # ── Step 3: 汇总结果生成回复 ──
    tool_names = [c["tool"] for c in tool_calls]
    combined = "\n\n---\n\n".join(
        f"[{tool_names[i] if i < len(tool_names) else 'rag'}] {r[:2000]}"
        for i, r in enumerate(results)
    )

    reply = await _stream_reply(
        "你是一个友好的旅游助手。请基于以下查询结果，用亲切、简洁的中文回答用户。"
        "如有具体数据（温度、距离、时间、八字、五行等），务必完整保留。不要编造信息。",
        f"用户查询：{user_query}\n\n查询结果：\n{combined}",
        agent="info_query",
    )

    return {
        "final_answer": reply,
        "is_complete": True,
        "messages": [AIMessage(content=reply)],
        "tool_results": [{"tool": "information_query", "calls": tool_calls, "results": results}],
    }


# ──────────────────────────────────────────────────────────
# 节点 3c：简单查询（无预算/无出发地时的短路径）
# ──────────────────────────────────────────────────────────

@node("simple_rag_search")
async def simple_rag_search_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """简单查询：有目的地但无预算/无出发地。

    LLM bind_tools 选工具（最多7个）并发执行；
    强制保证 RAG + POI 被执行（供 _extract_attractions 提取景点），
    其他工具结果（天气/距离/八字等）放入 tool_results 供 summarizer 参考。
    """
    pc = state.get("planner_context") or {}
    destination = pc.get("destination", "") or ""
    user_query = state.get("user_query", "") or ""
    preferences = pc.get("preferences", []) or []

    if not destination:
        # 没目的地也没预算，直接对话式回复
        reply = await _stream_reply(
            "你是友好的旅游助手。用户的查询信息不足，请用友好的语气询问用户想去哪个城市。",
            user_query,
            agent="info_query",
        )
        return {
            "final_answer": reply,
            "is_complete": True,
            "messages": [AIMessage(content=reply)],
        }

    # ── Step 1: LLM bind_tools 选择工具（最多7个） ──
    tool_calls = await _llm_select_tools(
        user_query,
        f"你是旅游信息检索助手。用户想了解目的地「{destination}」的相关信息。"
        f"请根据用户查询选择合适的工具，最多选{_MAX_TOOL_CALLS}个。"
        f"建议至少选择 rag（检索攻略）和 poi（搜索景点）以获得全面的景点推荐。"
    )

    # ── Step 2: 强制保证 RAG + POI 被执行 ──
    has_rag = any(c.get("tool") == "rag" for c in tool_calls)
    has_poi = any(c.get("tool") == "poi" for c in tool_calls)
    if not has_rag:
        tool_calls.append({"tool": "rag", "params": {"query": f"{destination} 景点 攻略"}})
    if not has_poi:
        tool_calls.append({"tool": "poi", "params": {"keywords": f"{destination} 景点", "city": destination}})

    # ── Step 3: 并发执行所有工具 ──
    logger.info(f"🔍 [简单查询] {destination}：并发执行 {len(tool_calls)} 个工具：{[c['tool'] for c in tool_calls]}")
    pairs: List[Tuple[str, str]] = list(await asyncio.gather(*[_run_info_tool_call(c) for c in tool_calls]))

    # ── Step 4: 提取 RAG + POI 结果用于景点提取（合并同工具多组参数的结果） ──
    rag_parts: List[str] = []
    poi_parts: List[str] = []
    tool_results: List[Dict] = []
    for (tool_name, result_str), call in zip(pairs, tool_calls):
        tool_results.append({
            "tool": tool_name,
            "result": result_str,
            "city": destination,
            "params": call.get("params", {}),
        })
        if tool_name == "rag" and result_str:
            rag_parts.append(result_str)
        elif tool_name == "poi" and result_str:
            poi_parts.append(result_str)

    rag_raw = "\n\n---\n\n".join(rag_parts)
    poi_raw = "\n\n---\n\n".join(poi_parts)

    # ── Step 5: 提取景点 + 构造 city_plan ──
    attractions = await _extract_attractions(rag_raw, poi_raw, destination, preferences, user_query)

    city_plan = {
        "city": destination,
        "attractions_candidates": attractions,
        "route_plan": {
            "selected_attractions": [a.get("name", "") for a in attractions[:5]],
            "days": [{"day": 1, "stops": [], "lunch": True}],
            "attractions_cost": 0,
        },
        "hotels": [],
        "transport_cost": 0,
        "hotel_cost": 0,
        "nights": 0,
        "city_total_cost": 0,
        "_simple_mode": True,
    }

    logger.info(f"✅ [简单查询] {destination}：{len(tool_calls)} 个工具并发完成，候选 {len(attractions)} 个景点")

    return {
        "cities": [destination],
        "current_city": destination,
        "current_city_index": 0,
        "city_plans": [city_plan],
        "rag_results_history": [rag_raw] if rag_raw else [],
        "tool_results": tool_results,
        "spent_budget": 0.0,
    }
