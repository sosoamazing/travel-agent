"""12306 MCP 工具 + 本地复合工具（12306 Server）

包含：
- 8 个 12306 MCP 工具定义（日期/城市车站/车站代码/电报码/余票/中转/经停站）
- train_query 本地复合工具：组合 get-station-code-of-citys + get-tickets，
  传入中文城市名即可查票（无需先手动查 station_code）
"""
import json
import logging
from .base import ToolDefinition

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 本地复合工具 handler
# ═══════════════════════════════════════════════════════════

async def train_query_handler(**kwargs) -> str:
    """12306 查询 handler：先取站点代码再取车票

    组合 get-station-code-of-citys（城市→站点代码）+ get-tickets（查余票），
    调用方只需传中文城市名，无需先手动查 station_code。
    """
    from tools.mcp_tools import get_mcp_manager
    manager = await get_mcp_manager()

    from_city = kwargs.get("origin", "")
    to_city = kwargs.get("destination", "")
    travel_date = kwargs.get("date", "")

    try:
        station_result = await manager.call_tool(
            "12306 Server", "get-station-code-of-citys", citys=f"{from_city}|{to_city}"
        )
        from_code = to_code = None
        if station_result and "error" not in str(station_result).lower():
            codes_data = json.loads(station_result) if isinstance(station_result, str) else station_result
            if isinstance(codes_data, dict):
                for city in [from_city, to_city]:
                    if city not in codes_data:
                        continue
                    entry = codes_data[city]
                    code = None
                    if isinstance(entry, list) and entry:
                        code = entry[0].get('station_code') or entry[0].get('code')
                    elif isinstance(entry, dict):
                        code = entry.get('station_code') or entry.get('code')
                    if code:
                        if city == from_city:
                            from_code = code
                        else:
                            to_code = code
        if from_code and to_code:
            return await manager.call_tool(
                "12306 Server", "get-tickets",
                fromStation=from_code, toStation=to_code, date=travel_date
            )
        return json.dumps({"error": "无法获取站点代码", "from": from_city, "to": to_city}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
# 12306 MCP 工具定义
# ═══════════════════════════════════════════════════════════

SERVER_12306_TOOLS = [
    ToolDefinition(
        name="12306_get_current_date",
        description="获取当前日期（上海时区 Asia/Shanghai），格式为 YYYY-MM-DD。用于解析用户提到的相对日期（如'明天'、'下周三'）。",
        parameters={
            "type": "object",
            "properties": {},
            "required": []
        },
        tool_type="mcp",
        server_name="12306 Server",
        mcp_tool_name="get-current-date"
    ),

    ToolDefinition(
        name="12306_get_stations_in_city",
        description="通过中文城市名查询该城市所有火车站的名称及 station_code 列表。例如查询'西安'会返回西安站、西安北站等。",
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "中文城市名称，例如：'北京'、'上海'、'西安'"}
            },
            "required": ["city"]
        },
        tool_type="mcp",
        server_name="12306 Server",
        mcp_tool_name="get-stations-code-in-city"
    ),

    ToolDefinition(
        name="12306_get_station_code",
        description="通过中文城市名查询代表该城市的火车站 station_code。支持多城市用|分隔（如'北京|上海'）。",
        parameters={
            "type": "object",
            "properties": {
                "citys": {"type": "string", "description": "城市名，多个城市用|分隔，例如：'北京' 或 '北京|上海'"}
            },
            "required": ["citys"]
        },
        tool_type="mcp",
        server_name="12306 Server",
        mcp_tool_name="get-station-code-of-citys"
    ),

    ToolDefinition(
        name="12306_get_station_code_by_name",
        description="通过具体的中文车站名查询 station_code。例如'北京南'→'VNP'。支持多车站用|分隔。",
        parameters={
            "type": "object",
            "properties": {
                "stationNames": {"type": "string", "description": "车站名称，多个用|分隔，例如：'北京南|上海虹桥'"}
            },
            "required": ["stationNames"]
        },
        tool_type="mcp",
        server_name="12306 Server",
        mcp_tool_name="get-station-code-by-names"
    ),

    ToolDefinition(
        name="12306_get_station_by_telecode",
        description="通过车站的 station_telecode（3位字母编码）查询车站详细信息，包括名称、拼音、所属城市等。例如输入 'BJP' 可查询北京站详情。",
        parameters={
            "type": "object",
            "properties": {
                "stationTelecode": {"type": "string", "description": "车站的 station_telecode (3位字母编码)，例如 'BJP'"}
            },
            "required": ["stationTelecode"]
        },
        tool_type="mcp",
        server_name="12306 Server",
        mcp_tool_name="get-station-by-telecode"
    ),

    ToolDefinition(
        name="12306_get_tickets",
        description="查询12306火车票余票信息。支持按车型（高铁/动车/直达/特快/快速）、出发时间、排序方式筛选。返回车次、时间、历时、票价、余票数量等详细信息。",
        parameters={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "查询日期，格式 YYYY-MM-DD，例如 '2026-08-15'"},
                "fromStation": {"type": "string", "description": "出发地的 station_code（如 BJP）。必须先用 12306_get_station_code / 12306_get_station_code_by_name 查询得到，严禁直接传中文地名。"},
                "toStation": {"type": "string", "description": "到达地的 station_code（如 SHH）。必须先用 12306_get_station_code / 12306_get_station_code_by_name 查询得到，严禁直接传中文地名。"},
                "trainFilterFlags": {"type": "string", "description": "车次筛选：G=高铁/动车, D=动车, Z=直达, T=特快, K=快速。可组合如 'GD'。", "default": ""},
                "earliestStartTime": {"type": "number", "description": "最早出发时间 (0-24)", "default": 0},
                "latestStartTime": {"type": "number", "description": "最迟出发时间 (0-24)", "default": 24},
                "sortFlag": {"type": "string", "description": "排序方式：startTime/arriveTime/duration", "default": ""},
                "sortReverse": {"type": "boolean", "description": "是否逆向排序", "default": False},
                "limitedNum": {"type": "number", "description": "返回余票数量限制，0=不限", "default": 0},
                "format": {"type": "string", "description": "返回格式：text/csv/json", "default": "text"}
            },
            "required": ["date", "fromStation", "toStation"]
        },
        tool_type="mcp",
        server_name="12306 Server",
        mcp_tool_name="get-tickets"
    ),

    ToolDefinition(
        name="12306_get_interline_tickets",
        description="查询12306中转余票信息。当没有直达车或用户想经某地转车时使用。支持指定中转城市，返回中转方案。",
        parameters={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "查询日期，格式 YYYY-MM-DD"},
                "fromStation": {"type": "string", "description": "出发地的 station_code（如 BJP）。必须先用 12306_get_station_code / 12306_get_station_code_by_name 查询得到，严禁直接传中文地名。"},
                "toStation": {"type": "string", "description": "到达地的 station_code（如 SHH）。必须先用 12306_get_station_code / 12306_get_station_code_by_name 查询得到，严禁直接传中文地名。"},
                "middleStation": {"type": "string", "description": "中转城市（可选）", "default": ""},
                "showWZ": {"type": "boolean", "description": "是否显示无座车", "default": False},
                "trainFilterFlags": {"type": "string", "description": "车次筛选：G=高铁/城际, D=动车, Z=直达特快, T=特快, K=快速, O=其他, F=复兴号, S=智能动车组。可组合如 'GD'。", "default": ""},
                "earliestStartTime": {"type": "number", "description": "最早出发时间 (0-24)", "default": 0},
                "latestStartTime": {"type": "number", "description": "最迟出发时间 (0-24)", "default": 24},
                "sortFlag": {"type": "string", "description": "排序方式：startTime/arriveTime/duration", "default": ""},
                "sortReverse": {"type": "boolean", "description": "是否逆向排序", "default": False},
                "limitedNum": {"type": "number", "description": "返回中转方案数量限制，默认 10", "default": 10}
            },
            "required": ["date", "fromStation", "toStation"]
        },
        tool_type="mcp",
        server_name="12306 Server",
        mcp_tool_name="get-interline-tickets"
    ),

    ToolDefinition(
        name="12306_get_train_route",
        description="查询特定车次的经停站信息，包括每个车站的到达时间、出发时间、历时等。例如查询 G1 次列车经停站。",
        parameters={
            "type": "object",
            "properties": {
                "trainCode": {"type": "string", "description": "车次号，例如：'G1033'、'Z1'"},
                "departDate": {"type": "string", "description": "出发日期，格式 YYYY-MM-DD"},
                "format": {"type": "string", "description": "返回格式：text/json", "default": "text"}
            },
            "required": ["trainCode", "departDate"]
        },
        tool_type="mcp",
        server_name="12306 Server",
        mcp_tool_name="get-train-route-stations"
    ),

    # ── 本地复合工具：中文城市名直接查票 ──
    ToolDefinition(
        name="train_query",
        description="火车票查询（中文城市名直达）：传入中文出发城市、目的城市和日期，自动完成 station_code 转换并查询余票。返回车次、时间、票价、余票等信息。",
        parameters={
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "出发城市中文名，例如：'北京'、'上海'"},
                "destination": {"type": "string", "description": "目的城市中文名，例如：'杭州'、'苏州'"},
                "date": {"type": "string", "description": "出发日期，格式 YYYY-MM-DD，例如：'2026-08-15'"}
            },
            "required": ["origin", "destination", "date"]
        },
        tool_type="local",
        handler=train_query_handler,
    ),
]
