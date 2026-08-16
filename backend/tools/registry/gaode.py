"""高德地图 MCP 工具 + 本地复合工具（Gaode Server）

包含：
- 12 个高德 MCP 工具定义（POI/酒店/天气/地理编码/驾车/逆地理/IP定位/骑行/步行/公交/距离/周边/详情）
- driving_cost_query 本地复合工具：组合 gaode_geo + gaode_driving + 本地油价计算，
  一次返回距离、耗时、油费、高速费、总费用
- 公共辅助函数：_unwrap_gaode（高德返回值解包）、_geo_cache、_query_driving_details、_calc_driving_cost
"""
from typing import Dict, Any, Tuple
import json
import logging

from .base import ToolDefinition
from config.settings import (
    FUEL_PRICE_PER_LITER, FUEL_CONSUMPTION_PER_100KM, HIGHWAY_TOLL_PER_KM,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 公共辅助函数（workflow_nodes 也需要 _unwrap_gaode）
# ═══════════════════════════════════════════════════════════

# 地理编码缓存，避免重复查询同一个城市
_geo_cache: Dict[str, str] = {}


def _unwrap_gaode(data: Any) -> Any:
    """高德 MCP 返回值解包：兼容新结构 {"return": [{...}]} 和旧顶层结构 {...}。

    新版 ModelScope 高德 MCP 把所有结果统一包在 `return` 数组里（如 maps_geo 返回
    `{"return": [{"location": "...", ...}]}`）。本函数把 `return[0]` 解出来；
    老结构或非高德结构直接透传。
    """
    if isinstance(data, dict):
        ret = data.get("return")
        if isinstance(ret, list) and ret:
            return ret[0]
    return data


async def _query_driving_details(manager, from_city: str, to_city: str) -> Dict[str, float]:
    """查询两城市间驾车距离（公里）和耗时（小时），失败返回空 dict"""
    async def _get_loc(city: str) -> str:
        if city in _geo_cache:
            return _geo_cache[city]
        raw = await manager.call_tool("Gaode Server", "maps_geo", address=city)
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            data = _unwrap_gaode(data)
            if isinstance(data, dict):
                loc = data.get("location", "")
                if loc:
                    _geo_cache[city] = loc
                    return loc
        except Exception:
            pass
        return ""

    from_loc = await _get_loc(from_city)
    to_loc = await _get_loc(to_city)
    if not from_loc or not to_loc:
        return {}

    dist_raw = await manager.call_tool(
        "Gaode Server", "maps_direction_driving",
        origin=from_loc, destination=to_loc
    )
    try:
        dist_data = json.loads(dist_raw) if isinstance(dist_raw, str) else dist_raw
        dist_data = _unwrap_gaode(dist_data)
        if isinstance(dist_data, dict):
            paths = (dist_data.get("route") or {}).get("paths") or []
            if not paths:
                paths = dist_data.get("paths") or []
            if paths and paths[0].get("distance") is not None:
                distance_km = float(paths[0]["distance"]) / 1000.0
                # 高德返回 duration 单位为秒
                duration_sec = float(paths[0].get("duration", 0) or 0)
                duration_hours = round(duration_sec / 3600.0, 1) if duration_sec > 0 else 0.0
                return {"distance_km": round(distance_km, 2), "duration_hours": duration_hours}
    except Exception:
        pass
    return {}


def _calc_driving_cost(distance_km: float) -> Tuple[float, Dict[str, float]]:
    """根据驾车里程计算自驾费用（油费 + 高速费）。

    返回 (total_cost, breakdown)，breakdown 含 fuel_cost, toll_cost, distance_km。
    """
    if distance_km <= 0:
        return 0.0, {"fuel_cost": 0, "toll_cost": 0, "distance_km": 0}
    # 油费 = 里程(km) × 油耗(L/100km) / 100 × 油价(元/L)
    fuel_cost = round(distance_km * FUEL_CONSUMPTION_PER_100KM / 100.0 * FUEL_PRICE_PER_LITER, 2)
    # 高速费 = 里程(km) × 高速费单价(元/km)
    toll_cost = round(distance_km * HIGHWAY_TOLL_PER_KM, 2)
    total_cost = round(fuel_cost + toll_cost, 2)
    return total_cost, {
        "fuel_cost": fuel_cost,
        "toll_cost": toll_cost,
        "distance_km": round(distance_km, 2),
    }


# ═══════════════════════════════════════════════════════════
# 本地复合工具 handler
# ═══════════════════════════════════════════════════════════

async def poi_search_handler(**kwargs) -> str:
    """POI 精简搜索 handler：调用高德 maps_text_search 后只保留 name/address/typecode。

    与 gaode_poi_search 等价，但返回结果仅含 name、address、typecode 三个字段，
    丢弃 id、photos 等冗余字段，减少 token 占用。
    """
    from tools.mcp_tools import get_mcp_manager
    manager = await get_mcp_manager()

    keywords = kwargs.get("keywords", "")
    city = kwargs.get("city", "")

    raw = await manager.call_tool(
        "Gaode Server", "maps_text_search",
        keywords=keywords, city=city,
    )

    # 解析 POI 列表（兼容 pois / return / data / 顶层 list 结构）
    pois: list = []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        data = _unwrap_gaode(data)
        if isinstance(data, dict):
            for key in ("pois", "return", "data"):
                val = data.get(key)
                if isinstance(val, list):
                    pois = val
                    break
            else:
                pois = data.get("pois") or []
        elif isinstance(data, list):
            pois = data
    except Exception:
        pois = []

    # 只保留 name / address / typecode 三个字段
    filtered = []
    for p in pois:
        if not isinstance(p, dict):
            continue
        filtered.append({
            "name": p.get("name", ""),
            "address": p.get("address", ""),
            "typecode": p.get("typecode", ""),
        })

    # 从 PostgreSQL 字典表取出 typecode 的中文描述，替换 typecode 字段（DB 不可用时保留原码）
    from tools.typecode_db import aget_typecode_desc
    for item in filtered:
        desc = await aget_typecode_desc(item["typecode"])
        if desc:
            item["typecode"] = desc

    logger.info(f"  🔍 POI精简搜索 {keywords} ({city}): 共 {len(filtered)} 条结果")
    return json.dumps(filtered, ensure_ascii=False)


async def driving_cost_handler(**kwargs) -> str:
    """自驾费用查询 handler：距离 + 油价 + 耗时

    组合高德 maps_geo（地理编码）+ maps_direction_driving（驾车路线）+ 本地油价计算，
    一次返回距离、耗时、油费、高速费、总费用。
    """
    from tools.mcp_tools import get_mcp_manager
    manager = await get_mcp_manager()

    from_city = kwargs.get("from", "")
    to_city = kwargs.get("to", "")

    driving_info = await _query_driving_details(manager, from_city, to_city)
    distance_km = driving_info.get("distance_km", 0.0)
    duration_hours = driving_info.get("duration_hours", 0.0)
    cost, breakdown = _calc_driving_cost(distance_km)

    result = {
        "mode": "driving",
        "from": from_city,
        "to": to_city,
        "distance_km": distance_km,
        "duration_hours": duration_hours,
        "fuel_cost": breakdown["fuel_cost"],
        "toll_cost": breakdown["toll_cost"],
        "total_cost": cost,
    }
    logger.info(f"  🚗 自驾查询 {from_city}→{to_city}: {distance_km:.0f}km, {duration_hours}h, 费用{cost:.0f}元")
    return json.dumps(result, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
# 高德 MCP 工具定义
# ═══════════════════════════════════════════════════════════

GAODE_TOOLS = [
    # ── 本地精简工具：POI 搜索只返回 name/address/typecode ──
    ToolDefinition(
        name="gaode_poi_search_lite",
        description="搜索高德地图的POI（兴趣点）信息，获取实时的景点、餐厅、购物等地点信息。当需要查找具体的景点、餐厅、购物场所时使用。返回结果仅含名称、地址、类型码三个字段，比gaode_poi_search更精简。",
        parameters={
            "type": "object",
            "properties": {
                "keywords": {"type": "string", "description": "搜索关键词，例如：'苏州 景点'、'杭州 西湖'、'北京 故宫'"},
                "city": {"type": "string", "description": "城市名称，例如：'苏州'、'杭州'、'北京'"}
            },
            "required": ["keywords"]
        },
        tool_type="local",
        handler=poi_search_handler,
    ),

    ToolDefinition(
        name="gaode_hotel_search",
        description="搜索高德地图的酒店和民宿信息。当需要为用户推荐住宿时使用。可以根据预算和偏好搜索不同类型的酒店。",
        parameters={
            "type": "object",
            "properties": {
                "keywords": {"type": "string", "description": "搜索关键词，例如：'苏州 酒店'、'杭州 民宿'。预算>500用'高端酒店'，300-500用'酒店'，<300用'经济型酒店'或'民宿'"},
                "city": {"type": "string", "description": "城市名称"}
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
                "city": {"type": "string", "description": "城市名称，例如：'苏州'、'杭州'、'北京'"}
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
                "address": {"type": "string", "description": "地址名称，例如：'北京'、'上海'、'苏州'"}
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
                "origin": {"type": "string", "description": "起点坐标（经纬度），格式：'经度,纬度'，例如：'120.619585,31.299379'。需要先使用gaode_geo获取坐标。"},
                "destination": {"type": "string", "description": "终点坐标（经纬度），格式：'经度,纬度'"}
            },
            "required": ["origin", "destination"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_direction_driving"
    ),

    ToolDefinition(
        name="gaode_regeo",
        description="逆地理编码：将经纬度坐标转换为具体地址信息（省/市/区/街道）。当需要根据坐标确定具体位置时使用。",
        parameters={
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "经纬度坐标，格式：'经度,纬度'，例如：'116.397428,39.90923'"}
            },
            "required": ["location"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_regeocode"
    ),

    ToolDefinition(
        name="gaode_ip_location",
        description="IP定位：根据IP地址获取当前位置信息（省份、城市、城市编码）。可用于快速确定用户所在城市。",
        parameters={
            "type": "object",
            "properties": {
                "ip": {"type": "string", "description": "IP地址，例如：'114.247.50.2'。如不传则定位请求来源IP。"}
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
                "origin": {"type": "string", "description": "起点坐标（经纬度），格式：'经度,纬度'"},
                "destination": {"type": "string", "description": "终点坐标（经纬度），格式：'经度,纬度'"}
            },
            "required": ["origin", "destination"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_bicycling"
    ),

    ToolDefinition(
        name="gaode_walking",
        description="步行路线规划：规划100km以内的步行方案。适用于市内景点间步行游览路线规划。",
        parameters={
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "起点坐标（经纬度），格式：'经度,纬度'"},
                "destination": {"type": "string", "description": "终点坐标（经纬度），格式：'经度,纬度'"}
            },
            "required": ["origin", "destination"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_direction_walking"
    ),

    ToolDefinition(
        name="gaode_transit",
        description="公交路线规划：综合火车、公交、地铁等公共交通方式规划通勤方案。跨城场景必须传起点城市与终点城市。",
        parameters={
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "起点坐标（经纬度），格式：'经度,纬度'"},
                "destination": {"type": "string", "description": "终点坐标（经纬度），格式：'经度,纬度'"},
                "city": {"type": "string", "description": "起点城市名称，跨城时必填"},
                "cityd": {"type": "string", "description": "终点城市名称，跨城时必填"}
            },
            "required": ["origin", "destination"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_direction_transit_integrated"
    ),

    ToolDefinition(
        name="gaode_distance",
        description="距离测量：测量两个经纬度坐标之间的直线距离和预计耗时。适用于快速估算两地的远近程度。",
        parameters={
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "起点坐标（经纬度），格式：'经度,纬度'"},
                "destination": {"type": "string", "description": "终点坐标（经纬度），格式：'经度,纬度'"}
            },
            "required": ["origin", "destination"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_distance"
    ),

    ToolDefinition(
        name="gaode_around_search",
        description="周边搜索：根据关键词和中心点坐标，搜索指定半径范围内的POI地点。适用于查找'酒店附近有什么餐厅'等场景。",
        parameters={
            "type": "object",
            "properties": {
                "keywords": {"type": "string", "description": "搜索关键词，例如：'餐厅'、'购物'、'地铁站'"},
                "location": {"type": "string", "description": "中心点经纬度，格式：'经度,纬度'"},
                "radius": {"type": "integer", "description": "搜索半径（米），默认1000", "default": 1000}
            },
            "required": ["keywords", "location"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_around_search"
    ),

    ToolDefinition(
        name="gaode_detail_search",
        description="POI详情搜索：查询指定POI ID的详细信息，包括地址、商圈、类型、评分等。",
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "POI ID，从gaode_poi_search或gaode_around_search结果中获取"}
            },
            "required": ["id"]
        },
        tool_type="mcp",
        server_name="Gaode Server",
        mcp_tool_name="maps_search_detail"
    ),

    # ── 本地复合工具：距离 + 油价 + 耗时 ──
    ToolDefinition(
        name="driving_cost_query",
        description="查询两城市间自驾费用：一次返回距离(km)、耗时(小时)、油费、高速费、总费用。组合高德驾车路线规划与本地油价计算，适用于交通方式对比和预算估算。",
        parameters={
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "出发城市，例如：'北京'、'上海'"},
                "to": {"type": "string", "description": "目的城市，例如：'杭州'、'苏州'"}
            },
            "required": ["from", "to"]
        },
        tool_type="local",
        handler=driving_cost_handler,
    ),
]
