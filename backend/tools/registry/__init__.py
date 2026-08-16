"""
工具注册表 - 按 MCP 服务器分文件管理

目录结构：
- base.py:         ToolDefinition dataclass（含 handler 字段）
- gaode.py:        高德地图 MCP 工具 + driving_cost_query 本地复合工具（距离+油价+耗时）
- server_12306.py: 12306 MCP 工具 + train_query 本地复合工具（中文城市名直达查票）
- bazi.py:         八字黄历 MCP 工具
- local.py:        本地工具（rag_search 知识库检索）

调用方式（workflow_nodes._call_mcp_tool 统一分发）：
- 本地工具（tool_type="local"）：走 tool_def.handler(**params)
- MCP 工具（tool_type="mcp"）：走 manager.call_tool(server_name, mcp_tool_name, **params)
"""
from typing import Dict, Any, List, Optional

from .base import ToolDefinition
from .gaode import GAODE_TOOLS
from .server_12306 import SERVER_12306_TOOLS
from .bazi import BAZI_TOOLS
from .local import LOCAL_TOOLS


# 所有可用工具的聚合列表
AVAILABLE_TOOLS: List[ToolDefinition] = GAODE_TOOLS + SERVER_12306_TOOLS + BAZI_TOOLS + LOCAL_TOOLS


def get_tool_by_name(tool_name: str) -> Optional[ToolDefinition]:
    """根据工具名称获取工具定义"""
    for tool in AVAILABLE_TOOLS:
        if tool.name == tool_name:
            return tool
    return None


# ═══════════════════════════════════════════════════════════
# information_query_node 专用：返回 bind_tools 格式的工具 schema
# ═══════════════════════════════════════════════════════════

def get_info_query_tool_schemas() -> List[Dict[str, Any]]:
    """
    返回 information_query_node 可用的工具 schema 列表，
    格式兼容 ChatOpenAI.bind_tools() / ChatAnthropic.bind_tools()。

    工具清单：weather / rag / poi / distance / ip_location / lucky_day
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "weather",
                "description": "查询指定城市的天气（当前天气与多日预报）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "城市名，如 杭州、北京"},
                    },
                    "required": ["city"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "rag",
                "description": "从旅游知识库检索攻略、美食、文化、景点推荐等信息",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索词，如 杭州美食、苏州园林攻略"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "poi",
                "description": "搜索POI地点（景点/餐厅/购物/住宿等）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keywords": {"type": "string", "description": "搜索关键词，如 西湖、火锅"},
                        "city": {"type": "string", "description": "城市名"},
                    },
                    "required": ["keywords"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "distance",
                "description": "查询两地驾车距离、预计时间和过路费",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "from_city": {"type": "string", "description": "出发城市"},
                        "to_city": {"type": "string", "description": "目的城市"},
                    },
                    "required": ["from_city", "to_city"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ip_location",
                "description": "获取用户当前IP所在城市（无需参数）",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lucky_day",
                "description": "查询指定日期的黄历信息（农历、干支、宜忌、八字等），适用于择日、运势、传统文化查询",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "日期 YYYY-MM-DD，如 2024-12-10"},
                    },
                    "required": ["date"],
                },
            },
        },
    ]
