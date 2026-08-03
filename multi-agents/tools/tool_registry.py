"""
工具注册表 - 统一管理所有可用工具，供 ReAct Agent 选择
"""
from typing import Dict, Any, List, Optional
from dataclasses import dataclass


@dataclass
class ToolDefinition:
    """工具定义"""
    name: str  # 工具名称
    description: str  # 工具描述（供LLM理解何时使用）
    parameters: Dict[str, Any]  # 参数schema（JSON Schema格式）
    tool_type: str  # 工具类型: "rag", "mcp", "r1", "special"
    server_name: Optional[str] = None  # MCP服务器名称（仅MCP工具需要）
    mcp_tool_name: Optional[str] = None  # MCP工具名称（仅MCP工具需要）


# 所有可用工具的定义
AVAILABLE_TOOLS: List[ToolDefinition] = [
    # ========== RAG 工具 ==========
    ToolDefinition(
        name="rag_search",
        description="从知识库中检索旅游攻略和景点信息。当需要了解某个城市的旅游攻略、景点推荐、特色美食、最佳游玩时间等信息时使用。",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索查询词，例如：'苏州 景点'、'杭州 美食'、'北京 旅游攻略'"
                },
                "k": {
                    "type": "integer",
                    "description": "返回结果数量，默认3",
                    "default": 3
                }
            },
            "required": ["query"]
        },
        tool_type="rag"
    ),
    
    # ========== 高德地图 MCP 工具 ==========
    ToolDefinition(
        name="gaode_poi_search",
        description="搜索高德地图的POI（兴趣点）信息，获取实时的景点、餐厅、购物等地点信息。当需要查找具体的景点、餐厅、购物场所时使用。",
        parameters={
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "搜索关键词，例如：'苏州 景点'、'杭州 西湖'、'北京 故宫'"
                },
                "city": {
                    "type": "string",
                    "description": "城市名称，例如：'苏州'、'杭州'、'北京'"
                }
            },
            "required": ["keywords"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_text_search"
    ),
    
    ToolDefinition(
        name="gaode_hotel_search",
        description="搜索高德地图的酒店和民宿信息。当需要为用户推荐住宿时使用。可以根据预算和偏好搜索不同类型的酒店。",
        parameters={
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "搜索关键词，例如：'苏州 酒店'、'杭州 民宿'、'北京 经济型酒店'。可以根据预算调整：预算>500用'高端酒店'，300-500用'酒店'，<300用'经济型酒店'或'民宿'"
                },
                "city": {
                    "type": "string",
                    "description": "城市名称，例如：'苏州'、'杭州'"
                }
            },
            "required": ["keywords"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_text_search"
    ),
    
    ToolDefinition(
        name="gaode_weather",
        description="查询高德地图的天气预报信息。当需要了解旅行期间的天气情况时使用。可以查询当前天气和多日预报。",
        parameters={
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名称，例如：'苏州'、'杭州'、'北京'"
                }
            },
            "required": ["city"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_weather"
    ),
    
    ToolDefinition(
        name="gaode_geo",
        description="将地址转换为经纬度坐标。当需要获取城市或地点的坐标信息时使用（通常用于路线规划）。",
        parameters={
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": "地址名称，例如：'北京'、'上海'、'苏州'"
                }
            },
            "required": ["address"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_geo"
    ),
    
    ToolDefinition(
        name="gaode_driving",
        description="查询高德地图的驾车路线规划，包括距离、时间、过路费等信息。当需要对比自驾和公共交通方案时使用。",
        parameters={
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "起点坐标（经纬度），格式：'经度,纬度'，例如：'120.619585,31.299379'。需要先使用gaode_geo获取坐标。"
                },
                "destination": {
                    "type": "string",
                    "description": "终点坐标（经纬度），格式：'经度,纬度'，例如：'120.619585,31.299379'"
                }
            },
            "required": ["origin", "destination"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_direction_driving"
    ),
    
    ToolDefinition(
        name="gaode_regeo",
        description="逆地理编码：将经纬度坐标转换为具体地址信息（省/市/区/街道）。当需要根据坐标确定具体位置时使用，例如确认某个POI属于哪个区、是否方便游玩。",
        parameters={
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "经纬度坐标，格式：'经度,纬度'，例如：'116.397428,39.90923'"
                }
            },
            "required": ["location"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_regeocode"
    ),
    
    ToolDefinition(
        name="gaode_ip_location",
        description="IP定位：根据IP地址获取当前位置信息（省份、城市、城市编码）。可用于快速确定用户所在城市，辅助起终点判断。",
        parameters={
            "type": "object",
            "properties": {
                "ip": {
                    "type": "string",
                    "description": "IP地址，例如：'114.247.50.2'。如不传则定位请求来源IP。"
                }
            },
            "required": []
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_ip_location"
    ),
    
    ToolDefinition(
        name="gaode_bicycling",
        description="骑行路线规划：规划两地之间的骑行方案，考虑天桥、单行线、封路等情况。适用于市内短距离景点间的骑行导航，最大支持500km。",
        parameters={
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "起点坐标（经纬度），格式：'经度,纬度'"
                },
                "destination": {
                    "type": "string",
                    "description": "终点坐标（经纬度），格式：'经度,纬度'"
                }
            },
            "required": ["origin", "destination"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_bicycling"
    ),
    
    ToolDefinition(
        name="gaode_walking",
        description="步行路线规划：规划100km以内的步行方案。适用于市内景点间步行游览路线规划，例如：'从酒店步行到西湖需要多久'。",
        parameters={
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "起点坐标（经纬度），格式：'经度,纬度'"
                },
                "destination": {
                    "type": "string",
                    "description": "终点坐标（经纬度），格式：'经度,纬度'"
                }
            },
            "required": ["origin", "destination"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_direction_walking"
    ),
    
    ToolDefinition(
        name="gaode_transit",
        description="公交路线规划：综合火车、公交、地铁等公共交通方式规划通勤方案。跨城场景必须传起点城市与终点城市。适用于市内或跨城公共交通出行规划。",
        parameters={
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "起点坐标（经纬度），格式：'经度,纬度'"
                },
                "destination": {
                    "type": "string",
                    "description": "终点坐标（经纬度），格式：'经度,纬度'"
                },
                "city": {
                    "type": "string",
                    "description": "起点城市名称，跨城时必填"
                },
                "cityd": {
                    "type": "string",
                    "description": "终点城市名称，跨城时必填"
                }
            },
            "required": ["origin", "destination"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_direction_transit_integrated"
    ),
    
    ToolDefinition(
        name="gaode_distance",
        description="距离测量：测量两个经纬度坐标之间的直线距离和预计耗时。适用于快速估算两地的远近程度，辅助行程安排决策。",
        parameters={
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "起点坐标（经纬度），格式：'经度,纬度'"
                },
                "destination": {
                    "type": "string",
                    "description": "终点坐标（经纬度），格式：'经度,纬度'"
                }
            },
            "required": ["origin", "destination"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_distance"
    ),
    
    ToolDefinition(
        name="gaode_around_search",
        description="周边搜索：根据关键词和中心点坐标，搜索指定半径范围内的POI地点。适用于查找'酒店附近有什么餐厅''景点周边有什么购物'等场景。",
        parameters={
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "搜索关键词，例如：'餐厅'、'购物'、'地铁站'"
                },
                "location": {
                    "type": "string",
                    "description": "中心点经纬度，格式：'经度,纬度'"
                },
                "radius": {
                    "type": "integer",
                    "description": "搜索半径（米），默认1000",
                    "default": 1000
                }
            },
            "required": ["keywords", "location"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_around_search"
    ),
    
    ToolDefinition(
        name="gaode_detail_search",
        description="POI详情搜索：查询指定POI ID的详细信息，包括地址、商圈、类型、评分等。通常在关键词搜索或周边搜索后，用于获取某个具体地点的完整信息。",
        parameters={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "POI ID，从gaode_poi_search或gaode_around_search结果中获取"
                }
            },
            "required": ["id"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_search_detail"
    ),
    
    # ========== 12306 MCP 工具 ==========
    ToolDefinition(
        name="train_query",
        description="查询12306火车票信息，包括车次、时间、票价等。**同时会自动查询自驾路线**（距离、时间、过路费），为用户提供完整的交通方案对比。建议为所有完整旅行规划调用此工具。",
        parameters={
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "出发城市名称，例如：'上海'、'北京'"
                },
                "destination": {
                    "type": "string",
                    "description": "目的地城市名称，例如：'苏州'、'杭州'"
                },
                "date": {
                    "type": "string",
                    "description": "出发日期，格式：'YYYY-MM-DD'，例如：'2024-12-10'"
                }
            },
            "required": ["origin", "destination", "date"]
        },
        tool_type="mcp",
        server_name="12306 Server",
        mcp_tool_name="get-tickets"
    ),
    
    # ========== 八字黄历 MCP 工具 ==========
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
    
    # ========== 航班查询 MCP 工具 ==========
    ToolDefinition(
        name="flight_query",
        description="查询航班信息，包括航班号、起飞时间、降落时间、机型等。建议查询条件：1) 距离>800km的跨省出行（如上海-成都、北京-广州） 2) 用户明确需要航班 3) 有老人儿童同行且距离>500km。为用户提供完整的交通方案对比（高链vs航班）。",
        parameters={
            "type": "object",
            "properties": {
                "dep": {
                    "type": "string",
                    "description": "出发城市名，例如：'上海'、'北京'、'成都'（系统会自动转换为机场三字码）"
                },
                "arr": {
                    "type": "string",
                    "description": "目的地城市名，例如：'广州'、'深圳'、'成都'（系统会自动转换为机场三字码）"
                },
                "date": {
                    "type": "string",
                    "description": "出发日期，格式：'YYYY-MM-DD'，例如：'2024-12-10'"
                }
            },
            "required": ["dep", "arr", "date"]
        },
        tool_type="mcp",
        server_name="flight Server",
        mcp_tool_name="searchFlightsByDepArr"
    ),
    
    # ========== DeepSeek R1 工具 ==========
    ToolDefinition(
        name="r1_analysis",
        description="使用DeepSeek R1进行深度推理和优化分析。当遇到复杂问题需要深度分析时使用，例如：多城市路线优化、紧张预算优化、多重约束条件平衡等。注意：这个工具成本较高，只在真正需要深度分析时使用。",
        parameters={
            "type": "object",
            "properties": {
                "problem": {
                    "type": "string",
                    "description": "需要分析的问题描述"
                },
                "context": {
                    "type": "object",
                    "description": "上下文信息，包括已收集的所有信息"
                }
            },
            "required": ["problem", "context"]
        },
        tool_type="r1"
    ),
    
    # ========== 特殊工具 ==========
    ToolDefinition(
        name="final_answer",
        description="表示已经收集到足够的信息，可以生成最终答案了。当所有必要信息都已收集完成时使用此工具，系统将进入答案生成阶段。",
        parameters={
            "type": "object",
            "properties": {},
            "required": []
        },
        tool_type="special"
    ),
]


def get_tool_by_name(tool_name: str) -> Optional[ToolDefinition]:
    """根据工具名称获取工具定义"""
    for tool in AVAILABLE_TOOLS:
        if tool.name == tool_name:
            return tool
    return None


def get_all_tools() -> List[ToolDefinition]:
    """获取所有可用工具"""
    return AVAILABLE_TOOLS


def get_tools_description_for_llm() -> str:
    """生成供LLM使用的工具描述文本"""
    descriptions = []
    for tool in AVAILABLE_TOOLS:
        desc = f"- {tool.name}: {tool.description}"
        # 添加参数说明
        params = tool.parameters.get("properties", {})
        if params:
            param_list = []
            for param_name, param_schema in params.items():
                param_desc = param_schema.get("description", "")
                required = param_name in tool.parameters.get("required", [])
                required_mark = "（必需）" if required else "（可选）"
                param_list.append(f"  - {param_name}{required_mark}: {param_desc}")
            if param_list:
                desc += "\n  参数：\n" + "\n".join(param_list)
        descriptions.append(desc)
    
    return "\n\n".join(descriptions)

