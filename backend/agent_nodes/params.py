"""参数提取节点：extract_params_node / ask_clarification_node 及城市解析辅助。

从原 workflow_nodes.py 拆分而来。
"""
from typing import Dict, Any, List
import re
import logging

from langchain_core.messages import HumanMessage, AIMessage

from ._common import _LLM, _stream_reply, _time_anchors
from ._observability import node

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# 城市解析
# ──────────────────────────────────────────────────────────

async def _parse_cities_llm(user_query: str, destination: str, origin: str) -> List[str]:
    """LLM 从查询中解析按游览顺序排列的城市列表（若用户在出发地游览，可包含出发地为首城）"""
    llm = _LLM(agent="params", temperature=0.0)
    prompt = (f"从用户旅行查询中提取要游览的城市列表（按游览顺序排列）。\n"
              f"规则：\n"
              f"- 若用户明确要在出发地 '{origin}' 游览（如'先在本地玩几天再去...'），则将出发地作为第一个城市\n"
              f"- 若出发地仅是起点不游览，则不要包含出发地\n"
              f"- 只返回城市名，用逗号分隔，例如：上海,苏州,杭州。若只有一个城市就返回一个。\n"
              f"\n"
              f"{_time_anchors()}\n"
              f"用户查询：{user_query}\n"
              f"出发地：{origin}\n"
              f"已提取的目的地字段：{destination}")
    try:
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        parts = re.split(r"[，,、/]", resp.content)
        cities: List[str] = []
        for p in parts:
            p = p.strip().strip("。.")
            if p and p not in cities:
                cities.append(p)
        return cities or ([destination] if destination else [])
    except Exception:
        return [destination] if destination else []


# ──────────────────────────────────────────────────────────
# 节点 3：参数提取
# ──────────────────────────────────────────────────────────

@node("extract_params")
async def extract_params_node(state: Dict[str, Any]) -> Dict[str, Any]:
    from tools.agent_tools import extract_travel_plan
    user_query = state.get("user_query", "") or ""
    messages = state.get("messages", [])
    session_id = state.get("session_id") or ""
    user_id = state.get("user_id") or "default_user"

    result = await extract_travel_plan(user_query=user_query, conversation_history=messages)

    planner_context: Dict[str, Any] = {
        "destination": result.get("destination", ""),
        "origin": result.get("origin", ""),
        "travel_days": result.get("travel_days", 0) or 0,
        "budget": result.get("budget", 0) or 0,
        "travel_date": result.get("travel_date", ""),
        "preferences": result.get("preferences", []) or [],
        "needs_clarification": result.get("needs_clarification", False),
        "clarification_question": result.get("clarification_question", ""),
    }

    # ── 记忆系统：注入情景记忆 few-shot（历史行程参考） ──
    # 需要澄清时暂不注入（此时目的地/预算未确认）
    if not planner_context["needs_clarification"]:
        try:
            from memory import get_few_shot_context
            few_shot = await get_few_shot_context(
                destination=planner_context["destination"],
                origin=planner_context["origin"],
                budget=float(planner_context["budget"] or 0),
                user_id=user_id,
                limit=2,
            )
            if few_shot:
                planner_context["memory_fewshot"] = few_shot
                logger.info("🧠 [情景记忆] 已注入历史行程 few-shot 参考")
        except Exception as e:
            logger.warning(f"🧠 [情景记忆] few-shot 注入失败: {e}")

    # 若需要澄清，直接返回
    if planner_context["needs_clarification"]:
        return {"planner_context": planner_context}

    # 解析城市列表
    cities = await _parse_cities_llm(user_query, planner_context["destination"], planner_context["origin"])
    if not cities:
        cities = [planner_context["destination"]] if planner_context["destination"] else []

    planner_context["_cities_count"] = len(cities)

    return {
        "planner_context": planner_context,
        "cities": cities,
        "current_city_index": 0,
        "current_city": cities[0] if cities else None,
        "total_budget": float(planner_context["budget"] or 0),
        "spent_budget": 0.0,
        "spent_before_city": 0.0,
        "transport_costs": {},
        "over_budget": False,
        "budget_message": None,
        "replan_count": 0,
        "city_plans": [],
        "tool_results": [],
        "rag_results_history": [],
    }


# ──────────────────────────────────────────────────────────
# 节点 3b：请求澄清
# ──────────────────────────────────────────────────────────

@node("ask_clarification")
async def ask_clarification_node(state: Dict[str, Any]) -> Dict[str, Any]:
    pc = state.get("planner_context") or {}
    question = pc.get("clarification_question") or "请提供更多关于您旅行计划的信息（目的地、出发地、天数、预算、日期）。"
    # 流式输出澄清问题，便于前端实时展示
    reply = await _stream_reply(
        "你是友好的旅游助手。请用亲切的语气重述以下澄清问题，保持原意，不要添加额外建议。",
        question,
        agent="params",
    )
    return {
        "final_answer": reply,
        "is_complete": True,
        "messages": [AIMessage(content=reply)],
    }
