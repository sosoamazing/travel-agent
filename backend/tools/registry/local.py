"""本地工具（不依赖 MCP 服务器，直接调用本地实现）"""
import logging
from .base import ToolDefinition

logger = logging.getLogger(__name__)


async def rag_search_handler(**kwargs) -> str:
    """RAG 知识库检索 handler

    从旅游知识库检索攻略、美食、文化、景点推荐等信息。
    """
    from tools.rag_tool import query_travel_knowledge
    query = kwargs.get("query", "")
    return await query_travel_knowledge(query)


LOCAL_TOOLS = [
    ToolDefinition(
        name="rag_search",
        description="从旅游知识库检索攻略、美食、文化、景点推荐等信息。当需要获取目的地的旅游攻略、特色美食、文化背景等知识时使用。",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词，例如：'杭州 美食'、'苏州 园林 攻略'、'北京 文化'"
                }
            },
            "required": ["query"]
        },
        tool_type="local",
        handler=rag_search_handler,
    ),
]
