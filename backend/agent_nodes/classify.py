"""意图分类与对话节点：classify_node / conversation_reply_node / handle_feedback_node。

从原 workflow_nodes.py 拆分而来。
"""
from typing import Dict, Any
import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ._common import _LLM, _stream_reply
from ._observability import node

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# 节点 1：意图分类
# ──────────────────────────────────────────────────────────

@node("classify")
async def classify_node(state: Dict[str, Any]) -> Dict[str, Any]:
    user_query = state.get("user_query", "") or ""
    llm = _LLM(agent="classify", temperature=0.3)
    system_prompt = f"""你是查询分类器。判断用户查询属于哪一类：
- feedback: 用户在表达偏好/反馈（如"我喜欢古镇"、"下次别推荐寺庙"、"预算改成3000"、"改成去杭州"）
- conversation: 纯对话（问候、感谢、再见、"你是谁"等，不涉及旅行需求）
- information: 通用信息查询（天气、两地距离、某地概况、美食推荐等，不涉及行程规划和预算）
- travel: 旅游规划相关（需要规划多天/多城市行程、制定路线、安排住宿等）
只返回分类结果：feedback / conversation / information / travel"""
    try:
        resp = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"用户查询：{user_query}"),
        ])
        result = resp.content.strip().lower()
        if "feedback" in result:
            qt = "feedback"
        elif "conversation" in result:
            qt = "conversation"
        elif "information" in result:
            qt = "information"
        else:
            qt = "travel"
    except Exception:
        qt = "travel"
    logger.info(f"🔍 查询分类: {qt}")
    from agent_nodes._observability import record_query_type
    await record_query_type(qt)
    return {"query_type": qt}


# ──────────────────────────────────────────────────────────
# 节点 2a：对话回复
# ──────────────────────────────────────────────────────────

@node("conversation_reply")
async def conversation_reply_node(state: Dict[str, Any]) -> Dict[str, Any]:
    user_query = state.get("user_query", "") or ""
    reply = await _stream_reply(
        "你是一个友好的旅游助手。请用简洁、温暖的中文回复用户的问候或对话。",
        user_query,
        agent="conversation_reply",
    )
    return {
        "final_answer": reply,
        "is_complete": True,
        "messages": [AIMessage(content=reply)],
    }


# ──────────────────────────────────────────────────────────
# 节点 2b：反馈处理
# ──────────────────────────────────────────────────────────

@node("handle_feedback")
async def handle_feedback_node(state: Dict[str, Any]) -> Dict[str, Any]:
    from tools.agent_tools import process_user_feedback
    user_feedback = state.get("user_query", "") or ""
    try:
        from user_profile_manager import get_profile_manager
        current_profile = get_profile_manager().load_profile()
    except Exception:
        current_profile = {}
    result = {}
    try:
        result = await process_user_feedback(user_feedback=user_feedback, current_profile=current_profile)
        msg = result.get("confirmation_message", "好的，我记住您的反馈了！")
    except Exception as e:
        logging.getLogger(__name__).warning(f"反馈处理失败: {e}")
        msg = "好的，我会记住您的反馈！"

    # 记忆系统：把本次反馈的满意度回写到最近一次历史行程（情景记忆）
    try:
        await _update_latest_episode_feedback(result, state)
    except Exception as e:
        logging.getLogger(__name__).warning(f"🧠 [情景记忆] 反馈回写失败: {e}")

    reply = await _stream_reply(
        "你是友好的旅游助手，请用亲切的中文回应，可自然提及已记住用户偏好。",
        f"用户反馈：{user_feedback}\n确认信息：{msg}\n请给出友好回应：",
        agent="feedback",
    )
    return {
        "final_answer": reply,
        "is_complete": True,
        "messages": [AIMessage(content=reply)],
    }


async def _update_latest_episode_feedback(result: Dict[str, Any], state: Dict[str, Any]):
    """根据反馈类型更新用户最近一次历史行程的满意度（0-5）。

    - positive → 5；negative → 1；neutral → 3；core_change → 不写满意度（仅记录反馈文本）
    """
    from memory import get_memory_manager

    ftype = result.get("feedback_type", "neutral")
    if ftype not in ("positive", "negative", "neutral"):
        return
    satisfaction = {"positive": 5, "negative": 1, "neutral": 3}[ftype]

    memory = get_memory_manager()
    episodes = await memory.search_episodes(user_id=state.get("user_id") or "default_user", limit=1)
    if episodes:
        ep = episodes[0]
        await memory.update_episode_feedback(int(ep["id"]), state.get("user_query", ""), satisfaction)
