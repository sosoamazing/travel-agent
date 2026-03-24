"""
Main Agent - 协调者，负责路由和控制流程
支持从 Feedback 返回后的决策
"""
from typing import Dict, Any
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage
from config.settings import QWEN3_MODEL, QWEN3_API_BASE, DASHSCOPE_API_KEY, QWEN3_TEMPERATURE
from graph.state import GlobalState
from user_profile_manager import get_profile_manager


def format_messages(messages: list) -> str:
    """格式化消息历史用于提示词"""
    formatted = []
    for msg in messages:
        try:
            if isinstance(msg, HumanMessage):
                role = "用户"
                content = msg.content or ""
            elif isinstance(msg, AIMessage):
                role = "助手"
                content = msg.content or ""
            elif isinstance(msg, dict):
                role = msg.get("role", "未知")
                if role == "user":
                    role = "用户"
                elif role == "assistant":
                    role = "助手"
                content = msg.get("content", "")
            elif hasattr(msg, 'type'):
                if msg.type == 'human':
                    role = "用户"
                elif msg.type == 'ai':
                    role = "助手"
                else:
                    role = "未知"
                content = getattr(msg, 'content', "")
            else:
                continue
            formatted.append(f"{role}: {content}")
        except Exception:
            continue
    return "\n".join(formatted)


async def regenerate_with_summarizer(state: GlobalState, confirmation_message: str) -> str:
    """
    直接调用 Summarizer 重新生成回答
    """
    from agent_nodes.summarizer_agent import summarizer_agent_node
    
    print(f"\n🔄 复用之前的工具结果，调用 Summarizer 重新生成...")
    
    result = await summarizer_agent_node(state)
    
    summarizer_context = result.get("summarizer_context", {})
    regenerated_answer = summarizer_context.get("final_summary", "")
    
    if regenerated_answer:
        return f"{confirmation_message}\n\n根据您的反馈，我重新为您生成了回答：\n\n{regenerated_answer}"
    
    return f"{confirmation_message}\n\n请您重新提问，我会根据您的新偏好来回答！"


async def main_agent_node(state: GlobalState) -> Dict[str, Any]:
    """
    主协调者Agent节点
    
    职责：
    1. 判断是否是从 Feedback 返回
    2. 如果是从 Feedback 返回，决定是重新规划还是只重新生成
    3. 如果是新查询，判断类型并路由
    """
    print(f"\n{'='*60}")
    print("▶️ Main Agent 开始执行")
    print(f"{'='*60}")
    
    messages = state.get("messages") or []
    user_query = state.get("user_query", "") or ""
    current_agent = state.get("current_agent", "")
    
    print(f"📊 当前状态:")
    print(f"  上一个 Agent: {current_agent}")
    print(f"  对话历史长度: {len(messages)}")
    print(f"  用户查询: {user_query}")
    
    # ========== 情况 1：从 Feedback 返回 ==========
    if current_agent == "feedback":
        print(f"\n🔙 从 Feedback Agent 返回")
        
        needs_replan = state.get("needs_replan", False)
        feedback_type = state.get("feedback_type", "neutral")
        confirmation_message = state.get("confirmation_message", "好的，我记住您的反馈了！")
        
        print(f"  feedback_type: {feedback_type}")
        print(f"  needs_replan: {needs_replan}")
        
        executor_context = state.get("executor_context") or {}
        tool_results = executor_context.get("tool_results", []) if executor_context else []
        rag_results = executor_context.get("rag_results_history", []) if executor_context else []
        
        if needs_replan:
            print(f"\n🔄 需要重新规划（核心需求改变）")
            
            # 重置各子 Agent 的上下文，重新走完整流程
            return {
                "current_agent": "main",
                "next_agent": "planner",
                "is_complete": False,
                "planner_context": None,
                "executor_context": None,
                "summarizer_context": None,
                "messages": [AIMessage(content=confirmation_message)]
            }
        
        elif tool_results or rag_results:
            print(f"\n🔄 有之前的工具结果，直接调用 Summarizer 重新生成")
            
            final_answer = await regenerate_with_summarizer(state, confirmation_message)
            
            return {
                "current_agent": "main",
                "next_agent": None,
                "is_complete": True,
                "messages": [AIMessage(content=final_answer)]
            }
        
        else:
            print(f"\n💬 没有之前的工具结果，给出友好的确认回应")
            
            llm = ChatOpenAI(
                model=QWEN3_MODEL,
                base_url=QWEN3_API_BASE,
                api_key=DASHSCOPE_API_KEY,
                temperature=QWEN3_TEMPERATURE
            )
            
            friendly_prompt = ChatPromptTemplate.from_messages([
                ("system", """你是一个友好的旅游助手。用户刚刚给了你一个反馈，你已经记住了他们的偏好。

请给用户一个友好、温暖的回应，包括：
1. 确认你已经记住了他们的偏好
2. 询问他们现在有什么旅行需求，或者主动提供一些帮助
3. 语气要亲切、自然

不要说"请您重新提问"这样的话，要更主动地帮助用户。"""),
                ("human", """用户的反馈：{user_feedback}
确认消息：{confirmation_message}

请给出友好的回应：""")
            ])
            
            user_feedback = state.get("user_query", "")
            chain = friendly_prompt | llm
            friendly_response = (await chain.ainvoke({
                "user_feedback": user_feedback,
                "confirmation_message": confirmation_message
            })).content.strip()
            
            final_answer = f"{confirmation_message}\n\n{friendly_response}"
            
            return {
                "current_agent": "main",
                "next_agent": None,
                "is_complete": True,
                "messages": [AIMessage(content=final_answer)]
            }
    
    # ========== 情况 2：新查询 ==========
    print(f"\n🆕 处理新查询")
    
    llm = ChatOpenAI(
        model=QWEN3_MODEL,
        base_url=QWEN3_API_BASE,
        api_key=DASHSCOPE_API_KEY,
        temperature=0.3
    )
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", """你是一个查询分类器。请判断用户的查询属于哪一类。

分类标准：
- feedback: 用户在给出反馈（例如"我喜欢古镇"、"下次别推荐寺庙"、"这个不错"、"预算有限"等）
- conversation: 对话类查询（问候、感谢、再见、追问"我刚刚说了什么"等）
- travel: 旅游规划类查询（询问景点、天气、美食、攻略、推荐、规划行程等）

请只返回分类结果（feedback/conversation/travel），不要返回其他内容。"""),
        ("human", "用户查询：{user_query}")
    ])
    
    chain = prompt | llm
    classification_response = await chain.ainvoke({"user_query": user_query})
    query_type = classification_response.content.strip().lower()
    
    print(f"\n🔍 查询类型判断: {query_type}")
    
    if "feedback" in query_type:
        print(f"\n💬 检测到用户反馈，路由给 Feedback Agent")
        print(f"{'='*60}\n")
        return {
            "current_agent": "main",
            "next_agent": "feedback"
        }
    
    if "conversation" in query_type:
        print(f"\n💬 检测到对话类查询，直接回答")
        
        conversation_llm = ChatOpenAI(
            model=QWEN3_MODEL,
            base_url=QWEN3_API_BASE,
            api_key=DASHSCOPE_API_KEY,
            temperature=QWEN3_TEMPERATURE
        )
        
        conversation_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个友好的旅游助手。请根据对话历史给出恰当的回应。

可用信息：
- 用户当前查询：{user_query}

请直接给出回应，不要使用任何格式标记。"""),
            ("human", """对话历史：
{conversation_history}

请回应：""")
        ])
        
        conversation_history = format_messages(messages)
        conversation_chain = conversation_prompt | conversation_llm
        response = (await conversation_chain.ainvoke({
            "user_query": user_query,
            "conversation_history": conversation_history
        })).content.strip()
        
        print(f"\n✅ 直接回答用户问题")
        print(f"{'='*60}\n")
        return {
            "current_agent": "main",
            "next_agent": None,
            "is_complete": True,
            "messages": [AIMessage(content=response)]
        }
    
    print(f"\n🔀 旅游查询，路由给 Planner Agent")
    print(f"{'='*60}\n")
    return {
        "current_agent": "main",
        "next_agent": "planner"
    }
