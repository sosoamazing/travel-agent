"""总结节点：summarizer_node。

从原 workflow_nodes.py 拆分而来。
"""
from typing import Dict, Any, List
import json
import re
import logging

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from config.prompt_registry import render_prompt
from ._common import _llm_for_prompt
from ._observability import node

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# 节点 10：总结
# ──────────────────────────────────────────────────────────

@node("summarizer")
async def summarizer_node(state: Dict[str, Any]) -> Dict[str, Any]:
    user_query = state.get("user_query", "") or ""
    pc = state.get("planner_context") or {}
    city_plans = state.get("city_plans", []) or []
    total_budget = float(state.get("total_budget", 0) or 0)
    spent = float(state.get("spent_budget", 0) or 0)
    transport_costs = state.get("transport_costs", {}) or {}
    # 获取用户偏好
    try:
        from user_profile_manager import get_profile_manager
        user_preferences_str = get_profile_manager().format_profile_for_prompt()
    except Exception:
        user_preferences_str = "暂无用户偏好信息"

    # 序列化城市计划（完整传给 flash，不截断）
    plans_text = json.dumps(city_plans, ensure_ascii=False, indent=2)
    transport_text = json.dumps(transport_costs, ensure_ascii=False, indent=2)

    few_shot = pc.get("memory_fewshot", "") or ""
    few_shot_lines = f"\n\n{few_shot}" if few_shot else ""

    llm, template, _ = await _llm_for_prompt(
        "summarizer", "plan", model_type="flash", streaming=True,
    )
    system_prompt = render_prompt(
        template,
        user_preferences_str=user_preferences_str,
        few_shot_lines=few_shot_lines,
        user_query=user_query,
        total_budget=f"{total_budget:.0f}",
        spent=f"{spent:.0f}",
        transport_text=transport_text,
        plans_text=plans_text,
    )
    # 注意：system_prompt 是 f-string 已格式化好的最终字符串，里面包含
    # plans_text / transport_text 的 JSON 字面量花括号。不能用 ChatPromptTemplate
    # （它会再次做 {var} 插值，遇到 JSON 花括号会报 "unmatched '{' in format spec"）。
    # 直接用 SystemMessage / HumanMessage 喂给 llm.astream。
    summary = ""
    async for chunk in llm.astream([
        SystemMessage(content=system_prompt),
        HumanMessage(content="请生成完整的旅行规划方案："),
    ]):
        summary += chunk.content

    # 后处理：清除 LLM 可能残留的 <br> 等 HTML 标签（Streamlit 用 markdown 渲染，<br> 不被识别）
    summary = re.sub(r'<br\s*/?>', '\n', summary, flags=re.IGNORECASE)
    summary = re.sub(r'</?(?:p|div|span)\b[^>]*>', '', summary, flags=re.IGNORECASE)

    # 预算超支提示
    if total_budget > 0 and spent > total_budget:
        summary += f"\n\n⚠️ 预算提示：当前累计花费 {spent:.0f} 元已超出总预算 {total_budget:.0f} 元，请酌情调整。"
    else:
        summary += f"\n\n💰 预算汇总：累计花费 {spent:.0f} 元 / 总预算 {total_budget:.0f} 元，剩余 {total_budget - spent:.0f} 元。"

    # ── 记忆系统：规划结束 → 情景记忆落库 + 语义偏好蒸馏 + 最终快照持久化 ──
    try:
        await _persist_memory(state, city_plans, summary, spent, transport_costs)
    except Exception as e:
        logger.warning(f"🧠 [记忆] 规划落库失败: {e}")

    return {
        "final_answer": summary,
        "is_complete": True,
        "messages": [AIMessage(content=summary)],
    }


async def _persist_memory(
    state: Dict[str, Any],
    city_plans: List[Dict[str, Any]],
    summary: str,
    spent: float,
    transport_costs: Dict[str, float],
):
    """规划完成后执行记忆落库：

    - 情景记忆：从 city_plans 构建 episode 并保存（trip_episodes 表）
    - 语义记忆：从 city_plans + 用户查询蒸馏偏好回写用户档案
    - 工作记忆：快照已在 plan_all_cities_concurrent_node 中保存，这里确保再次持久化最终版本
    """
    from memory import get_memory_manager

    memory = get_memory_manager()
    pc = state.get("planner_context") or {}
    session_id = state.get("session_id") or ""
    user_id = state.get("user_id") or "default_user"

    # 情景记忆：结构化字段直接从 city_plans 提取；summary 用本次生成的方案
    episode = memory.build_episode(
        city_plans,
        session_id=session_id,
        user_id=user_id,
        origin=pc.get("origin", ""),
        total_budget=float(state.get("total_budget", 0) or 0),
        total_spent=spent,
        transport_costs=transport_costs,
        summary=summary,
    )
    if episode is not None:
        await memory.save_episode(episode)

    # 语义记忆：蒸馏用户偏好回写档案
    memory.distill_from_city_plans(city_plans, user_query=state.get("user_query", "") or "")

