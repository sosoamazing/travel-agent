"""
LangGraph 全局状态定义 - 固定流程架构
状态字段服务于：多城市循环 / 预算累加校验 / 重规划 / 工具结果累积
"""
from typing import TypedDict, List, Optional, Annotated, Dict, Any
from langchain_core.messages import BaseMessage
import operator


class GlobalState(TypedDict):
    """全局上下文 - 固定 langgraph 工作流"""

    # ========== 对话/会话相关 ==========
    messages: Annotated[List[BaseMessage], operator.add]
    user_query: Optional[str]
    session_id: Optional[str]         # 当前会话ID（记忆系统键）
    user_id: Optional[str]            # 当前用户ID（记忆系统键，默认 default_user）

    # ========== 意图分类 ==========
    query_type: Optional[str]  # conversation | feedback | travel

    # ========== 提取的旅行参数（单次提取） ==========
    planner_context: Dict[str, Any]

    # ========== 多城市循环 ==========
    cities: List[str]                # 按游览顺序排列的城市列表（不含出发地）
    current_city_index: int
    current_city: Optional[str]
    has_next_city: bool              # next_city_gate 路由标记

    # ========== 预算跟踪 ==========
    total_budget: float
    spent_budget: float              # 累计已花（交通 + 已完成城市的景点/酒店）
    spent_before_city: float         # 进入当前城市规划前的累计花销（用于重规划时回滚）
    transport_costs: Dict[str, float]   # leg_key "from->to" -> 费用
    over_budget: bool
    budget_message: Optional[str]

    # ========== 记忆系统 ==========
    memory_flags: Optional[Dict[str, str]]  # {城市: "keep"/"replan"}，预算分配节点产出
    city_plans_kept: Optional[bool]         # 本次是否复用了工作记忆中的已完成计划

    # ========== 当前城市工作流数据 ==========
    current_attractions: List[Dict]      # 候选景点（含备份）
    current_route_plan: Dict[str, Any]   # 路线/日程/景点间交通
    current_hotels: List[Dict]
    replan_count: int                    # 当前城市重规划次数

    # ========== 跨城交通时刻表（供城内规划使用） ==========
    # {"城市名": {"start_time":"10:30","start_location":"XX站","end_time":"17:00","end_location":"XX站"}}
    city_transport_context: Dict[str, Dict]

    # ========== 累计结果（供总结） ==========
    city_plans: Annotated[List[Dict], operator.add]   # 每个城市的完整计划
    tool_results: Annotated[List[Dict], operator.add]
    rag_results_history: Annotated[List[str], operator.add]

    # ========== 控制 / 输出 ==========
    final_answer: Optional[str]
    is_complete: bool
    # 兼容旧字段（app.py 会重置）
    current_agent: Optional[str]
    next_agent: Optional[str]
