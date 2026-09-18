"""
Agent 工具封装 - 将 Planner / Feedback 逻辑封装为可调用工具
"""
from typing import Dict, Any, List, Optional
import json
import logging

from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from config.settings import QWEN3_TEMPERATURE
from config.prompt_registry import render_prompt
from agent_nodes._common import _llm_for_prompt, _time_anchors

logger = logging.getLogger(__name__)


# ── Pydantic 模型 ──

class TravelPlanExtraction(BaseModel):
    """提取的旅行计划信息"""
    destination: str = Field(description="Destination city in Chinese")
    origin: str = Field(description="Origin city in Chinese")
    travel_days: int = Field(description="Number of travel days")
    budget: float = Field(description="Budget in yuan")
    travel_date: str = Field(description="Departure date in YYYY-MM-DD format")
    preferences: list[str] = Field(description="Travel preferences")
    needs_deep_analysis: bool = Field(default=False)
    tools_needed: list[str] = Field(default_factory=lambda: ["旅游攻略检索", "12306查询"])


# ── 工具 1: extract_travel_plan（原 Planner Agent） ──

async def extract_travel_plan(
    user_query: str,
    conversation_history: Optional[List] = None
) -> Dict[str, Any]:
    """
    从用户自然语言查询中提取旅行计划的结构化参数。
    原 Planner Agent 的核心逻辑。

    Returns:
        {
            "destination": str,
            "origin": str,
            "travel_days": int,
            "budget": float,
            "travel_date": str,
            "preferences": list[str],
            "needs_deep_analysis": bool,
            "tools_needed": list[str],
            "is_simple_query": bool,
            "needs_clarification": bool,
            "clarification_question": str
        }
    """
    logger.info("🔧 [extract_travel_plan] 开始提取旅行计划参数")
    logger.info(f"   用户查询: {user_query}")

    qwen3_llm, template, _ = await _llm_for_prompt(
        "planner", "extract",
        temperature=QWEN3_TEMPERATURE,
        extra_body={"thinking": {"type": "disabled"}},
    )

    try:
        qwen3_structured = qwen3_llm.with_structured_output(TravelPlanExtraction)
    except Exception:
        qwen3_structured = None

    dynamic_prompt = render_prompt(template, NOW=_time_anchors())

    messages = [SystemMessage(content=dynamic_prompt)]
    if conversation_history:
        for msg in conversation_history:
            if isinstance(msg, (HumanMessage, AIMessage)):
                messages.append(msg)
            elif isinstance(msg, dict):
                role = msg.get("role", msg.get("type", ""))
                if role in ("user", "human"):
                    messages.append(HumanMessage(content=msg.get("content", "")))
                elif role in ("assistant", "ai"):
                    messages.append(AIMessage(content=msg.get("content", "")))

    result: Dict[str, Any] = {
        "destination": "",
        "origin": "",
        "travel_days": 0,
        "budget": 0,
        "travel_date": "",
        "preferences": [],
        "needs_deep_analysis": False,
        "tools_needed": [],
        "is_simple_query": True,
        "needs_clarification": False,
        "clarification_question": ""
    }

    # ── structured output 路径 ──
    if qwen3_structured is not None:
        try:
            extraction = await qwen3_structured.ainvoke(messages)
            result["destination"] = extraction.destination
            result["origin"] = extraction.origin
            result["travel_days"] = extraction.travel_days
            result["budget"] = extraction.budget
            result["travel_date"] = extraction.travel_date
            result["preferences"] = extraction.preferences
            result["needs_deep_analysis"] = extraction.needs_deep_analysis
            result["tools_needed"] = extraction.tools_needed
        except Exception:
            # fall through to JSON path
            extraction = None

    # ── Fallback: JSON 路径 ──
    if not result.get("destination"):
        try:
            response = await qwen3_llm.ainvoke(messages)
            content = response.content.strip()
            from tools.json_utils import extract_json_block
            content = extract_json_block(content)
            extraction = json.loads(content)
            for key in result:
                if key in extraction:
                    result[key] = extraction[key]
        except Exception:
            pass

    # ── 判断查询类型 ──
    simple_keywords = ["天气", "景点", "美食", "攻略", "推荐", "怎么样", "如何", "好玩", "哪里"]
    has_simple_keyword = any(kw in user_query for kw in simple_keywords)

    is_simple_query = (
        (result["destination"] and
         not result["travel_days"] and
         not result["budget"] and
         not result["travel_date"]) or
        has_simple_keyword
    )
    result["is_simple_query"] = is_simple_query

    # ── 检查关键信息缺失 ──
    if not is_simple_query:
        if not result["destination"]:
            result["needs_clarification"] = True
            result["clarification_question"] = "请问您想去哪里旅游？"
        elif result["destination"] and not result["origin"]:
            result["needs_clarification"] = True
            result["clarification_question"] = f"请问您从哪个城市出发去{result['destination']}？这样我才能为您查询具体的交通和行程信息。"

    logger.info(f"   提取结果: destination={result['destination']}, days={result['travel_days']}, "
                f"simple={is_simple_query}, needs_clarification={result['needs_clarification']}")

    return result


# ── 工具 2: process_user_feedback（原 Feedback Agent） ──

async def process_user_feedback(
    user_feedback: str,
    current_profile: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    分析用户反馈，更新偏好档案。原 Feedback Agent 的核心逻辑。

    Returns:
        {
            "feedback_type": "positive|negative|neutral|core_change",
            "preference_updates": {...},
            "confirmation_message": str,
            "needs_replan": bool
        }
    """
    logger.info("🔧 [process_user_feedback] 分析用户反馈")
    logger.info(f"   反馈内容: {user_feedback}")

    if current_profile is None:
        current_profile = {}

    llm, template, _ = await _llm_for_prompt("feedback", "analyze", temperature=0.3)
    system_prompt = render_prompt(
        template,
        current_profile=json.dumps(current_profile, ensure_ascii=False, indent=2),
    )

    # ── 第 1 步：分析语义（只调 1 次） ──
    response = await llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"用户反馈：{user_feedback}"),
    ])

    from tools.json_utils import extract_json_block

    content = extract_json_block(response.content)
    try:
        result = json.loads(content)
        logger.info("   ✅ Qwen 输出直接解析成功")
    except Exception:
        logger.warning("   ⚠️ Qwen 输出解析失败，启用 Flash 修正")
        try:
            from tools.json_utils import fix_json_with_flash
            result = await fix_json_with_flash(
                bad_output=response.content,
                schema_hint="包含 feedback_type, preference_updates, confirmation_message, needs_replan 的对象",
            )
        except Exception:
            logger.error("   ⚠️ Flash 修正全部失败，使用默认值")
            result = {
                "feedback_type": "neutral",
                "preference_updates": {},
                "confirmation_message": "好的，我会记住您的反馈！",
                "needs_replan": False
            }

    # 副作用：更新用户档案
    preference_updates = result.get("preference_updates", {})
    if preference_updates:
        try:
            from user_profile_manager import get_profile_manager
            profile_manager = get_profile_manager()
            profile_manager.update_profile(preference_updates)
            logger.info(f"   ✅ 已更新用户偏好: {list(preference_updates.keys())}")
        except Exception as e:
            logger.warning(f"   ⚠️ 更新用户档案失败: {e}")

    return result
