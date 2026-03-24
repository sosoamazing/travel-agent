"""
LangGraph 工作流定义
主 Agent 控制全局
双模式架构：
- 简单模式：Main → Planner → ReAct循环 → Summarizer
- 复杂模式：Main → Planner → Plan-then-Execute → Summarizer
- 对话类查询：Main 直接处理
- 用户反馈：Main → Feedback → Main（决策）
"""
from typing import Literal
from langgraph.graph import StateGraph, END
from graph.state import GlobalState
from agent_nodes import (
    main_agent_node,
    planner_agent_node,
    executor_agent_node,
    summarizer_agent_node,
    feedback_agent_node
)


def route_after_main(state: GlobalState) -> Literal["planner", "feedback", END]:
    """
    Main之后的路由决策
    """
    if state.get("is_complete", False):
        return END
    if state.get("needs_clarification", False):
        return END
    next_agent = state.get("next_agent", "planner")
    if next_agent == "feedback":
        return "feedback"
    return "planner"


def route_after_feedback(state: GlobalState) -> Literal["main", END]:
    """
    Feedback之后的路由决策
    Feedback 总是回到 Main，让 Main 做决策
    """
    if state.get("is_complete", False):
        return END
    return "main"


def route_after_planner(state: GlobalState) -> Literal["executor", END]:
    """
    Planner之后的路由决策
    """
    if state.get("needs_clarification", False):
        return END
    return "executor"


def create_travel_planning_graph():
    """
    创建旅游规划工作流图
    """
    workflow = StateGraph(GlobalState)
    
    workflow.add_node("main", main_agent_node)
    workflow.add_node("planner", planner_agent_node)
    workflow.add_node("executor", executor_agent_node)
    workflow.add_node("summarizer", summarizer_agent_node)
    workflow.add_node("feedback", feedback_agent_node)
    
    workflow.set_entry_point("main")
    
    workflow.add_conditional_edges(
        "main",
        route_after_main,
        {
            "planner": "planner",
            "feedback": "feedback",
            END: END
        }
    )
    
    workflow.add_conditional_edges(
        "feedback",
        route_after_feedback,
        {
            "main": "main",
            END: END
        }
    )
    
    workflow.add_conditional_edges(
        "planner",
        route_after_planner,
        {
            "executor": "executor",
            END: END
        }
    )
    
    workflow.add_edge("executor", "summarizer")
    workflow.add_edge("summarizer", END)
    
    return workflow.compile()


travel_graph = create_travel_planning_graph()
