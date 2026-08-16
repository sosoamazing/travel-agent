"""
工具包
"""
from .rag_tool import query_travel_knowledge

__all__ = [
    "query_travel_knowledge"
]

# MCP工具需要时再延迟导入，避免启动时依赖openai-agents
def get_mcp_manager():
    from .mcp_tools import get_mcp_manager as _get_mcp_manager
    return _get_mcp_manager()

def MCPToolManager(*args, **kwargs):
    from .mcp_tools import MCPToolManager as _MCPToolManager
    return _MCPToolManager(*args, **kwargs)
