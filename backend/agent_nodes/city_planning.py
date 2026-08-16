"""城内规划（核心）：并发城内规划节点、预算分配节点及相关 Pydantic models 与辅助函数。

从原 workflow_nodes.py 拆分而来。
"""
from typing import Dict, Any, List, Tuple
import asyncio
import json
import math
import re
import logging

from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage

from tools.rag_tool import query_travel_knowledge
from tools.registry.gaode import _unwrap_gaode
from config.settings import PLAN_MAX_REPLAN, CITY_PLAN_MAX_CONCURRENCY
from ._common import _LLM, _call_mcp_tool
from ._observability import node, node_scope
from memory import get_memory_manager
from memory.base import (
    CITY_DONE, CITY_PARTIAL, CITY_PENDING,
    FLAG_KEEP, FLAG_REPLAN,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# 景点提取
# ──────────────────────────────────────────────────────────

class _Attraction(BaseModel):
    name: str
    ticket_price: float = Field(default=0, description="门票价格（元/人）")
    visit_hours: float = Field(default=1.5, description="建议游玩时长（小时）")
    location: str = Field(default="", description="经纬度 lng,lat（来自高德POI）")
    description: str = Field(default="", description="简短特色描述")


class _AttractionList(BaseModel):
    attractions: List[_Attraction]


async def _extract_attractions(rag_raw: str, poi_raw: str, city: str,
                               preferences: List[str], user_query: str,
                               few_shot: str = "") -> List[Dict]:
    """综合 RAG + 高德 POI + LLM 训练记忆，提取候选景点（含备份）"""
    llm = _LLM(agent="attractions", temperature=0.3)
    few_shot_lines = f"\n\n{few_shot}" if few_shot else ""
    prompt = f"""请提取该城市的适量候选景点（6-10 个，需含备份景点），每个景点包含：
- name: 名称
- ticket_price: 门票价格（元/人），无门票填 0
- visit_hours: 建议游玩时长（小时），每个至少 1.5
- location: 经纬度 "lng,lat"（尽量从高德 POI 结果中取，没有则空）
- description: 一句话特色

要求：
1. 优先选择符合用户偏好的景点
2. 必须包含备份景点（多于实际游玩数量）
3. 若 RAG 与 POI 均为空，可凭训练记忆给出该城市知名景点

城市：{city}
用户偏好：{preferences}
用户查询：{user_query}
{few_shot_lines}

【RAG 知识库攻略】
{str(rag_raw) if rag_raw else "（空）"}

【高德 POI 搜索结果（含 location 坐标）】
{str(poi_raw) if poi_raw else "（空）"}
"""
    try:
        structured = llm.with_structured_output(_AttractionList)
        data: _AttractionList = await structured.ainvoke([HumanMessage(content=prompt)])
        return [a.model_dump() for a in data.attractions]
    except Exception as e:
        logger.warning(f"景点提取失败({city}): {e}")
        return []


# ──────────────────────────────────────────────────────────
# 城市路线规划
# ──────────────────────────────────────────────────────────

class _RouteStop(BaseModel):
    attraction_name: str
    visit_hours: float = 1.5
    ticket_price: float = 0
    note: str = ""


class _DayPlan(BaseModel):
    day: int
    starting_point: str = Field(default="", description="当日出发地（如：酒店、火车站、民宿名称）")
    departure_time: str = Field(default="08:30", description="当日建议出发时间")
    stops: List[_RouteStop]
    lunch: bool = True


class _CityRoutePlan(BaseModel):
    selected_attractions: List[str]
    days: List[_DayPlan]
    total_ticket_cost: float = 0
    inter_attraction_transport_cost: float = 0
    daily_starting_point: str = Field(default="", description="全市统一的每日出发参考点（酒店/车站区域）")
    reasoning: str = ""


def _estimate_transport_cost_from_distance(distance_km: float) -> float:
    """景点间交通费用启发式：步行/公交/打车"""
    if distance_km <= 0:
        return 0.0
    if distance_km < 2.5:
        return 0.0  # 步行
    if distance_km < 20:
        return 5.0  # 公交/地铁
    return round(distance_km * 2.5, 1)  # 打车近似


async def _query_leg_distance(origin_loc: str, dest_loc: str) -> float:
    """查询两坐标间驾车距离（公里），失败返回 -1。

    统一走 gaode_driving 工具分发（与 info_query 的距离查询一致）。
    """
    if not origin_loc or not dest_loc:
        return -1.0
    try:
        raw = await _call_mcp_tool("gaode_driving", origin=origin_loc, destination=dest_loc)
        data = json.loads(raw) if isinstance(raw, str) else raw
        data = _unwrap_gaode(data)  # 兼容 {"return": [{...}]} 新结构
        # 高德返回结构 route.paths[0].distance（米）
        paths = None
        if isinstance(data, dict):
            route = data.get("route") or {}
            paths = route.get("paths") or data.get("paths")
        if paths and isinstance(paths, list) and paths[0].get("distance") is not None:
            return float(paths[0]["distance"]) / 1000.0
        # 兜底：让 LLM 解析
        llm = _LLM(agent="route_distance", temperature=0.0)
        resp = await llm.ainvoke([HumanMessage(
            content=f"从以下高德驾车路线结果中提取距离（公里）。只输出数字。\n{str(raw)[:1000]}"
        )])
        m = re.search(r"\d+(?:\.\d+)?", resp.content)
        return float(m.group()) if m else -1.0
    except Exception:
        return -1.0


async def _plan_city_route(city: str, attractions: List[Dict], planner_context: Dict,
                           user_query: str, spent_before_city: float, total_budget: float,
                           is_replan: bool, replan_count: int,
                           transport_ctx: Dict = None) -> Dict[str, Any]:
    """让 plan-agent 选择景点、规划路线、校验时间与预算约束"""
    llm = _LLM(agent="route_plan", temperature=0.3)
    travel_days = planner_context.get("travel_days", 1) or 1
    preferences = planner_context.get("preferences", [])
    cities_count = max(1, planner_context.get("_cities_count", 1))
    suggested_days = max(1, travel_days // cities_count)

    few_shot = planner_context.get("memory_fewshot", "") or ""
    few_shot_lines = f"\n\n{few_shot}" if few_shot else ""

    budget_hint = ""
    if is_replan:
        budget_hint = (f"\n⚠️ 这是第 {replan_count} 次重规划，上一版超预算。"
                       f"请删减昂贵景点、增加免费景点、缩短景点间距离以降低费用。"
                       f"进入该市前已花 {spent_before_city:.0f} 元，总预算 {total_budget:.0f} 元。")

    # 跨城交通上下文（到站/出发时刻表）
    transport_hint = ""
    if transport_ctx:
        s_time = transport_ctx.get("start_time", "")
        s_loc = transport_ctx.get("start_location", "")
        e_time = transport_ctx.get("end_time", "")
        e_loc = transport_ctx.get("end_location", "")
        if s_time or s_loc or e_time or e_loc:
            transport_hint = (
                f"\n跨城交通约束："
                f"\n- 到达：{s_time or '未知时间'} 抵达「{s_loc or '未知位置'}」"
                f"\n- 离开：需在 {e_time or '未知时间'} 前前往「{e_loc or '未知位置'}」出发"
                f"\n- 首日 starting_point 应设为「{s_loc}」附近"
                f"\n- 末日最后应安排在「{e_loc}」附近以便前往下一程"
            )

    attraction_lines = "\n".join(
        f"- {a.get('name', '?')} | 门票{a.get('ticket_price', 0)}元 | 游玩{a.get('visit_hours', 1.5)}h | loc={a.get('location', '')} | {a.get('description', '')}"
        for a in attractions
    )

    prompt = f"""你是旅行路线规划师。请为「以上城市」规划景点游玩路线。

约束条件：
- 每个景点至少 1.5 小时游玩
- 每天午饭 + 休息按 2 小时计（在 _DayPlan 中 lunch=true 表示已安排）
- 时间要够用，景点数量要合理
- 计算总门票费用 total_ticket_cost（按 1 人计）
- inter_attraction_transport_cost 先给一个粗估（市内交通总和，元）

每日计划需包含：
- starting_point: 当日出发地（如酒店名、车站区域，首日通常为抵达车站/机场附近）
- departure_time: 建议出发时间（格式 HH:MM，如 08:30）
- daily_starting_point: 全市统一的每日出发参考点（供总结时统一描述）

输出 selected_attractions（选定的景点名列表）、days（每日行程，含 starting_point / departure_time / stops）、total_ticket_cost、inter_attraction_transport_cost、daily_starting_point、reasoning。

【城市】{city}
【建议游玩天数】{suggested_days} 天（总旅行天数 {travel_days}，共 {cities_count} 个城市）{budget_hint}{transport_hint}
{few_shot_lines}

候选景点：
{attraction_lines or "（无候选，请凭训练记忆给出该城市知名景点作为候选并选择）"}

用户偏好：{preferences}
用户查询：{user_query}
"""
    plan_dict: Dict[str, Any]
    try:
        structured = llm.with_structured_output(_CityRoutePlan)
        data: _CityRoutePlan = await structured.ainvoke([HumanMessage(content=prompt)])
        plan_dict = data.model_dump()
    except Exception as e:
        logger.warning(f"路线规划失败({city}): {e}")
        plan_dict = {
            "selected_attractions": [a.get("name", "") for a in attractions[:3]],
            "days": [{"day": 1, "stops": [], "lunch": True}],
            "total_ticket_cost": 0,
            "inter_attraction_transport_cost": 0,
            "reasoning": f"规划失败: {e}",
        }

    # 用高德驾车距离修正 inter_attraction_transport_cost（按选定景点顺序）
    name_to_loc = {a.get("name"): a.get("location", "") for a in attractions}

    # 计算选定景点的几何中心（到各点距离和最小），供后续按位置搜索酒店
    try:
        sel_locs: List[Tuple[float, float]] = []
        for _name in (plan_dict.get("selected_attractions") or []):
            loc = name_to_loc.get(_name, "") or ""
            if loc and "," in loc:
                lng_s, lat_s = loc.split(",", 1)
                try:
                    sel_locs.append((float(lng_s), float(lat_s)))
                except ValueError:
                    continue
        if sel_locs:
            cx, cy = _geometric_center(sel_locs)
            plan_dict["hotel_center"] = f"{cx:.6f},{cy:.6f}"
            max_km = max(_geo_distance_km(cx, cy, px, py) for px, py in sel_locs)
            # 搜索半径按景点覆盖范围自适应，最小 2km、最大 8km
            plan_dict["hotel_center_radius"] = int(min(8000, max(2000, max_km * 1200)))
            logger.info(
                f"📍 [{city}] 景点几何中心 {plan_dict['hotel_center']}（半径 {plan_dict['hotel_center_radius']}m，{len(sel_locs)} 个景点）"
            )
    except Exception as e:
        logger.warning(f"⚠️ [{city}] 计算景点几何中心失败: {e}")

    try:
        seg_distances: List[float] = []
        for day in plan_dict.get("days", []):
            stops = day.get("stops", [])
            for i in range(len(stops) - 1):
                o = name_to_loc.get(stops[i].get("attraction_name"), "")
                d = name_to_loc.get(stops[i + 1].get("attraction_name"), "")
                if o and d:
                    dist = await _query_leg_distance(o, d)
                    if dist >= 0:
                        seg_distances.append(dist)
        if seg_distances:
            plan_dict["inter_attraction_transport_cost"] = round(
                sum(_estimate_transport_cost_from_distance(x) for x in seg_distances), 1
            )
            plan_dict["_seg_distances_km"] = seg_distances
    except Exception as e:
        logger.warning(f"高德距离修正失败({city}): {e}")

    total_ticket = float(plan_dict.get("total_ticket_cost", 0) or 0)
    inter_cost = float(plan_dict.get("inter_attraction_transport_cost", 0) or 0)
    plan_dict["attractions_cost"] = round(total_ticket + inter_cost, 2)
    plan_dict["city"] = city
    plan_dict["replan_count"] = replan_count
    return plan_dict


# ──────────────────────────────────────────────────────────
# 酒店提取
# ──────────────────────────────────────────────────────────

class _Hotel(BaseModel):
    name: str
    price_per_night: float = 0
    address: str = ""
    location: str = ""
    features: str = ""


class _HotelList(BaseModel):
    hotels: List[_Hotel]


class _HotelChoice(BaseModel):
    """LLM 选定的酒店结果"""
    name: str = Field(description="选定的酒店名称（必须与候选列表中的名称完全一致）")
    reason: str = Field(description="选择理由，说明位置匹配度、价格与用户偏好契合度")


def _geo_distance_km(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """两点经纬度间的球面距离（公里，Haversine）。"""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(min(1.0, math.sqrt(a)))


def _geometric_center(points: List[Tuple[float, float]]) -> Tuple[float, float]:
    """到各点距离和最小的几何中位数（Weiszfeld 迭代），失败退化为均值中心。

    计算前按纬度余弦压缩经度，把经纬度近似投影到平面（城市尺度足够），
    算完再还原，避免高纬度经度被夸大。
    """
    if not points:
        return (0.0, 0.0)
    if len(points) == 1:
        return points[0]
    mid_lat = math.radians(sum(p[1] for p in points) / len(points))
    k = math.cos(mid_lat) or 1.0
    proj = [(p[0] * k, p[1]) for p in points]
    x = sum(p[0] for p in proj) / len(proj)
    y = sum(p[1] for p in proj) / len(proj)
    for _ in range(300):
        num_x = num_y = denom = 0.0
        for px, py in proj:
            d = math.hypot(px - x, py - y)
            if d < 1e-12:
                continue
            num_x += px / d
            num_y += py / d
            denom += 1.0 / d
        if denom == 0:
            break
        nx, ny = num_x / denom, num_y / denom
        if abs(nx - x) < 1e-9 and abs(ny - y) < 1e-9:
            x, y = nx, ny
            break
        x, y = nx, ny
    return (x / k, y)


def _decide_hotel_keywords(planner_context: Dict, user_query: str, remaining_budget: float,
                           route_plan: Dict = None, transport_ctx: Dict = None) -> str:
    """根据剩余预算决定酒店档次关键词，并结合路线位置（每日出发参考点 / 跨城到达站）生成位置词。

    位置优先级：
    1. route_plan.daily_starting_point（全市统一每日出发参考点）
    2. route_plan.days[0].starting_point（首日出发地，通常为抵达车站/机场附近）
    3. transport_ctx.start_location / end_location（跨城交通到达/离开站点）

    最终返回形如 "广州南站 经济型酒店"；无位置信息时退回纯档次词。
    """
    mentioned = any(k in user_query for k in ["酒店", "住宿", "民宿", "住"])
    if remaining_budget < 300:
        kw = "经济型酒店"
    elif remaining_budget < 800:
        kw = "酒店"
    else:
        kw = "中高端酒店"
    if not mentioned:
        kw = "酒店"  # 用户未提及，给通用词

    # 位置词：从路线规划与跨城交通中提取
    loc = _hotel_location_hint(route_plan, transport_ctx)
    if loc:
        return f"{loc} {kw}"
    return kw


def _hotel_location_hint(route_plan: Dict, transport_ctx: Dict) -> str:
    """从路线规划与跨城交通中提取酒店应靠近的位置词，无有效位置返回空串。

    关键词清洗规则（避免 LLM 长描述原样拼进搜索词导致搜空）：
    1. 去掉括号及其内容（（...）/ (...)，含未闭合的"（"），如"深圳湾万象城（南山区…"→"深圳湾万象城"
    2. 去掉"推荐入住/毗邻/附近/片区"等描述性词
    3. 长度超过 10 字时截断到最早出现的 站/机场/商圈/广场/中心/地铁 处；无关键词则截到前 10 字
    """
    if not route_plan and not transport_ctx:
        return ""
    candidates = []
    if route_plan:
        dp = (route_plan.get("daily_starting_point") or "").strip()
        if dp:
            candidates.append(dp)
        days = route_plan.get("days") or []
        if days:
            sp = (days[0].get("starting_point") or "").strip()
            if sp:
                candidates.append(sp)
    if transport_ctx:
        sl = (transport_ctx.get("start_location") or "").strip()
        if sl:
            candidates.append(sl)
        el = (transport_ctx.get("end_location") or "").strip()
        if el:
            candidates.append(el)
    # 过滤无意义词：纯"酒店/住宿/附近"等，或含"未知"
    bad = {"酒店", "住宿", "民宿", "酒店附近", "附近", "市中心酒店", "火车站", "车站", "机场"}
    # 描述性前缀词：整体剔除，避免"毗邻地铁…/推荐入住…"等长描述混入搜索关键词
    strip_words = ["推荐入住", "毗邻", "附近", "片区", "靠近"]
    for c in candidates:
        # 1. 去括号及其内容（同时处理未闭合的括号，直接截断到"（"前）
        c = re.sub(r"[（(].*?[）)]", "", c)
        c = re.split(r"[（(]", c)[0]
        # 2. 去描述性前缀词
        for w in strip_words:
            c = c.replace(w, "")
        c = c.replace("酒店", "").replace("附近", "").strip(" ，,、；")
        if not c or c in bad or "未知" in c or "请" in c:
            continue
        # 3. 过长截断：取最早出现的 站/机场/商圈/广场/中心/地铁 处，避免整段描述成为搜索词
        if len(c) > 10:
            best = None  # (位置, 关键词)，保留完整关键词不被截半
            for k in ["站", "机场", "商圈", "广场", "中心", "地铁"]:
                i = c.find(k)
                if i >= 0 and (best is None or i < best[0]):
                    best = (i, k)
            if best:
                c = c[: best[0] + len(best[1])]
            else:
                c = c[:10]
        c = c[:10].strip(" ，,、；")
        if c and c not in bad:
            return c
    return ""


async def _extract_hotels(hotel_raw: str, city: str, planner_context: Dict,
                          user_query: str, max_n: int = 5,
                          search_keywords: str = "") -> List[Dict]:
    """从高德酒店搜索结果中提取至多 max_n 个备选酒店，并把价格拼接到 POI JSON 中。

    流程：
    1. 从高德 MCP 返回的 POI 原始数据中解析酒店列表（保留 id/name/address/typecode/photos
       等所有高德字段）
    2. 把酒店名+地址+入住日期 + 提示词丢给 DeepSeek-Flash 轻量模型，让它输出每个酒店的估价
       （考虑淡旺季/节假日因素）
    3. 把 price_per_night 字段**直接拼接到 POI 原始 dict 上**，形成增强版 POI JSON 返回

    返回的每个 hotel dict 结构 = 高德 POI 原始字段 + {
        "price_per_night": float,   # Flash 估价（元/晚），已考虑入住日期的淡旺季因素
    }

    注意：高德 POI 不含房价，必须由 LLM 估价。
    """
    # 第一步：解析 POI 列表（保留所有原始字段）
    hotels_raw: List[Dict] = []
    try:
        data = json.loads(hotel_raw) if isinstance(hotel_raw, str) else hotel_raw
        if isinstance(data, dict):
            pois = data.get("pois") or data.get("return") or data.get("data") or []
            if isinstance(pois, list):
                hotels_raw = [p for p in pois if isinstance(p, dict)][:max_n]
        elif isinstance(data, list):
            hotels_raw = [p for p in data if isinstance(p, dict)][:max_n]
    except Exception:
        hotels_raw = []

    if not hotels_raw:
        kw_hint = f"（搜索关键词：{search_keywords}）" if search_keywords else ""
        logger.warning(f"⚠️ [{city}] POI 解析为空{kw_hint}，无法估价")
        return []

    # 第二步：丢给 DeepSeek-Flash 估价（只喂 name+address，省 token）
    # 注意：deepseek-v4-flash 不支持 response_format / with_structured_output，
    # 所以让 LLM 直接输出 JSON 数组字符串，自己解析。
    travel_date = planner_context.get("travel_date", "") or ""
    travel_days = planner_context.get("travel_days", 0) or 0

    hotels_brief = [
        {"name": h.get("name", ""), "address": h.get("address", "")}
        for h in hotels_raw
    ]
    date_hint = f"入住日期：{travel_date}（共 {travel_days} 天）" if travel_date else "入住日期：未知"
    prompt = f"""请为列表中每个酒店给出**该入住日期**的每晚房价估价（元/晚）。

价格规则：
- 凭酒店名称/品牌/类型，给出该城市该档次酒店的合理估价（元/晚）。
  例：经济型连锁（汉庭、如家、7天）≈150-300；中端（全季、桔子水晶、亚朵）≈300-600；
  高端（香格里拉、洲际、喜来登）≈800-1500；青年旅舍 ≈80-150；不知名小宾馆 ≈120-250。
- **必须考虑入住日期的淡旺季因素**：
  · 春节、五一、国庆、元旦等法定节假日：价格上浮 50%-150%
  · 暑期（7-8月）：旅游城市上浮 20%-50%
  · 淡季（如北方城市冬季）：可下浮 10%-30%
  · 普通工作日：基准价
- 价格必须是正整数（>0），不要填 0。
- 输出顺序必须与输入顺序一致。

【输出格式】只输出一个 JSON 数组，不要任何额外文字、代码块或解释。格式：
[{{"name": "酒店名", "price_per_night": 268}}, ...]

城市：{city}
{date_hint}
酒店列表（来自高德 POI，不含房价）：
{json.dumps(hotels_brief, ensure_ascii=False, indent=2)}
"""
    priced: List[Dict] = []
    try:
        resp = await _LLM(agent="hotel_price", model_type="flash").ainvoke([HumanMessage(content=prompt)])
        content = resp.content.strip()
        # 去掉可能的 ```json ... ``` 包裹
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        # 提取第一个 JSON 数组
        m = re.search(r"\[.*\]", content, re.DOTALL)
        if not m:
            raise ValueError(f"未在返回中找到 JSON 数组：{content[:200]}")
        parsed = json.loads(m.group(0))
        if isinstance(parsed, list):
            priced = parsed
    except Exception as e:
        logger.warning(f"⚠️ [{city}] Flash 估价失败：{e}，所有酒店按 200/晚 兜底")

    # 第三步：把 price_per_night 直接拼接到 POI 原始 dict 上
    # 返回的 JSON = 高德 POI 完整字段 + 估价字段，作为完整输出
    for i, poi in enumerate(hotels_raw):
        if i < len(priced):
            price = float(priced[i].get("price_per_night", 0) or 0)
        else:
            price = 0.0
        # 兜底：Flash 没返回价格或返回 0
        if price <= 0:
            price = 200.0
            logger.warning(f"⚠️ [{city}] {poi.get('name','?')} 价格缺失，按 200/晚 估算")
        poi["price_per_night"] = price

    # 日志：按 "酒店名(价格/晚)" 格式输出
    date_str = f" @ {travel_date}" if travel_date else ""
    hotels_brief_log = ", ".join(
        f"{h.get('name', '?')}({float(h.get('price_per_night', 0)):.0f}/晚)"
        for h in hotels_raw
    )
    logger.info(f"💰 [{city}] Flash 估价完成（{len(hotels_raw)} 个{date_str}）：{hotels_brief_log}")
    return hotels_raw


def _poi_count_from_raw(raw: str) -> int:
    """统计高德 MCP 返回中的 POI 条数（判断搜索结果是否为空）。"""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, dict):
            for key in ("pois", "return", "data"):
                v = data.get(key)
                if isinstance(v, list):
                    return len(v)
        elif isinstance(data, list):
            return len(data)
    except Exception:
        pass
    return 0


async def _search_hotels(city: str, kw: str, route_plan: Dict) -> str:
    """按景点几何中心周边搜索酒店；无中心或结果为空时退回城市文本搜索。

    route_plan 需含 _plan_city_route 写入的 hotel_center / hotel_center_radius。
    文本搜索无结果时，再用纯档次词（去掉位置词）重试一次，避免脏位置词导致搜空。
    """
    tier_kw = kw.split()[-1] if kw else "酒店"
    center = (route_plan or {}).get("hotel_center") or ""
    if center:
        radius = int((route_plan or {}).get("hotel_center_radius") or 3000)
        try:
            # around 搜索的 keywords 只保留档次词（去掉文本搜索用的位置前缀）
            raw = await _call_mcp_tool(
                "gaode_around_search", keywords=tier_kw, location=center, radius=radius
            )
            if raw and _poi_count_from_raw(raw) > 0:
                logger.info(f"🏨 [{city}] 按几何中心 {center} 周边 {radius}m 搜索酒店（关键词：{tier_kw}）")
                return raw
            logger.warning(f"⚠️ [{city}] 中心 {center} 周边未搜到酒店，退回文本搜索")
        except Exception as e:
            logger.warning(f"⚠️ [{city}] 周边搜索异常，退回文本搜索: {e}")
    # 城市文本搜索：先带位置词，无结果则用纯档次词重试
    raw = await _call_mcp_tool("gaode_hotel_search", keywords=f"{city} {kw}", city=city)
    if not raw or _poi_count_from_raw(raw) == 0:
        logger.warning(f"⚠️ [{city}] 文本搜索无结果（关键词：{kw}），改用纯档次词重试：{tier_kw}")
        retry = await _call_mcp_tool(
            "gaode_hotel_search", keywords=f"{city} {tier_kw}", city=city
        )
        if retry and _poi_count_from_raw(retry) > 0:
            logger.info(f"🏨 [{city}] 纯档次词重试成功（关键词：{city} {tier_kw}）")
            return retry
        logger.warning(f"⚠️ [{city}] 纯档次词重试仍无结果，保留原文本搜索结果")
    return raw


async def _select_hotel_with_llm(city: str, hotels: List[Dict], route_plan: Dict,
                                 planner_context: Dict, user_query: str,
                                 hotel_budget: float, nights: int) -> Dict[str, Any]:
    """让 LLM 根据景点位置 + 每日出发参考点 + 用户偏好/预算，从候选酒店中选一个最合适的。

    - 输入：候选酒店（含地址/经纬度/价格）、路线规划（daily_starting_point + 各日 starting_point + 景点坐标）、
      用户偏好、酒店预算（per_night 上限 = hotel_budget / nights）
    - 输出：选中的酒店 dict（含 selected_reason）
    - 失败兜底：选最便宜的一家
    """
    if not hotels:
        return {}
    # 从路线规划提取每日出发参考点 + 景点位置线索
    days_info = []
    days = (route_plan or {}).get("days") or []
    for d in days:
        stops = []
        for s in (d.get("stops") or []):
            an = s.get("attraction_name", "")
            if an:
                stops.append(an)
        sp = (d.get("starting_point") or "").strip()
        days_info.append(f"第{d.get('day', '?')}天 出发地:{sp} 景点:{', '.join(stops) or '无'}")
    daily_starting_point = ((route_plan or {}).get("daily_starting_point") or "").strip()
    center = ((route_plan or {}).get("hotel_center") or "").strip()

    def _dist_to_center(h: Dict) -> str:
        """返回酒店距几何中心的距离文案（无坐标时为空）。"""
        if not center:
            return ""
        try:
            hlng, hlat = (h.get("location") or "").split(",", 1)
            clng, clat = center.split(",", 1)
            d = _geo_distance_km(float(hlng), float(hlat), float(clng), float(clat))
            return f"（距中心 {d:.1f}km）"
        except Exception:
            return ""

    hotel_lines = "\n".join(
        f"- {h.get('name', '?')} | {h.get('address', '')} | loc={h.get('location', '')} | {h.get('price_per_night', 0)}元/晚{_dist_to_center(h)}"
        for h in hotels
    )
    per_night_budget = (hotel_budget / nights) if nights > 0 else (hotel_budget or 0)
    preferences = planner_context.get("preferences", []) or []
    pref_str = ", ".join(preferences) if preferences else "无特别偏好"

    prompt = f"""你是酒店选择专家。请为「以上城市」的行程从以下候选酒店中选择**最合适的一家**。

选择要求（按优先级）：
1. 位置：优先靠近景点几何中心 / daily_starting_point / 首日出发地 / 多数景点（候选已附距中心距离），减少每天通勤
2. 价格：每晚价格尽量不超过 per_night_budget；若全部超预算，选最接近上限的
3. 偏好：契合用户偏好（如靠近地铁/商圈/安静/亲子等，可从酒店名与地址判断）

只输出一个 JSON 对象，不要任何额外文字：
{{"name": "选中的酒店名称（必须与候选列表完全一致）", "reason": "一句话理由，说明位置与预算契合度"}}

【城市】{city}
【用户需求】{user_query}
【用户偏好】{pref_str}
【酒店预算】共 {nights} 晚，每晚上限约 {per_night_budget:.0f} 元（总 {hotel_budget:.0f} 元）

【候选酒店】
{hotel_lines}

【每日行程位置线索】
{chr(10).join(days_info) if days_info else "（无详细行程）"}
全市统一出发参考点：{daily_starting_point or "（无）"}
景点几何中心（到各景点距离和最小的参考点，越近越省通勤）：{center or "（未计算）"}
"""
    try:
        llm = _LLM(agent="hotel_select", temperature=0.2)
        structured = llm.with_structured_output(_HotelChoice)
        data: _HotelChoice = await structured.ainvoke([HumanMessage(content=prompt)])
        chosen_name = (data.name or "").strip()
        for h in hotels:
            if h.get("name") == chosen_name:
                h = dict(h)
                h["selected_reason"] = data.reason
                return h
        # 名称不完全一致时尝试包含匹配
        for h in hotels:
            if chosen_name and (chosen_name in h.get("name", "") or h.get("name", "") in chosen_name):
                h = dict(h)
                h["selected_reason"] = data.reason
                return h
        logger.warning(f"⚠️ [{city}] LLM 选中的酒店 '{chosen_name}' 不在候选中，回退最便宜")
    except Exception as e:
        logger.warning(f"⚠️ [{city}] LLM 选酒店失败：{e}，回退最便宜")

    # 兜底：最便宜
    cheapest = min(hotels, key=lambda h: float(h.get("price_per_night", 0) or 0))
    cheapest = dict(cheapest)
    cheapest["selected_reason"] = "LLM 选择失败，回退最便宜"
    return cheapest


def _city_nights(route_plan: Dict, travel_days: int) -> int:
    days = route_plan.get("days") or []
    return max(1, len(days)) if days else max(1, travel_days or 1)


# ──────────────────────────────────────────────────────────
# 预算分配（LLM 智能）
# ──────────────────────────────────────────────────────────

class _CityBudget(BaseModel):
    city: str
    attractions_budget: float = Field(description="景点+市内交通预算（元）")
    hotel_budget: float = Field(description="酒店住宿总预算（元，按夜数合计）")
    nights: int = Field(default=1, description="该市住宿夜数")
    reasoning: str = Field(default="", description="分配理由，供日志查看")


class _CityBudgetPlan(BaseModel):
    city_budgets: List[_CityBudget]
    buffer_budget: float = Field(default=0.0, description="预留弹性预算（元），不分配到具体城市")
    total_allocated: float = Field(default=0.0)


@node("city_budget_allocation")
async def city_budget_allocation_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LLM 智能分配剩余预算到各城市（按城市×天数×偏好×淡旺季）

    输入：total_budget / transport_costs / planner_context / cities / user_query
    输出：city_budgets_map: {city: {attractions_budget, hotel_budget, nights, total, reasoning}}
          + city_budgets_list（有序）+ buffer_budget + 为每个城市预填 per_city_budget（供并发城内规划读取）
          + memory_flags（记忆系统 keep/replan 判定）

    记忆系统预算公式（混合场景）：
        可用池 = total_budget - transport_total - Σ(locked=true 城市的已花费) - buffer_budget
        仅对 replan 城市 + keep 但未完成(partial/pending)城市重新分配；
        keep 且已规划完(done)的城市直接沿用工作记忆中的计划与花费（锁定）。
    """
    pc = state.get("planner_context") or {}
    cities = state.get("cities", []) or []
    total_budget = float(state.get("total_budget", 0) or 0)
    transport_total = round(sum(float(v or 0) for v in (state.get("transport_costs") or {}).values()), 2)
    travel_days = int(pc.get("travel_days", 0) or 0) or max(1, len(cities))
    travel_date = pc.get("travel_date", "") or ""
    preferences = pc.get("preferences", []) or []
    user_query = state.get("user_query", "") or ""
    session_id = state.get("session_id") or ""
    user_id = state.get("user_id") or "default_user"

    # 单城市 / 没交通花费时，直接把总预算全部分配下去（留 5% buffer 更稳健）
    if not cities:
        return {"city_budgets_map": {}, "city_budgets_list": [], "buffer_budget": 0.0}

    # ── 记忆系统：加载工作记忆快照 + keep/replan 判定 ──
    memory = get_memory_manager()
    snapshot = await memory.load_snapshot(session_id, user_id) if session_id else None
    flags = {}
    if snapshot is not None:
        flags = await memory.decide_city_flags(snapshot, cities, user_query, user_id)

    locked_spent = round(snapshot.locked_spent, 2) if snapshot is not None else 0.0
    # 待分配城市：replan 城市 + keep 但未规划完/没规划的城市；keep 且已规划完的直接沿用旧计划
    alloc_cities: List[str] = []
    keep_done_cities: List[str] = []
    for c in cities:
        if snapshot is not None and flags.get(c) == FLAG_KEEP:
            st = snapshot.cities.get(c)
            if st is not None and st.status == CITY_DONE and st.plan:
                keep_done_cities.append(c)
            else:
                alloc_cities.append(c)
        else:
            alloc_cities.append(c)

    if keep_done_cities:
        logger.info(f"🧠 [预算分配] keep 且已规划完，直接沿用旧计划：{keep_done_cities}（锁定 {locked_spent:.0f} 元）")

    # 可用池 = 总预算 - 交通 - 已锁定花费
    pool = round(max(0.0, total_budget - transport_total - locked_spent), 2) if total_budget > 0 else 0.0

    # ======== 规则兜底：按 travel_days / alloc_cities 均分，每城默认 1 晚+，最少 1 晚 ========
    # 如果 LLM 失败，用规则兜底的结果作为预算，保证链路不中断
    days_per_city = max(1, travel_days // len(cities)) if cities else 1
    rule_result: List[Dict[str, Any]] = []
    if alloc_cities:
        for c in alloc_cities:
            share = round(pool / len(alloc_cities), 2) if pool > 0 else 0.0
            # 按经验：酒店约占 50%，景点+市内约占 50%
            att_share = round(share * 0.5, 2)
            ht_share = round(share * 0.5, 2)
            rule_result.append({
                "city": c,
                "attractions_budget": att_share,
                "hotel_budget": ht_share,
                "nights": days_per_city,
                "reasoning": "规则兜底（均分+50/50）",
                "total": share,
            })
    buffer_rule = round(pool - sum(x["total"] for x in rule_result), 2) if pool > 0 else 0.0

    # 无剩余预算或无总预算时，直接返回规则结果，避免调 LLM
    if pool <= 0 or total_budget <= 0 or not alloc_cities:
        logger.warning(f"💰 [预算分配] 无可用剩余预算（pool={pool}，total={total_budget}）")
        cmap: Dict[str, Dict[str, Any]] = {}
        for r in rule_result:
            r["attractions_budget"] = 0.0
            r["hotel_budget"] = 0.0
            r["total"] = 0.0
            cmap[r["city"]] = r
        # keep 且已规划完的城市：预算沿用工作记忆中的份额
        for c in keep_done_cities:
            st = snapshot.cities[c]
            prev_budget = st.budget or {}
            cmap[c] = {
                "city": c,
                "attractions_budget": float(prev_budget.get("attractions_budget", 0) or 0),
                "hotel_budget": float(prev_budget.get("hotel_budget", 0) or 0),
                "nights": int(prev_budget.get("nights", 1) or 1),
                "reasoning": "keep 沿用旧计划预算",
                "total": round(st.spent, 2),
            }
        return {
            "city_budgets_map": cmap,
            "city_budgets_list": rule_result + [cmap[c] for c in keep_done_cities],
            "buffer_budget": 0.0,
            "memory_flags": flags,
        }

    # ======== LLM 智能分配（仅对 alloc_cities） ========
    # 各城市天数提示：总天数按城市均分，可让 LLM 调整 nights
    cities_line = "\n".join(
        f"- {c}（建议游玩天数约 {days_per_city} 天）" for c in alloc_cities
    )
    transport_lines = "\n".join(f"  - {k}: {v:.0f}元" for k, v in (state.get("transport_costs") or {}).items() if v)
    lock_line = ""
    if keep_done_cities:
        lock_line = (f"\n注意：以下城市已规划完毕并锁定预算，不在本次分配范围内：{', '.join(keep_done_cities)}"
                     f"（已锁定 {locked_spent:.0f} 元，不计入可分配池）")

    llm = _LLM(agent="budget_alloc", temperature=0.3)
    prompt = f"""请将旅游预算智能分配到以下每个城市。

分配要求：
1. 每个城市拆分为：attractions_budget（景点门票+市内交通）、hotel_budget（住宿按夜数合计）
2. nights：估计在该市住宿夜数（通常 = 游玩天数，不超过 travel_days）
3. 需要考虑：
   - 用户偏好（如"高消费/豪华"→酒店多分配）
   - 城市档次（如三亚/上海/北京 住宿更贵）
   - 淡旺季（出行日期如果是节假日/暑期，酒店要上浮）
   - 所有城市的 attractions_budget + hotel_budget 之和，要 ≤ 可分配剩余预算 pool
4. 建议留 5%~10% 作为 buffer_budget（弹性缓冲，不分配到具体城市）

输出 JSON，不要任何解释。格式：
{{"city_budgets": [
  {{"city": "城市名", "attractions_budget": 数字, "hotel_budget": 数字, "nights": 整数, "reasoning": "一句话理由"}},
  ...
], "buffer_budget": 数字, "total_allocated": 数字}}

注意：city_budgets 必须按城市列表的顺序，覆盖全部 {len(alloc_cities)} 座城市。

用户查询：{user_query}
用户偏好：{preferences}
出行日期：{travel_date or '未指定'}
总预算：{total_budget:.0f} 元
交通总花费：{transport_total:.0f} 元
各段交通明细：
{transport_lines or '  - （无）'}
可分配剩余预算（景点+酒店，已扣除锁定花费）：{pool:.0f} 元
城市列表（共 {len(alloc_cities)} 座，仅需分配这些）：
{cities_line}
{lock_line}
"""
    try:
        structured = llm.with_structured_output(_CityBudgetPlan)
        data: _CityBudgetPlan = await structured.ainvoke([HumanMessage(content=prompt)])
        plan_list = [x.model_dump() for x in data.city_budgets]

        # 填充缺失城市（防止 LLM 漏输出）
        given = {x.get("city") for x in plan_list}
        for rule in rule_result:
            if rule["city"] not in given:
                plan_list.append({
                    "city": rule["city"],
                    "attractions_budget": rule["attractions_budget"],
                    "hotel_budget": rule["hotel_budget"],
                    "nights": rule["nights"],
                    "reasoning": f"LLM 漏填，回填规则兜底 {rule['reasoning']}",
                })
        # 按 alloc_cities 顺序
        city_order = {c: i for i, c in enumerate(alloc_cities)}
        plan_list.sort(key=lambda x: city_order.get(x.get("city", ""), 9999))
        # 计算 total
        for item in plan_list:
            item["total"] = round(float(item.get("attractions_budget", 0) or 0) + float(item.get("hotel_budget", 0) or 0), 2)
        allocated_sum = sum(x["total"] for x in plan_list)
        buffer_val = round(float(getattr(data, "buffer_budget", 0.0) or 0.0), 2)
        # 如果总和超过 pool，按比例压缩（稳健处理）
        if allocated_sum + buffer_val > pool * 1.001 and allocated_sum > 0:
            scale = pool / (allocated_sum + buffer_val)
            for item in plan_list:
                item["attractions_budget"] = round(float(item.get("attractions_budget", 0)) * scale, 2)
                item["hotel_budget"] = round(float(item.get("hotel_budget", 0)) * scale, 2)
                item["total"] = round(float(item.get("attractions_budget", 0)) + float(item.get("hotel_budget", 0)), 2)
            buffer_val = round(buffer_val * scale, 2)
            logger.warning(f"💰 [预算分配] LLM 总和超 pool={pool:.0f}，按 {scale:.2%} 压缩")
    except Exception as e:
        logger.warning(f"💰 [预算分配] LLM 失败：{e}，使用规则兜底")
        plan_list = rule_result
        allocated_sum = sum(x["total"] for x in plan_list)
        buffer_val = buffer_rule

    # keep 且已规划完的城市：预算沿用工作记忆中的份额，附到 map 但不再分配
    for c in keep_done_cities:
        st = snapshot.cities[c]
        prev_budget = st.budget or {}
        plan_list.append({
            "city": c,
            "attractions_budget": float(prev_budget.get("attractions_budget", 0) or 0),
            "hotel_budget": float(prev_budget.get("hotel_budget", 0) or 0),
            "nights": int(prev_budget.get("nights", 1) or 1),
            "reasoning": "keep 沿用旧计划预算",
            "total": round(st.spent, 2),
        })

    cmap: Dict[str, Dict[str, Any]] = {x["city"]: x for x in plan_list}
    logger.info(
        f"💰 [预算分配] 可用池 {pool:.0f} / 分配 {allocated_sum:.0f} / 弹性缓冲 {buffer_val:.0f}"
        f" / 锁定 {locked_spent:.0f}\n"
        + "\n".join(
            f"  - {x['city']}: 景点{x.get('attractions_budget',0):.0f} + 酒店{x.get('hotel_budget',0):.0f} "
            f"(共{x.get('total',0):.0f}) 住{x.get('nights',1)}晚 · {x.get('reasoning','')}"
            for x in plan_list
        )
    )

    # ── 记忆系统：把本次预算分配写回工作记忆快照（各城 budget / total / transport） ──
    if snapshot is None:
        snapshot = memory.build_snapshot(
            session_id=session_id, user_id=user_id,
            total_budget=total_budget,
            transport_costs=state.get("transport_costs") or {},
            buffer_budget=buffer_val,
        )
    else:
        snapshot.total_budget = round(float(total_budget), 2)
        snapshot.transport_costs = dict(state.get("transport_costs") or {})
        snapshot.transport_total = transport_total
        snapshot.buffer_budget = round(float(buffer_val), 2)
    for item in plan_list:
        memory.update_city_state(
            snapshot, item["city"],
            budget={
                "attractions_budget": float(item.get("attractions_budget", 0) or 0),
                "hotel_budget": float(item.get("hotel_budget", 0) or 0),
                "nights": int(item.get("nights", 1) or 1),
                "total_allocated": float(item.get("total", 0) or 0),
            },
        )
    await memory.save_snapshot(snapshot)

    return {
        "city_budgets_map": cmap,
        "city_budgets_list": plan_list,
        "buffer_budget": buffer_val,
        "memory_flags": flags,
    }


# ──────────────────────────────────────────────────────────
# 全城市并发城内规划
# ──────────────────────────────────────────────────────────

async def _run_one_city_async(
    city: str,
    idx: int,
    state_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """对单个城市独立执行完整城内子图流程（attractions → planner[含replan] → hotel）。

    每个城市有自己的 per_city_budget（来自 city_budgets_map），
    超预算校验只对该城市分配的额度进行，不再依赖全局 spent_budget 串行累加。

    异常兜底：任一步骤失败时返回带 _error 标记的空 city_plan，不影响其他城市。
    """
    try:
        # 节点名不带城市标签：并发多城都写同名 "city_plan"，monitor 侧直接同名计数+累加
        with node_scope("city_plan"):
            return await _run_one_city_inner(city, idx, state_snapshot)
    except Exception as e:
        logger.error(f"❌ [{city}] 城内规划失败: {e}", exc_info=True)
        # 返回带错误标记的空 city_plan，不影响其他城市
        budgets_map = state_snapshot.get("city_budgets_map") or {}
        city_budget = budgets_map.get(city) or {}
        per_city_attractions = float(city_budget.get("attractions_budget", 0) or 0)
        per_city_hotel = float(city_budget.get("hotel_budget", 0) or 0)
        return {
            "city": city,
            "city_plan": {
                "city": city,
                "attractions_candidates": [],
                "route_plan": {"selected_attractions": [], "days": [], "attractions_cost": 0},
                "hotels": [],
                "transport_cost": 0,
                "attractions_cost": 0,
                "hotel_cost": 0,
                "nights": int(city_budget.get("nights", 1) or 1),
                "city_total_cost": 0,
                "per_city_budget": {
                    "attractions_allocated": per_city_attractions,
                    "hotel_allocated": per_city_hotel,
                    "total_allocated": round(per_city_attractions + per_city_hotel, 2),
                    "attractions_used": 0,
                    "hotel_used": 0,
                },
                "_error": str(e),
            },
            "tool_results": [],
        }


async def _run_one_city_inner(
    city: str,
    idx: int,
    state_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """_run_one_city_async 的内部实现（无 try/except，由外层兜底）。"""
    # --- 构造该城市的局部 state ---
    pc = state_snapshot.get("planner_context") or {}
    budgets_map = state_snapshot.get("city_budgets_map") or {}
    city_budget = budgets_map.get(city) or {}
    per_city_attractions = float(city_budget.get("attractions_budget", 0) or 0)
    per_city_hotel = float(city_budget.get("hotel_budget", 0) or 0)
    per_city_total = round(per_city_attractions + per_city_hotel, 2)

    transport_ctx = (state_snapshot.get("city_transport_context") or {}).get(city, {})
    preferences = pc.get("preferences", []) or []
    user_query = state_snapshot.get("user_query", "") or ""

    # --- Step A: attractions_search ---
    rag_raw = await query_travel_knowledge(f"{city} 景点 攻略")
    poi_raw = await _call_mcp_tool("gaode_poi_search", keywords=f"{city} 景点", city=city)
    attractions = await _extract_attractions(
        rag_raw, poi_raw, city, preferences, user_query,
        few_shot=pc.get("memory_fewshot", "") or "",
    )
    att_brief = ", ".join(
        f"{a.get('name','?')}({a.get('ticket_price',0):.0f}元)" for a in attractions
    )
    logger.info(f"📍 [{city}] 候选景点 {len(attractions)} 个：{att_brief[:200]}")

    # --- Step B: 路线规划（独立 replan） ---
    # 用分配的 per_city_attractions 作为该城景点预算上限
    spent_before_city = 0.0  # 城内不再串行累加
    total_replan = PLAN_MAX_REPLAN  # .env 可配，默认 3
    best_plan: Dict[str, Any] = {}
    for replan_i in range(total_replan):
        is_replan = replan_i > 0
        plan = await _plan_city_route(
            city, attractions, pc, user_query,
            spent_before_city=0,  # 子函数内部只用到 spent_before_city+city_cost > total 的判断；下面我们自己用 per_city 校验
            total_budget=per_city_attractions,  # 用 per_city 景点额度当 total，让内部提示生效
            is_replan=is_replan,
            replan_count=replan_i,
            transport_ctx=transport_ctx,
        )
        city_att_cost = float(plan.get("attractions_cost", 0) or 0)
        over = (per_city_attractions > 0 and city_att_cost > per_city_attractions)
        if not over:
            best_plan = plan
            break
        best_plan = plan  # 失败时记录最后一版
        logger.info(f"🔄 [{city}] 第 {replan_i + 1}/{total_replan} 次重规划（景点 {city_att_cost:.0f} > 分配 {per_city_attractions:.0f}）")
    # 确保字段齐全
    best_plan.setdefault("city", city)

    # --- Step C: 酒店检索（用 per_city_hotel） ---
    remaining_for_hotel = per_city_hotel
    kw = _decide_hotel_keywords(pc, user_query, remaining_for_hotel,
                                route_plan=best_plan, transport_ctx=transport_ctx)
    # 优先按景点几何中心周边搜索酒店（best_plan 含 hotel_center），无中心退回文本搜索
    hotel_raw = await _search_hotels(city, kw, best_plan)
    hotels = await _extract_hotels(hotel_raw, city, pc, user_query, max_n=5,
                                   search_keywords=kw)
    route_plan = best_plan
    # 以 route_plan 的 days 长度为准，city_budget 的 nights 仅作兜底
    # （LLM 预算分配时的 nights 可能不准，比如 3 天行程给了 1 晚）
    nights_from_plan = _city_nights(route_plan, pc.get("travel_days", 1) or 1)
    nights_from_budget = int(city_budget.get("nights", 1) or 1)
    if not route_plan or not route_plan.get("days"):
        nights_in_city = nights_from_budget
    else:
        nights_in_city = nights_from_plan
    # 让 LLM 根据景点位置/出发参考点/偏好/预算选酒店；失败回退最便宜
    chosen = await _select_hotel_with_llm(
        city, hotels, route_plan, pc, user_query,
        hotel_budget=per_city_hotel, nights=nights_in_city,
    )
    cheapest = float(chosen.get("price_per_night", 0) or 0)
    hotel_cost = round(cheapest * nights_in_city, 2)
    chosen_name = chosen.get("name", "?") if chosen else "(无)"
    reason = chosen.get("selected_reason", "")
    logger.info(
        f"  ✅ [{city}] 选定酒店：{chosen_name} {cheapest:.0f}/晚 × {nights_in_city}晚 = {hotel_cost:.0f}元"
        f"（分配酒店预算 {per_city_hotel:.0f}）（{reason}）"
    )

    # --- 组装 city_plan ---
    origin = pc.get("origin", "")
    cities = state_snapshot.get("cities", []) or []
    prev = origin if idx == 0 else (cities[idx - 1] if idx - 1 < len(cities) else origin)
    transport_cost = float((state_snapshot.get("transport_costs") or {}).get(f"{prev}->{city}", 0.0) or 0)
    city_att_cost_total = float(best_plan.get("attractions_cost", 0) or 0)
    city_plan = {
        "city": city,
        "attractions_candidates": attractions,
        "route_plan": best_plan,
        "hotels": hotels,
        "selected_hotel": chosen,
        "transport_cost": transport_cost,
        "attractions_cost": city_att_cost_total,
        "hotel_cost": hotel_cost,
        "nights": nights_in_city,
        "city_total_cost": round(transport_cost + city_att_cost_total + hotel_cost, 2),
        "per_city_budget": {
            "attractions_allocated": per_city_attractions,
            "hotel_allocated": per_city_hotel,
            "total_allocated": per_city_total,
            "attractions_used": city_att_cost_total,
            "hotel_used": hotel_cost,
        },
    }

    return {
        "city": city,
        "city_plan": city_plan,
        "tool_results": [
            {"tool": "rag_search", "result": rag_raw, "city": city},
            {"tool": "gaode_poi_search", "result": poi_raw, "city": city},
            {"tool": "gaode_hotel_search", "result": hotel_raw, "city": city},
        ],
    }


@node("plan_all_cities_concurrent")
async def plan_all_cities_concurrent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """全城市并发执行完整城内子图（asyncio.gather），然后汇总预算。

    - 记忆系统：keep 且已规划完的城市直接从工作记忆复用旧计划（跳过全部工具调用）；
      replan / keep 未完成的城市正常执行 attractions → planner(replan) → hotel
    - 各城市使用 city_budgets_map 分配到的 per_city 预算做独立校验
    - 全部完成后汇总 city_plans / spent_budget / tool_results / rag_results_history
    - 总超预算（超过 total_budget）时统一标记 over_budget + budget_message
    - 规划结束后把各城结果（status/spent/plan）写回工作记忆快照
    """
    cities = state.get("cities", []) or []
    total_budget = float(state.get("total_budget", 0) or 0)
    transport_total = round(sum(float(v or 0) for v in (state.get("transport_costs") or {}).values()), 2)
    buffer_budget = float(state.get("buffer_budget", 0) or 0)
    session_id = state.get("session_id") or ""
    user_id = state.get("user_id") or "default_user"
    flags = state.get("memory_flags") or {}

    if not cities:
        return {"city_plans": [], "spent_budget": transport_total, "over_budget": False}

    # 加载工作记忆快照，确定 keep 且已规划完的城市（直接复用旧计划）
    memory = get_memory_manager()
    snapshot = await memory.load_snapshot(session_id, user_id) if session_id else None
    reuse_plans: Dict[str, Dict[str, Any]] = {}
    if snapshot is not None:
        for c in cities:
            st = snapshot.cities.get(c)
            if (flags.get(c) == FLAG_KEEP and st is not None
                    and st.status == CITY_DONE and st.plan):
                reuse_plans[c] = st.plan

    # 只传递需要的字段快照，避免各城市并发任务共享同一个 dict（读多写少没问题，但快照更安全）
    snapshot_ctx: Dict[str, Any] = {
        "planner_context": state.get("planner_context") or {},
        "cities": list(cities),
        "user_query": state.get("user_query", "") or "",
        "city_budgets_map": state.get("city_budgets_map") or {},
        "transport_costs": state.get("transport_costs") or {},
        "city_transport_context": state.get("city_transport_context") or {},
    }

    replan_cities = [c for c in cities if c not in reuse_plans]
    # 并发上限：CITY_PLAN_MAX_CONCURRENCY（.env 可配，0=不限）；信号量限流，避免城市过多时工具/LLM 突发打满
    limit = CITY_PLAN_MAX_CONCURRENCY
    sem = asyncio.Semaphore(limit) if limit > 0 else None
    logger.info(
        f"🚀 [并发城内规划] 启动 {len(replan_cities)} 个城市并发"
        + (f"（上限 {limit}）" if sem else "（不限并发）")
        + f"：{replan_cities}"
        + (f"；keep 复用旧计划 {len(reuse_plans)} 个：{list(reuse_plans)}" if reuse_plans else "")
    )

    async def _run_limited(idx: int, city: str) -> Dict[str, Any]:
        if sem is not None:
            async with sem:
                return await _run_one_city_async(city, idx, snapshot_ctx)
        return await _run_one_city_async(city, idx, snapshot_ctx)

    new_results: List[Dict[str, Any]] = list(await asyncio.gather(*(
        _run_limited(idx, city)
        for idx, city in enumerate(cities) if city in replan_cities
    )))
    logger.info(f"✅ [并发城内规划] {len(new_results)} 个城市完成（另有 {len(reuse_plans)} 个复用旧计划）")

    # 汇总：reuse 城市构造与 _run_one_city_async 相同的返回结构
    results: List[Dict[str, Any]] = []
    for c in cities:
        if c in reuse_plans:
            results.append({"city": c, "city_plan": reuse_plans[c], "tool_results": []})
        else:
            for r in new_results:
                if r.get("city") == c:
                    results.append(r)
                    break

    city_plans: List[Dict[str, Any]] = []
    tool_results: List[Dict[str, Any]] = []
    rag_results_history: List[str] = []
    cities_total = 0.0
    for r in results:
        cp = r.get("city_plan") or {}
        city_plans.append(cp)
        cities_total += float(cp.get("attractions_cost", 0) or 0) + float(cp.get("hotel_cost", 0) or 0)
        for tr in (r.get("tool_results") or []):
            tool_results.append(tr)
            if tr.get("tool") == "rag_search" and tr.get("result"):
                rag_results_history.append(tr["result"])

    # 重排序：按 cities 顺序（asyncio.gather 本身就保序，但保险起见）
    order = {c: i for i, c in enumerate(cities)}
    city_plans.sort(key=lambda cp: order.get(cp.get("city", ""), 9999))

    spent = round(transport_total + cities_total, 2)
    # 允许在 total_budget + buffer_budget 范围内（buffer 用于弹性）
    allow_total = round(total_budget + buffer_budget, 2) if total_budget > 0 else 0
    over = (total_budget > 0 and spent > allow_total)
    budget_message = None
    if over:
        budget_message = (
            f"全部城市完成，但各城市方案已自动重规划最多 3 次，累计花费 {spent:.0f} 元"
            f"（含弹性缓冲 {buffer_budget:.0f} 元）仍超出总预算 {total_budget:.0f} 元。"
            f"建议调整需求（减少天数/城市数/更换目的地），或指定缩减景点门票、酒店住宿、交通中的某部分预算。"
        )
    logger.info(
        f"📊 [并发汇总] 交通{transport_total:.0f} + 城市内{cities_total:.0f} = 累计{spent:.0f} / "
        f"总预算{total_budget:.0f} + 缓冲{buffer_budget:.0f}，超预算={over}"
    )

    # ── 记忆系统：把各城规划结果写回工作记忆快照 ──
    if snapshot is None:
        snapshot = memory.build_snapshot(
            session_id=session_id, user_id=user_id,
            total_budget=total_budget,
            transport_costs=state.get("transport_costs") or {},
            buffer_budget=buffer_budget,
        )
    for cp in city_plans:
        city = cp.get("city", "")
        city_spent = round(float(cp.get("attractions_cost", 0) or 0) + float(cp.get("hotel_cost", 0) or 0), 2)
        memory.update_city_state(
            snapshot, city,
            status=CITY_DONE,
            spent=city_spent,
            locked=True,
            plan=cp,
            budget=cp.get("per_city_budget") or None,
        )
    await memory.save_snapshot(snapshot)

    return {
        "city_plans": city_plans,
        "spent_budget": spent,
        "over_budget": over,
        "budget_message": budget_message,
        "tool_results": tool_results,
        "rag_results_history": rag_results_history,
        "has_next_city": False,  # 全部城市已处理完毕
        "city_plans_kept": bool(reuse_plans),
    }
