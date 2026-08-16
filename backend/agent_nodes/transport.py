"""交通规划节点：transport_check_node / transport_select_node /
budget_fail_node 及交通方式选择/费用提取辅助。

从原 workflow_nodes.py 拆分而来。
"""
from typing import Dict, Any, List, Tuple
import json
import re
import logging

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from config.settings import DRIVING_MAX_DISTANCE_KM
from ._common import _LLM, _call_mcp_tool, _stream_reply
from ._observability import node

logger = logging.getLogger(__name__)


def _shift_time(time_str: str, minutes: int) -> str:
    """将 HH:MM 时间向前(负)/后(正)移动 minutes 分钟，跨日自动回绕。"""
    try:
        h, m = map(int, time_str.split(":"))
        total = h * 60 + m + minutes
        total %= 24 * 60
        return f"{total // 60:02d}:{total % 60:02d}"
    except Exception:
        return time_str


def _parse_text_transport_brief(raw: str, max_items: int = 3) -> str:
    """从 12306 text 格式返回中提取车次简报。

    text 格式示例：
        G2172 广州(telecode:GZQ) -> 西安东(telecode:XDY) 06:30 -> 15:51 历时：09:21
        - 商务座: 剩余19张票 2984元
        - 一等座: 有票 1423元
        - 二等座: 有票 891元
        - 无座: 无票 891元
    """
    text = str(raw)
    # 匹配车次行：车次号 站点(telecode:XXX) -> 站点(telecode:XXX) 时间 -> 时间 历时：XX:XX
    train_pattern = re.compile(
        r'^(\w+)\s+(\S+?)\(telecode:\w+\)\s*->\s*(\S+?)\(telecode:\w+\)\s*'
        r'(\d{2}:\d{2})\s*->\s*(\d{2}:\d{2})\s*历时：(\S+)',
        re.MULTILINE
    )
    matches = list(train_pattern.finditer(text))
    if not matches:
        return ""

    briefs = []
    for i, m in enumerate(matches[:max_items]):
        code, from_st, to_st, dep_t, arr_t, _duration = m.groups()
        # 在该车次和下一个车次之间的文本中找最低价格
        pos = m.end()
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        segment = text[pos:end_pos]
        prices = re.findall(r'(\d+(?:\.\d+)?)元', segment)
        min_price = min(float(p) for p in prices) if prices else 0
        price_str = f" {int(min_price)}元" if min_price > 0 else ""
        briefs.append(f"{code} {dep_t}-{arr_t} {from_st}→{to_st}{price_str}")

    suffix = f" ...(共{len(matches)}个)" if len(matches) > max_items else ""
    return "; ".join(briefs) + suffix


def _parse_transport_brief(raw: str, mode: str, max_items: int = 3) -> str:
    """从 12306/flight 返回中提取前 N 个车次/航班的简报（车次号/航班号、起止时间、价格）。

    用于日志打印。支持 JSON 和 text 两种格式，无法解析时返回原始内容前 500 字。
    """
    if not raw:
        return "(空)"
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        # JSON 解析失败，尝试解析 12306 text 格式
        brief = _parse_text_transport_brief(raw, max_items)
        if brief:
            return brief
        return str(raw)[:500]

    # 常见字段名兜底：尝试在 data 中找 list 类型的 candidates
    items: List[Dict] = []
    if isinstance(data, list):
        items = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        # 12306 返回可能是 {"return": [...]} 或 {"data": [...]} 或 {"result": [...]}
        for key in ("return", "data", "result", "tickets", "trains", "flights", "list"):
            v = data.get(key)
            if isinstance(v, list):
                items = [x for x in v if isinstance(x, dict)]
                break
        # 也可能是 {"return": {"tickets": [...]}} 嵌套
        if not items:
            for v in data.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    items = [x for x in v if isinstance(x, dict)]
                    break

    if not items:
        return str(raw)[:500]

    briefs: List[str] = []
    for it in items[:max_items]:
        # 车次号 / 航班号（优先 start_train_code，train_no 是内部编号如 240000G53107）
        code = (it.get("start_train_code") or it.get("trainCode") or it.get("train_no")
                or it.get("trainNo") or it.get("code")
                or it.get("flight_no") or it.get("flightNo") or it.get("fnum")
                or it.get("number") or "?")
        # 出发-到达时间
        dep_t = (it.get("start_time") or it.get("departureTime") or it.get("dep_time")
                 or it.get("startTime") or it.get("departTime") or "")
        arr_t = (it.get("arrive_time") or it.get("arrivalTime") or it.get("arr_time")
                 or it.get("arriveTime") or "")
        # 价格：优先 min_price/price/lowest_price；若 prices 是数组则取最低价
        price_raw = it.get("min_price") or it.get("price") or it.get("lowest_price")
        if not price_raw and isinstance(it.get("prices"), list):
            seat_prices = [p.get("price", 0) for p in it["prices"]
                           if isinstance(p, dict) and p.get("price")]
            price_raw = min(seat_prices) if seat_prices else 0
        price = price_raw or ""
        # 站点
        from_station = (it.get("from_station") or it.get("startStation") or it.get("dep") or "")
        to_station = (it.get("to_station") or it.get("endStation") or it.get("arr") or "")
        time_part = f"{dep_t}-{arr_t}" if (dep_t or arr_t) else ""
        station_part = f"{from_station}→{to_station}" if (from_station or to_station) else ""
        parts = [f"{code}"]
        if time_part:
            parts.append(time_part)
        if station_part:
            parts.append(station_part)
        if price:
            parts.append(f"{price}元")
        briefs.append(" ".join(parts))
    suffix = f" ...(共{len(items)}个)" if len(items) > max_items else ""
    return "; ".join(briefs) + suffix


# ──────────────────────────────────────────────────────────
# 交通相关：方式选择 / 费用提取
# ──────────────────────────────────────────────────────────

def _quick_extract_train_price(raw: str) -> float:
    """快速正则提取火车最低票价（每人，不调用 LLM），失败返回 0

    座位正则用 .*? + "元" 后缀锚定，避免误提取"剩余9张票"里的张数。
    """
    text = str(raw)
    price_patterns = [
        r"min_price[^\d]*(\d+(?:\.\d+)?)",
        r"price[^\d]*(\d+(?:\.\d+)?)",
        r"票价[^\d]*(\d+(?:\.\d+)?)",
        r"二等座.*?(\d+(?:\.\d+)?)元",
        r"一等座.*?(\d+(?:\.\d+)?)元",
        r"[¥￥]\s*(\d+(?:\.\d+)?)",
    ]
    for pat in price_patterns:
        for m in re.finditer(pat, text):
            try:
                return float(m.group(1))
            except (ValueError, IndexError):
                pass
    return 0.0


async def _decide_transport_mode(
    from_city: str, to_city: str, user_query: str = "",
    travel_date: str = "", manager=None
) -> Tuple[str, str, float, str]:
    """LLM 驱动的交通方式选择。

    流程：
    1. 告诉 LLM 有哪些工具可用（train_query / driving_cost_query）
    2. LLM 决定调用哪些工具
    3. 执行工具，获取结果
    4. LLM 根据结果做最终选择
    5. 返回 (mode, mode_used, cost, raw)
    """
    if manager is None:
        from tools.mcp_tools import get_mcp_manager
        manager = await get_mcp_manager()

    llm = _LLM(agent="transport", temperature=0.0)

    # ── Step 1: LLM 决定调用哪些工具 ──
    prompt_tools = f"""可用工具：
- train_query: 查询12306火车余票和票价（参数: origin, destination, date）
- driving_cost_query: 查询自驾距离、油费、高速费、耗时（参数: from, to）

请决定需要调用哪些工具来比较交通方案。
输出要调用的工具名，用逗号分隔。例如：train_query,driving_cost_query
若用户明确要求自驾，可只调用 driving_cost_query。
只输出工具名，不要其他文字。

路线：{from_city} → {to_city}
用户查询：{user_query}
出行日期：{travel_date or '未指定'}"""

    tool_choices = ["train_query", "driving_cost_query"]  # 默认两个都调
    try:
        resp = await llm.ainvoke([HumanMessage(content=prompt_tools)])
        raw_choice = resp.content.strip()
        parsed = [t.strip() for t in raw_choice.replace("，", ",").split(",") if t.strip()]
        if parsed:
            tool_choices = [t for t in parsed if t in ("train_query", "driving_cost_query")]
            if not tool_choices:
                tool_choices = ["train_query", "driving_cost_query"]
        logger.info(f"  🤖 LLM 选择工具: {tool_choices}")
    except Exception as e:
        logger.warning(f"  ⚠️ LLM 工具选择失败: {e}，默认两个都调")

    # ── Step 2: 执行工具（走统一分发） ──
    results: Dict[str, str] = {}
    for tname in tool_choices:
        if tname == "train_query":
            results["train"] = await _call_mcp_tool("train_query", origin=from_city, destination=to_city, date=travel_date)
        elif tname == "driving_cost_query":
            results["driving"] = await _call_mcp_tool("driving_cost_query", **{"from": from_city, "to": to_city})

    # ── Step 3: 只有一个结果时直接返回 ──
    if "train" in results and "driving" not in results:
        cost = await _extract_transport_cost(results["train"], user_query, from_city, to_city, "train")
        logger.info(f"  ✅ 仅火车可用，费用 {cost:.0f}元")
        return "train", "train", cost, results["train"]

    if "driving" in results and "train" not in results:
        d = json.loads(results["driving"])
        cost = d.get("total_cost", 0.0)
        logger.info(f"  ✅ 仅自驾可用，费用 {cost:.0f}元")
        return "driving", "driving", cost, results["driving"]

    if "driving" not in results and "train" not in results:
        logger.warning(f"  ⚠️ 无工具结果，兜底 train")
        return "train", "train", 0.0, '{"error": "无查询结果"}'

    # ── Step 4: 两个结果都有 → LLM 做最终选择 ──
    train_raw = results["train"]
    driving_raw = results["driving"]
    train_has_error = "error" in str(train_raw).lower()
    driving_data = json.loads(driving_raw)
    driving_cost = driving_data.get("total_cost", 0)
    distance_km = driving_data.get("distance_km", 0)
    duration_hours = driving_data.get("duration_hours", 0)

    if train_has_error and driving_cost > 0:
        logger.info(f"  ✅ 火车查询失败，选用自驾 {driving_cost:.0f}元")
        return "driving", "driving", driving_cost, driving_raw

    if train_has_error and driving_cost <= 0:
        logger.warning(f"  ⚠️ 火车和自驾均失败")
        return "train", "train", 0.0, train_raw

    # 两个都正常，交给 LLM 选择
    train_brief = _parse_transport_brief(train_raw, "train")
    train_price = _quick_extract_train_price(train_raw)
    persons = 1
    m_person = re.search(r"(\d+)\s*(?:人|位|个人)", user_query)
    if m_person:
        persons = int(m_person.group(1))
    train_total = round(train_price * persons, 2) if train_price > 0 else 0

    prompt_choice = f"""请从以下两个交通方案中选择更优的一个。

选择规则：
- 综合考虑价格、时间、便利性
- 若自驾距离超过 {DRIVING_MAX_DISTANCE_KM}km，不推荐自驾
- 若用户明确要求自驾/开车，选 driving
- 只输出一个词：train 或 driving

路线：{from_city} → {to_city}
用户查询：{user_query}
出行人数：{persons}人

【方案1：火车】
{train_brief}
最低票价：{train_price}元/人，{persons}人合计约 {train_total}元

【方案2：自驾】
驾车距离：{distance_km}km
预计耗时：{duration_hours}小时
油费：{driving_data.get('fuel_cost', 0)}元
高速费：{driving_data.get('toll_cost', 0)}元
自驾总费用：{driving_cost}元（不随人数变化）"""

    try:
        resp2 = await llm.ainvoke([HumanMessage(content=prompt_choice)])
        choice = resp2.content.strip().lower()
        if "driving" in choice and "train" not in choice:
            chosen = "driving"
        elif "train" in choice and "driving" not in choice:
            chosen = "train"
        else:
            chosen = "driving" if driving_cost < train_total and distance_km <= DRIVING_MAX_DISTANCE_KM else "train"
        logger.info(f"  🤖 LLM 选择: {chosen} (火车{train_total}元 vs 自驾{driving_cost}元)")
    except Exception as e:
        logger.warning(f"  ⚠️ LLM 选择失败: {e}，按价格比较")
        chosen = "driving" if driving_cost < train_total and distance_km <= DRIVING_MAX_DISTANCE_KM else "train"

    # 用户强制自驾时覆盖
    if re.search(r"自驾|开车|自己.?开车|驾车|自己.?驾车|走高速|开自己?车", user_query) and distance_km > 0:
        chosen = "driving"
        logger.info(f"  🚗 用户指定自驾，覆盖为 driving")

    if chosen == "driving":
        return "driving", "driving", driving_cost, driving_raw
    else:
        cost = await _extract_transport_cost(train_raw, user_query, from_city, to_city, "train")
        return "train", "train", cost, train_raw


async def _extract_transport_schedule(
    raw: str, mode_used: str, from_city: str, to_city: str, travel_date: str = ""
) -> Dict[str, str]:
    """从交通查询结果提取选定车次/路线的时刻表信息。

    返回: {"departure_time": "06:30", "departure_station": "广州",
           "arrival_time": "15:51", "arrival_station": "西安东"}
    - train: 从 12306 text 格式提取第一个车次的时刻表
    - driving: 由 LLM 估算出发/到达时间和位置
    """
    if mode_used == "train":
        text = str(raw)
        pattern = re.compile(
            r'^(\w+)\s+(\S+?)\(telecode:\w+\)\s*->\s*(\S+?)\(telecode:\w+\)\s*'
            r'(\d{2}:\d{2})\s*->\s*(\d{2}:\d{2})',
            re.MULTILINE
        )
        m = pattern.search(text)
        if m:
            _, dep_station, arr_station, dep_time, arr_time = m.groups()
            return {
                "departure_time": dep_time,
                "departure_station": dep_station,
                "arrival_time": arr_time,
                "arrival_station": arr_station,
            }
        return {}

    if mode_used == "driving":
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            distance_km = float(data.get("distance_km", 0) or 0)
            duration_hours = float(data.get("duration_hours", 0) or 0)
        except Exception:
            distance_km = 0
            duration_hours = 0

        llm = _LLM(agent="transport", temperature=0.0)
        prompt = f"""请估算合理的出发时间和到达时间，以及出发/到达的大致位置（如市中心）。
输出 JSON：{{"departure_time": "HH:MM", "arrival_time": "HH:MM", "departure_location": "位置", "arrival_location": "位置"}}
只输出 JSON，不要其他文字。

自驾路线：{from_city} → {to_city}
距离：{distance_km:.0f}km，预计行驶：{duration_hours:.1f}小时
出行日期：{travel_date or '未指定'}"""
        try:
            resp = await llm.ainvoke([HumanMessage(content=prompt)])
            content = resp.content.strip().strip('`')
            if content.startswith('json'):
                content = content[4:].strip()
            result = json.loads(content)
            return {
                "departure_time": result.get("departure_time", ""),
                "departure_station": result.get("departure_location", f"{from_city}市中心"),
                "arrival_time": result.get("arrival_time", ""),
                "arrival_station": result.get("arrival_location", f"{to_city}市中心"),
            }
        except Exception as e:
            logger.warning(f"自驾时刻表估算失败({from_city}->{to_city}): {e}")
            return {}

    return {}


async def _extract_transport_cost(raw: str, user_query: str, from_city: str, to_city: str, mode: str) -> float:
    """从 train/flight 原始结果中提取该段总交通费用（结合用户查询中的人数）"""
    text = str(raw)

    # 正则兜底：搜索票价相关模式
    # 12306 返回格式通常含 "价格"、"¥"、"票价"、"min_price" 等
    price_patterns = [
        r"[¥￥]\s*(\d+(?:\.\d+)?)",
        r"min_price[^\d]*(\d+(?:\.\d+)?)",
        r"price[^\d]*(\d+(?:\.\d+)?)",
        r"票价[^\d]*(\d+(?:\.\d+)?)",
        r"二等座.*?(\d+(?:\.\d+)?)元",
        r"一等座.*?(\d+(?:\.\d+)?)元",
        r"商务座.*?(\d+(?:\.\d+)?)元",
        r"无座.*?(\d+(?:\.\d+)?)元",
        r"硬座.*?(\d+(?:\.\d+)?)元",
        r"软卧.*?(\d+(?:\.\d+)?)元",
    ]

    prices: List[float] = []
    for pat in price_patterns:
        for m in re.finditer(pat, text):
            try:
                prices.append(float(m.group(1)))
            except (ValueError, IndexError):
                pass

    if prices:
        cheapest = min(prices)
        # 提取人数
        persons = 1
        m_person = re.search(r"(\d+)\s*(?:人|位|个人)", user_query)
        if m_person:
            persons = int(m_person.group(1))
        cost = round(cheapest * persons, 2)
        logger.info(f"  🚆 {from_city}->{to_city} [{mode}] 正则解析：最低 {cheapest}×{persons}={cost}")
        return cost

    # 正则兜底失败 → LLM 解析
    llm = _LLM(agent="transport", temperature=0.0)
    prompt = f"""请从以下{mode}查询结果中，提取从 {from_city} 到 {to_city} 的最便宜票价（每人），并按用户查询中提到的人数计算该段总费用。

规则：
- 若无法识别票价，返回 0
- 只输出一个数字（总费用，单位：元），不要任何文字或符号

用户查询：{user_query}
查询结果（节选）：
{text[:2000]}
"""
    try:
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        m = re.search(r"\d+(?:\.\d+)?", resp.content)
        return float(m.group()) if m else 0.0
    except Exception:
        logger.warning(f"  ⚠️ {from_city}->{to_city} 费用解析全部失败，返回 0")
        return 0.0


# ──────────────────────────────────────────────────────────
# 节点 4：交通费用累加校验
# ──────────────────────────────────────────────────────────

@node("transport_check")
async def transport_check_node(state: Dict[str, Any]) -> Dict[str, Any]:
    pc = state.get("planner_context") or {}
    origin = pc.get("origin", "")
    cities = state.get("cities", []) or []
    total_budget = float(state.get("total_budget", 0) or 0)
    user_query = state.get("user_query", "") or ""
    travel_date = pc.get("travel_date", "")

    if not origin or not cities:
        return {
            "over_budget": False,
            "budget_message": None,
            "transport_costs": {},
            "spent_budget": 0.0,
        }

    # 完整路线：出发地 → city1 → ... → cityN → 出发地（含返程）
    route_points = [origin] + cities + [origin]
    from tools.mcp_tools import get_mcp_manager
    manager = await get_mcp_manager()

    spent = 0.0
    transport_costs: Dict[str, float] = {}
    over_budget = False
    budget_message = None
    tool_results: List[Dict] = []
    city_transport_context: Dict[str, Dict] = {}

    for i in range(len(route_points) - 1):
        from_city = route_points[i]
        to_city = route_points[i + 1]
        leg_key = f"{from_city}->{to_city}"

        # 同城段：出发地 = 首个游览城市时，首段无需交通
        if from_city == to_city:
            transport_costs[leg_key] = 0.0
            logger.info(f"🚆 {leg_key} 同城跳过，费用 0")
            continue

        mode, mode_used, cost, raw = await _decide_transport_mode(
            from_city, to_city, user_query=user_query, travel_date=travel_date, manager=manager
        )

        # 打印查询简报
        brief = _parse_transport_brief(raw, mode_used) if mode_used != "driving" else \
            f"🚗 自驾 {json.loads(raw).get('distance_km', 0):.0f}km, 费用{cost:.0f}元"
        logger.info(f"  🔎 {leg_key} [{mode_used}] {brief}")

        transport_costs[leg_key] = cost
        spent += cost
        tool_results.append({"tool": f"{mode_used}_query", "result": raw, "leg": leg_key})

        # 提取时刻表，构建城市交通上下文
        schedule = await _extract_transport_schedule(raw, mode_used, from_city, to_city, travel_date)
        if schedule:
            logger.info(f"  🕐 {leg_key} [{mode_used}] {schedule.get('departure_time','?')}-{schedule.get('arrival_time','?')} "
                        f"{schedule.get('departure_station','?')}→{schedule.get('arrival_station','?')}")
            # to_city 的 start 信息（到站时间=城内规划开始时间，到站位置=城内规划开始位置）
            if to_city in cities:
                city_transport_context.setdefault(to_city, {}).update({
                    "start_time": schedule.get("arrival_time", ""),
                    "start_location": schedule.get("arrival_station", ""),
                })
            # from_city 的 end 信息（下一程出发时间提前60分钟=城内规划结束时间，出发位置=城内规划结束位置）
            if from_city in cities:
                dep_time = schedule.get("departure_time", "")
                city_transport_context.setdefault(from_city, {}).update({
                    "end_time": _shift_time(dep_time, -60) if dep_time else "",
                    "end_location": schedule.get("departure_station", ""),
                })

        logger.info(f"  🚆 {leg_key} [{mode_used}] 选定费用 {cost:.0f}元，累计 {spent:.0f}/{total_budget:.0f}")

        if total_budget > 0 and spent > total_budget:
            over_budget = True
            budget_message = (
                f"交通费用累加已超出预算，计划无法实现。\n"
                f"  当前累计交通费用：{spent:.0f} 元\n"
                f"  总预算：{total_budget:.0f} 元\n"
                f"  超支发生在段：{leg_key}\n"
                f"建议：提高预算、减少城市数量，或选择更经济的交通方式。"
            )
            break

    logger.info(f"🕐 城市交通上下文: {city_transport_context}")
    return {
        "transport_costs": transport_costs,
        "spent_budget": spent,
        "spent_before_city": spent,  # 进入第一个城市前的花销 = 全部交通费
        "over_budget": over_budget,
        "budget_message": budget_message,
        "tool_results": tool_results,
        "city_transport_context": city_transport_context,
    }


# ──────────────────────────────────────────────────────────
# 节点 4a：交通计划选定（交通预检通过后，总结并确认交通方案）
# ──────────────────────────────────────────────────────────

@node("transport_select")
async def transport_select_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """交通计划选定：汇总交通预检结果，确定各段交通方式与费用"""
    pc = state.get("planner_context") or {}
    transport_costs = state.get("transport_costs", {}) or {}
    cities = state.get("cities", []) or []
    origin = pc.get("origin", "")

    # 构建交通计划摘要
    legs_summary: List[str] = []
    route_points = [origin] + cities + [origin]
    total_transport = 0.0
    for i in range(len(route_points) - 1):
        leg = f"{route_points[i]}->{route_points[i + 1]}"
        cost = transport_costs.get(leg, 0.0)
        total_transport += cost
        legs_summary.append(f"  {leg}：{cost:.0f} 元")

    plan_text = "\n".join(legs_summary)
    logger.info(f"🚆 [交通计划选定] 共 {len(legs_summary)} 段，交通总费用 {total_transport:.0f} 元\n{plan_text}")

    return {
        "tool_results": [{
            "tool": "transport_select",
            "transport_plan": {
                "legs": transport_costs,
                "total": round(total_transport, 2),
                "summary": plan_text,
            },
        }],
    }


# ──────────────────────────────────────────────────────────
# 节点 5：预算不足终止
# ──────────────────────────────────────────────────────────

async def _stream_overbudget_plan(state: Dict[str, Any], budget_message: str) -> str:
    """规划已完整生成但仍超预算：把完整方案连同超支说明一起交给用户做决策。

    与 summarizer 相同：plan / transport JSON 含花括号，不能走 ChatPromptTemplate
    （会二次插值报 "unmatched '{' in format spec"），必须用 SystemMessage/HumanMessage
    直接构造；llm.astream + stream_to_user tag 供前端实时展示。
    """
    city_plans = state.get("city_plans", []) or []
    transport_costs = state.get("transport_costs", {}) or {}
    total_budget = float(state.get("total_budget", 0) or 0)
    buffer_budget = float(state.get("buffer_budget", 0) or 0)
    spent = float(state.get("spent_budget", 0) or 0)
    user_query = state.get("user_query", "") or ""
    allow_total = round(total_budget + buffer_budget, 2)
    overrun = max(spent - allow_total, 0.0)

    plans_text = json.dumps(city_plans, ensure_ascii=False, indent=2)
    transport_text = json.dumps(transport_costs, ensure_ascii=False, indent=2)

    system_prompt = (
        "你是友好的旅游助手。这份旅行规划已经**完整生成**（含每个城市的交通、每日行程、景点门票与酒店），"
        "但系统已对各城市方案自动重规划最多 3 次，累计花费仍超出总预算，无法自动收敛。\n\n"
        "请按以下要求回复：\n"
        "1. 先把这份**完整规划方案**原样呈现给用户：按城市分段展示交通、每日行程、景点门票、"
        "推荐酒店与费用，所有数字必须与上方计划数据一致，绝不编造；\n"
        "2. 然后如实说明超支情况：累计花费、总预算（含弹性缓冲）、超支差额，以及主要超支的项目；\n"
        "3. 说明系统自动规划/重规划已尽力，仍需要用户协助决定下一步；\n"
        "4. 给出两个处理方向供用户选择：A. 调整需求（减少天数/城市数/更换目的地或出行日期等，系统可据此重新规划）；"
        "B. 指定缩减某部分预算（如减少景点门票、降低酒店档次或住宿晚数、选择更经济的交通方式，系统可按指定部分重新规划）；\n"
        "5. 结尾用一个明确的问题引导用户回复（例如：是否需要我调整需求，或者您想先缩减哪部分的预算？）。\n\n"
        "使用 Markdown 格式，段落间用空行分隔，城市分段用 ## 标题，关键数字用 **加粗**。\n\n"
        f"用户原始需求：{user_query}\n"
        f"总预算：{total_budget:.0f} 元（含弹性缓冲 {buffer_budget:.0f} 元，即上限 {allow_total:.0f} 元）\n"
        f"累计已花（=交通+景点+酒店总和）：{spent:.0f} 元\n"
        f"超支差额：{overrun:.0f} 元\n"
        f"各段交通费用：{transport_text}\n\n"
        f"完整城市计划数据：\n{plans_text}"
    )
    llm = _LLM(agent="transport", streaming=True)
    text = ""
    async for chunk in llm.astream([
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"请展示这份完整的旅行方案，说明超支情况，并征询我的决定。\n预算提示：{budget_message}"),
    ]):
        text += chunk.content
    return text


@node("budget_fail")
async def budget_fail_node(state: Dict[str, Any]) -> Dict[str, Any]:
    msg = state.get("budget_message") or "预算不足，该旅行计划无法实现。"
    # 是否已执行并发城内规划（含最多3次自动重规划）：是则说明重规划已耗尽仍超预算
    replanned = bool(state.get("city_plans"))
    if replanned:
        # 完整规划已实现但总预算仍超支 → 把完整方案交给用户做决策
        replan_note = (
            "系统已对各城市的城内方案自动进行最多 3 次重规划，仍无法将总花费控制在预算内。"
        )
        reply = await _stream_overbudget_plan(state, msg)
    else:
        replan_note = (
            "系统已自动校验并选定交通方案，但预算仍无法满足全部行程，计划无法继续执行。"
        )
        # 流式输出预算不足说明，便于前端实时展示
        reply = await _stream_reply(
            f"你是友好的旅游助手。{replan_note}\n"
            "请用真诚、委婉的语气向用户说明当前预算问题，并征询用户的处理意见：\n"
            "1. 完整保留所有数字与关键信息（累计费用、总预算、超支差额、超支项目等），说明超出的具体部分；\n"
            "2. 告知用户自动规划/重规划已尽力，仍需用户协助决定下一步；\n"
            "3. 向用户给出两个处理方向供其选择：\n"
            "   A. 调整需求：例如减少旅行天数、减少城市数量、更换目的地或出行日期，系统可据此重新规划；\n"
            "   B. 指定缩减预算的部分：例如减少景点门票花费、降低酒店档次或住宿晚数、选择更经济的交通方式，"
            "系统可按用户指定的部分重新规划；\n"
            "4. 结尾用一个明确的问题引导用户回复（例如：是否需要我调整需求，或者您想先缩减哪部分的预算？）。",
            msg,
            agent="transport",
        )
    return {
        "final_answer": reply,
        "is_complete": True,
        "messages": [AIMessage(content=reply)],
    }
