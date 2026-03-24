"""
LangGraph 全局状态定义 - 按您的架构设计
- 全局上下文：只保存完整对话历史
- 各子 Agent 有自己的上下文
"""
from typing import TypedDict, List, Optional, Annotated, Dict, Any
from langchain_core.messages import BaseMessage
import operator


class PlannerContext(TypedDict):
    """Planner Agent 自己的上下文"""
    destination: Optional[str]
    origin: Optional[str]
    travel_days: Optional[int]
    budget: Optional[float]
    travel_date: Optional[str]
    preferences: Optional[List[str]]
    raw_destination_text: Optional[str]
    needs_deep_analysis: bool
    query_mode: Optional[str]
    scenario_type: Optional[str]
    tools_needed: Optional[List[str]]
    needs_clarification: bool
    clarification_question: Optional[str]


class ExecutorContext(TypedDict):
    """Executor Agent 自己的上下文"""
    tool_results: List[Dict[str, Any]]
    rag_results_history: List[str]
    collected_info: Optional[Dict[str, Any]]


class SummarizerContext(TypedDict):
    """Summarizer Agent 自己的上下文"""
    final_summary: Optional[str]


class GlobalState(TypedDict):
    """全局上下文 - 只保存对话历史，各子 Agent 有自己的上下文"""
    # ========== 全局共享：完整对话历史 ==========
    messages: Annotated[List[BaseMessage], operator.add]
    user_query: Optional[str]
    
    # ========== 各 Agent 自己的上下文 ==========
    planner_context: Optional[PlannerContext]
    executor_context: Optional[ExecutorContext]
    summarizer_context: Optional[SummarizerContext]
    
    # ========== 控制流 ==========
    current_agent: Optional[str]
    next_agent: Optional[str]
    is_complete: bool
    
    # ========== Feedback 相关 ==========
    needs_replan: Optional[bool]
    feedback_type: Optional[str]
    confirmation_message: Optional[str]
    preference_updates: Optional[Dict[str, Any]]
