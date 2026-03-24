"""
Feedback Agent - 处理用户反馈，更新用户偏好
只做一件事：理解反馈 + 更新档案
然后回到 Main Agent 做决策
"""
import asyncio
import json
from typing import Dict, Any
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage
from config.settings import QWEN3_MODEL, QWEN3_API_BASE, DASHSCOPE_API_KEY, QWEN3_TEMPERATURE
from graph.state import GlobalState
from user_profile_manager import get_profile_manager


async def analyze_feedback_with_llm(user_feedback: str, current_profile: Dict[str, Any]) -> Dict[str, Any]:
    """
    使用 LLM 分析用户反馈，提取偏好更新
    
    Returns:
        {
            "feedback_type": "positive|negative|neutral|core_change",
            "preference_updates": {...},
            "confirmation_message": "给用户的确认消息",
            "needs_replan": true/false  // 是否需要重新规划
        }
    """
    llm = ChatOpenAI(
        model=QWEN3_MODEL,
        base_url=QWEN3_API_BASE,
        api_key=DASHSCOPE_API_KEY,
        temperature=0.3
    )
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", """你是一个用户反馈分析专家。请分析用户的反馈，提取用户偏好的更新。

当前用户档案：
{current_profile}

请分析用户的反馈，并输出 JSON 格式的结果：
{{
    "feedback_type": "positive|negative|neutral|core_change",
    "preference_updates": {{
        "travel_style": ["要添加的旅行风格"],
        "destination_types": ["要添加的目的地类型"],
        "budget_level": "预算水平",
        "hotel_preference": ["要添加的住宿偏好"],
        "dietary_restrictions": ["要添加的饮食禁忌"],
        "cuisine_preference": ["要添加的菜系偏好"],
        "liked_activities": ["要添加的喜欢的活动"],
        "disliked_activities": ["要添加的不喜欢的活动"],
        "transport_priority": ["交通优先级"]
    }},
    "confirmation_message": "给用户的友好确认消息，说明你记住了什么",
    "needs_replan": true/false
}}

注意：
- feedback_type 说明：
  * positive: 正向反馈（"我喜欢古镇"）
  * negative: 负向反馈（"我不喜欢寺庙"）
  * neutral: 中性反馈
  * core_change: 核心需求改变（如"预算改成2000"、"改成去杭州"、"改成玩5天"）
- needs_replan: 
  * 如果是 core_change（核心需求改变），设为 true
  * 如果只是微调偏好但核心需求没变，设为 false
- 只在用户明确提到时才更新对应字段
- 列表类型的字段是追加新项，不是替换
- budget_level 可选值："经济型", "舒适型", "豪华型"
- 如果用户没有提到某个偏好，就不要包含在 preference_updates 中
- confirmation_message 要友好自然，让用户知道你记住了"""),
        ("human", "用户反馈：{user_feedback}")
    ])
    
    chain = prompt | llm
    response = await chain.ainvoke({
        "user_feedback": user_feedback,
        "current_profile": json.dumps(current_profile, ensure_ascii=False, indent=2)
    })
    
    content = response.content.strip()
    
    if "```json" in content:
        start = content.find("```json") + 7
        end = content.find("```", start)
        if end != -1:
            content = content[start:end]
    elif "```" in content:
        start = content.find("```") + 3
        end = content.find("```", start)
        if end != -1:
            content = content[start:end]
    
    try:
        return json.loads(content.strip())
    except Exception as e:
        print(f"❌ 解析反馈分析结果失败: {e}")
        return {
            "feedback_type": "neutral",
            "preference_updates": {},
            "confirmation_message": "好的，我会记住您的反馈！",
            "needs_replan": False
        }


async def feedback_agent_node(state: GlobalState) -> Dict[str, Any]:
    """
    Feedback Agent 节点
    
    职责：
    1. 理解用户反馈
    2. 更新用户档案
    3. 返回给 Main Agent 做决策
    """
    print(f"\n{'='*60}")
    print("💬 Feedback Agent 开始执行")
    print(f"{'='*60}")
    
    user_feedback = state.get("user_query", "") or ""
    
    print(f"📝 用户反馈: {user_feedback}")
    
    profile_manager = get_profile_manager()
    current_profile = profile_manager.load_profile()
    
    try:
        analysis_result = await analyze_feedback_with_llm(user_feedback, current_profile)
        confirmation_message = analysis_result.get("confirmation_message", "好的，我记住了！")
        preference_updates = analysis_result.get("preference_updates", {})
        needs_replan = analysis_result.get("needs_replan", False)
        feedback_type = analysis_result.get("feedback_type", "neutral")
        
        print(f"✅ 生成确认消息: {confirmation_message}")
        print(f"📋 反馈类型: {feedback_type}")
        print(f"🔄 是否需要重新规划: {needs_replan}")
        
        # 更新用户档案（同步更新，确保新偏好立即生效）
        if preference_updates:
            print(f"📋 更新偏好: {preference_updates}")
            profile_manager.update_profile(preference_updates)
        
        print(f"\n✅ Feedback Agent 执行完成，返回给 Main Agent")
        print(f"{'='*60}\n")
        
        # 返回给 Main Agent，让它做决策
        return {
            "current_agent": "feedback",
            "next_agent": "main",  # 回到 Main Agent
            "is_complete": False,  # 不结束，继续流程
            "needs_replan": needs_replan,
            "feedback_type": feedback_type,
            "confirmation_message": confirmation_message,
            "preference_updates": preference_updates
        }
        
    except Exception as e:
        print(f"❌ Feedback Agent 异常: {e}")
        import traceback
        traceback.print_exc()
        
        return {
            "current_agent": "feedback",
            "next_agent": "main",
            "is_complete": False,
            "needs_replan": False,
            "feedback_type": "neutral",
            "confirmation_message": "好的，我记住您的反馈了！",
            "preference_updates": {}
        }
