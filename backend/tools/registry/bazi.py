"""八字黄历 MCP 工具（bazi Server）"""
from .base import ToolDefinition


BAZI_TOOLS = [
    ToolDefinition(
        name="lucky_day",
        description="查询指定日期的黄历信息，包括农历、干支、宜忌等。**强烈建议**为所有完整旅行规划查询黄历吉日，为用户提供中国传统文化参考。黄历信息是完整旅行方案的重要组成部分，可以增加方案的文化价值和实用性。",
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "日期，格式：'YYYY-MM-DD'，例如：'2024-12-10'"
                }
            },
            "required": ["date"]
        },
        tool_type="mcp",
        server_name="bazi Server",
        mcp_tool_name="getChineseCalendar"
    ),
]
