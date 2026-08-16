"""
LangGraph 固定工作流定义 - 替代动态 ReAct DAG
流程由边固定，LLM 仅在节点内部做选择：

新流程：
  意图分类 → 对话/反馈/信息查询/旅行规划 四条分支
  旅行规划：
    参数提取 → 澄清 / 简单查询 / (市内旅游 | 跨城旅游)
    跨城旅游：交通全量预检 → 选定交通计划 → LLM 按城市分配剩余预算
                                                      → 全城市并发城内规划 → 总结
    市内旅游：LLM 按城市分配预算（交通=0）→ 全城市并发城内规划 → 总结
  超预算（交通累加 / 并发汇总后）直接终止并告知用户。

全城市并发城内规划：
  (独立执行：景点检索 → planner 重规划[最多3次] → 酒店检索) × N 城（asyncio.gather）
  每城使用 city_budgets_map 分配到的独立预算校验，互不干扰
"""
from langgraph.graph import StateGraph, END
from graph.state import GlobalState
from agent_nodes import (
    classify_node,
    conversation_reply_node,
    handle_feedback_node,
    information_query_node,
    extract_params_node,
    ask_clarification_node,
    simple_rag_search_node,
    transport_check_node,
    transport_select_node,
    budget_fail_node,
    city_budget_allocation_node,
    plan_all_cities_concurrent_node,
    summarizer_node,
)
import logging

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 路由函数
# ═══════════════════════════════════════════════════════════

def route_classify(state):
    qt = state.get("query_type")
    target = ("conversation_reply" if qt == "conversation"
              else "handle_feedback" if qt == "feedback"
              else "information_query" if qt == "information"
              else "extract_params")
    logger.info(f"🔀 [route_classify] query_type={qt!r} → {target}")
    return target


def route_clarify(state):
    pc = state.get("planner_context") or {}
    origin = pc.get("origin", "")
    cities = state.get("cities", []) or []
    if pc.get("needs_clarification"):
        target = "ask_clarification"
        reason = "需要澄清关键信息"
    elif not pc.get("budget") or not origin:
        target = "simple_rag_search"
        reason = f"简单查询（budget={pc.get('budget')!r}, origin={origin!r}）"
    elif cities and all(c == origin for c in cities):
        target = "intra_city_travel"
        reason = f"市内旅游（cities={cities}, origin={origin!r}）"
    else:
        target = "transport_check"
        reason = f"跨城旅游（cities={cities}, origin={origin!r}）"
    logger.info(f"🔀 [route_clarify] {reason} → {target}")
    return target


def route_transport(state):
    """交通预检后：超预算 → 终止；否则 → 选定交通计划"""
    over = bool(state.get("over_budget"))
    target = "budget_fail" if over else "transport_select"
    spent = float(state.get("spent_budget", 0) or 0)
    total = float(state.get("total_budget", 0) or 0)
    logger.info(f"🔀 [route_transport] over_budget={over}, spent={spent:.0f}/{total:.0f} → {target}")
    return target


def route_after_concurrent(state):
    """并发城内规划完成：超预算(汇总后) → budget_fail；否则 → summarizer"""
    over = bool(state.get("over_budget"))
    target = "budget_fail" if over else "summarizer"
    spent = float(state.get("spent_budget", 0) or 0)
    total = float(state.get("total_budget", 0) or 0)
    logger.info(f"🔀 [route_after_concurrent] over_budget={over}, spent={spent:.0f}/{total:.0f} → {target}")
    return target


# ═══════════════════════════════════════════════════════════
# 主图
# ═══════════════════════════════════════════════════════════

def create_travel_planning_graph(checkpointer=None):
    """创建固定旅游规划工作流主图（全城市并发版）。

    Args:
        checkpointer: 可选。LangGraph checkpointer（如 AsyncPostgresSaver）。
            传入后每次 invoke 都会在每个节点完成后持久化状态快照，配合同一个
            thread_id 即可实现崩溃后的断点续跑。不传则为无状态图（兼容旧调用/测试）。
    """
    workflow = StateGraph(GlobalState)

    # ── 注册原子节点 ──
    workflow.add_node("classify", classify_node)
    workflow.add_node("conversation_reply", conversation_reply_node)
    workflow.add_node("handle_feedback", handle_feedback_node)
    workflow.add_node("information_query", information_query_node)
    workflow.add_node("extract_params", extract_params_node)
    workflow.add_node("ask_clarification", ask_clarification_node)
    workflow.add_node("simple_rag_search", simple_rag_search_node)
    workflow.add_node("transport_check", transport_check_node)
    workflow.add_node("transport_select", transport_select_node)
    workflow.add_node("budget_fail", budget_fail_node)
    workflow.add_node("city_budget_allocation", city_budget_allocation_node)
    workflow.add_node("plan_all_cities_concurrent", plan_all_cities_concurrent_node)
    workflow.add_node("summarizer", summarizer_node)

    # ── 入口 ──
    workflow.set_entry_point("classify")

    # ── 意图分支（4 路） ──
    workflow.add_conditional_edges(
        "classify", route_classify,
        {
            "conversation_reply": "conversation_reply",
            "handle_feedback": "handle_feedback",
            "information_query": "information_query",
            "extract_params": "extract_params",
        },
    )
    workflow.add_edge("conversation_reply", END)
    workflow.add_edge("handle_feedback", END)
    workflow.add_edge("information_query", END)

    # ── 参数提取 → 澄清 / 简单查询 / 市内旅游 / 跨城交通预检 ──
    workflow.add_conditional_edges(
        "extract_params", route_clarify,
        {
            "ask_clarification": "ask_clarification",
            "simple_rag_search": "simple_rag_search",
            # 市内旅游 → 先做预算分配（transport=0，总预算直接当 pool）→ 并发城内
            "intra_city_travel": "city_budget_allocation",
            # 跨城旅游 → 交通预检 → ... → 预算分配 → 并发城内
            "transport_check": "transport_check",
        },
    )
    workflow.add_edge("ask_clarification", END)
    workflow.add_edge("simple_rag_search", "summarizer")

    # ── 交通预检 → 超预算终止 / 选定交通计划 ──
    workflow.add_conditional_edges(
        "transport_check", route_transport,
        {
            "budget_fail": "budget_fail",
            "transport_select": "transport_select",
        },
    )
    workflow.add_edge("budget_fail", END)

    # ── 选定交通计划 → 按城市分配剩余预算 ──
    workflow.add_edge("transport_select", "city_budget_allocation")

    # ── 预算分配（跨城 & 市内旅游都会走这里）→ 全城市并发城内 ──
    workflow.add_edge("city_budget_allocation", "plan_all_cities_concurrent")

    # ── 并发城内规划完成 → 超预算终止 / 总结 ──
    workflow.add_conditional_edges(
        "plan_all_cities_concurrent", route_after_concurrent,
        {
            "budget_fail": "budget_fail",
            "summarizer": "summarizer",
        },
    )

    workflow.add_edge("summarizer", END)

    return workflow.compile(checkpointer=checkpointer)


travel_graph = create_travel_planning_graph()
