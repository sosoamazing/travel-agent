"""工具定义基类 - 所有工具注册文件共享的 dataclass"""
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass


@dataclass
class ToolDefinition:
    """工具定义

    - MCP 工具：填 server_name + mcp_tool_name，由 MCPToolManager.call_tool 执行
    - 本地工具：填 handler（async 函数），由 _call_mcp_tool 直接调用
    """
    name: str  # 工具名称
    description: str  # 工具描述（供 LLM 理解何时使用）
    parameters: Dict[str, Any]  # 参数 schema（JSON Schema 格式）
    tool_type: str  # "mcp" / "local"
    server_name: Optional[str] = None  # MCP 服务器名称（仅 MCP 工具）
    mcp_tool_name: Optional[str] = None  # MCP 工具名称（仅 MCP 工具）
    handler: Optional[Callable] = None  # 本地工具的 async 执行函数（仅本地工具）
