"""Agents package - 固定 langgraph 工作流架构。

工作流节点按职责拆分到子模块：classify / params / info_query / transport /
city_planning / summarizer。共享基础工具位于 _common.py。

节点符号懒加载，避免 `import agent_nodes._observability` 时拉起 langchain。
"""
from importlib import import_module
from typing import Any

_EXPORTS = {
    "classify_node": ("agent_nodes.classify", "classify_node"),
    "conversation_reply_node": ("agent_nodes.classify", "conversation_reply_node"),
    "handle_feedback_node": ("agent_nodes.classify", "handle_feedback_node"),
    "extract_params_node": ("agent_nodes.params", "extract_params_node"),
    "ask_clarification_node": ("agent_nodes.params", "ask_clarification_node"),
    "information_query_node": ("agent_nodes.info_query", "information_query_node"),
    "simple_rag_search_node": ("agent_nodes.info_query", "simple_rag_search_node"),
    "transport_check_node": ("agent_nodes.transport", "transport_check_node"),
    "transport_select_node": ("agent_nodes.transport", "transport_select_node"),
    "budget_fail_node": ("agent_nodes.transport", "budget_fail_node"),
    "city_budget_allocation_node": ("agent_nodes.city_planning", "city_budget_allocation_node"),
    "plan_all_cities_concurrent_node": ("agent_nodes.city_planning", "plan_all_cities_concurrent_node"),
    "summarizer_node": ("agent_nodes.summarizer", "summarizer_node"),
}


def __getattr__(name: str) -> Any:
    spec = _EXPORTS.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(spec[0])
    value = getattr(module, spec[1])
    globals()[name] = value
    return value


__all__ = list(_EXPORTS)
