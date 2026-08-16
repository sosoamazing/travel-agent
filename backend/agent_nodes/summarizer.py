"""总结节点：summarizer_node。

从原 workflow_nodes.py 拆分而来。
"""
from typing import Dict, Any, List
import json
import re
import logging

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from ._common import _LLM, _token_tracker
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

    system_prompt = f"""你是一位专业的旅游规划师。请基于已完成的多城市规划数据，为用户生成一份完整、清晰、实用的旅行方案。

{user_preferences_str}
{few_shot_lines}

核心规则：
- 严格基于下方「城市计划数据」呈现，绝不编造景点/酒店/价格
- city_plan 中可用字段：city, route_plan(含 selected_attractions/days/attractions_cost), hotels(含 name/price_per_night), selected_hotel(LLM 已选定的最佳酒店，含 name/price_per_night/selected_reason), transport_cost, hotel_cost, nights, per_city_budget
- 禁止编造 city_plan 中不存在的字段，特别是：餐饮费、杂费、前期费用、已花销等。如需提及餐饮，仅作为行程建议而非预算项
- 「累计已花」已经包含全部交通费+景点费+酒店费，是总花费，不要再拆分或额外叠加
- 按城市顺序分段展示，每段包含：交通方案、每日行程（确保每景点≥1.5h、含2h午餐休息）、景点门票、**推荐酒店（优先展示 selected_hotel，并简述其位置/价格契合度，可再附 1-2 个备选）**、该市预算
- 末尾给出总预算汇总（交通+门票+酒店）与剩余预算
- 语气友好专业，使用 emoji 与分隔符提升可读性

输出格式要求（重要）：
- 使用纯 Markdown 格式，禁止使用 HTML 标签（特别是 <br>、<br/>、<p> 等）
- 换行用 Markdown 方式：段落之间用空行分隔，列表项用 - 开头
- 用 ## 作为城市分段标题，用 **加粗** 强调关键字段

用户原始需求：{user_query}
总预算：{total_budget:.0f} 元
累计已花（=交通+景点+酒店总和，不要再叠加）：{spent:.0f} 元
各段交通费用：{transport_text}

城市计划数据：
{plans_text}
"""

    llm = _LLM(agent="summarizer", model_type="flash", streaming=True)
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

    # Token 使用统计（含 summarizer 自身调用）
    logger.info(f"📊 [Token 统计]\n{_token_tracker.summary()}")

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

