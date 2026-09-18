"""提示词目录：agent + usage + version + content。

运行时只查 PostgreSQL `prompt_versions` 的最新一行（ORDER BY created_at DESC）。
代码里的 SEEDS 只做空库种子 / DB 不可用时的回退，不是热路径缓存。

版本 = sha256(正文)[:12]。占位符用 {{name}}，用 render_prompt 替换，禁止 str.format
（模板里常有 JSON 花括号）。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Dict, List, Optional, Tuple

from config.prompts import PLANNER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# ── 静态骨架（空库种子；独立 insert 程序写入后以库为准）────────────────

CLASSIFY_SYSTEM = """你是查询分类器。判断用户查询属于哪一类：
- feedback: 用户在表达偏好/反馈（如"我喜欢古镇"、"下次别推荐寺庙"、"预算改成3000"、"改成去杭州"）
- conversation: 纯对话（问候、感谢、再见、"你是谁"等，不涉及旅行需求）
- information: 通用信息查询（天气、两地距离、某地概况、美食推荐等，不涉及行程规划和预算）
- travel: 旅游规划相关（需要规划多天/多城市行程、制定路线、安排住宿等）
只返回分类结果：feedback / conversation / information / travel"""

CONVERSATION_REPLY_SYSTEM = (
    "你是一个友好的旅游助手。请用简洁、温暖的中文回复用户的问候或对话。"
)

FEEDBACK_REPLY_SYSTEM = (
    "你是友好的旅游助手，请用亲切的中文回应，可自然提及已记住用户偏好。"
)

JSON_FIX_SYSTEM = """你是一个 JSON 格式修正器。
请将用户提供的内容转换为合法的 JSON，严格按以下规则：
- 只输出 JSON，不要添加任何解释、代码块标记或其他文字
- 确保所有引号、括号正确闭合
- 字符串用双引号，不要用单引号
- 期望的 JSON 结构: {{schema_hint}}"""

PARAMS_PARSE_CITIES = """从用户旅行查询中提取要游览的城市列表（按游览顺序排列）。
规则：
- 若用户明确要在出发地游览（如'先在本地玩几天再去...'），则将出发地作为第一个城市
- 若出发地仅是起点不游览，则不要包含出发地
- 只返回城市名，用逗号分隔，例如：上海,苏州,杭州。若只有一个城市就返回一个。

{{time_anchors}}
用户查询：{{user_query}}
出发地：{{origin}}
已提取的目的地字段：{{destination}}"""

PARAMS_CLARIFY = (
    "你是友好的旅游助手。请用亲切的语气重述以下澄清问题，保持原意，不要添加额外建议。"
)

ATTRACTIONS_EXTRACT = """请提取该城市的适量候选景点（6-10 个，需含备份景点），每个景点包含：
- name: 名称
- ticket_price: 门票价格（元/人），无门票填 0
- visit_hours: 建议游玩时长（小时），每个至少 1.5
- location: 经纬度 "lng,lat"（尽量从高德 POI 结果中取，没有则空）
- description: 一句话特色

要求：
1. 优先选择符合用户偏好的景点
2. 必须包含备份景点（多于实际游玩数量）
3. 若 RAG 与 POI 均为空，可凭训练记忆给出该城市知名景点

城市：{{city}}
用户偏好：{{preferences}}
用户查询：{{user_query}}
{{few_shot_lines}}

【RAG 知识库攻略】
{{rag_raw}}

【高德 POI 搜索结果（含 location 坐标）】
{{poi_raw}}
"""

ROUTE_DISTANCE_PARSE = """从以下高德驾车路线结果中提取距离（公里）。只输出数字。
{{raw}}"""

ROUTE_PLAN = """你是旅行路线规划师。请为「以上城市」规划景点游玩路线。

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

【城市】{{city}}
【建议游玩天数】{{suggested_days}} 天（总旅行天数 {{travel_days}}，共 {{cities_count}} 个城市）{{budget_hint}}{{transport_hint}}
{{few_shot_lines}}

候选景点：
{{attraction_lines}}

用户偏好：{{preferences}}
用户查询：{{user_query}}
"""

HOTEL_PRICE = """请为列表中每个酒店给出**该入住日期**的每晚房价估价（元/晚）。

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

城市：{{city}}
{{date_hint}}
酒店列表（来自高德 POI，不含房价）：
{{hotels_brief}}
"""

HOTEL_SELECT = """你是酒店选择专家。请为「以上城市」的行程从以下候选酒店中选择**最合适的一家**。

选择要求（按优先级）：
1. 位置：优先靠近景点几何中心 / daily_starting_point / 首日出发地 / 多数景点（候选已附距中心距离），减少每天通勤
2. 价格：每晚价格尽量不超过 per_night_budget；若全部超预算，选最接近上限的
3. 偏好：契合用户偏好（如靠近地铁/商圈/安静/亲子等，可从酒店名与地址判断）

只输出一个 JSON 对象，不要任何额外文字：
{{"name": "选中的酒店名称（必须与候选列表完全一致）", "reason": "一句话理由，说明位置与预算契合度"}}

【城市】{{city}}
【用户需求】{{user_query}}
【用户偏好】{{pref_str}}
【酒店预算】共 {{nights}} 晚，每晚上限约 {{per_night_budget}} 元（总 {{hotel_budget}} 元）

【候选酒店】
{{hotel_lines}}

【每日行程位置线索】
{{days_info}}
全市统一出发参考点：{{daily_starting_point}}
景点几何中心（到各景点距离和最小的参考点，越近越省通勤）：{{center}}
"""

BUDGET_ALLOC = """请将旅游预算智能分配到以下每个城市。

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

注意：city_budgets 必须按城市列表的顺序，覆盖全部 {{alloc_count}} 座城市。

用户查询：{{user_query}}
用户偏好：{{preferences}}
出行日期：{{travel_date}}
总预算：{{total_budget}} 元
交通总花费：{{transport_total}} 元
各段交通明细：
{{transport_lines}}
可分配剩余预算（景点+酒店，已扣除锁定花费）：{{pool}} 元
城市列表（共 {{alloc_count}} 座，仅需分配这些）：
{{cities_line}}
{{lock_line}}
"""

TRANSPORT_CHOOSE_TOOLS = """可用工具：
- train_query: 查询12306火车余票和票价（参数: origin, destination, date）
- driving_cost_query: 查询自驾距离、油费、高速费、耗时（参数: from, to）

请决定需要调用哪些工具来比较交通方案。
输出要调用的工具名，用逗号分隔。例如：train_query,driving_cost_query
若用户明确要求自驾，可只调用 driving_cost_query。
只输出工具名，不要其他文字。

路线：{{from_city}} → {{to_city}}
用户查询：{{user_query}}
出行日期：{{travel_date}}"""

TRANSPORT_CHOOSE_MODE = """请从以下两个交通方案中选择更优的一个。

选择规则：
- 综合考虑价格、时间、便利性
- 若自驾距离超过 {{driving_max_km}}km，不推荐自驾
- 若用户明确要求自驾/开车，选 driving
- 只输出一个词：train 或 driving

出行人数：{{persons}}人
路线：{{from_city}} → {{to_city}}
用户查询：{{user_query}}

【方案1：火车】
{{train_brief}}
最低票价：{{train_price}}元/人，{{persons}}人合计约 {{train_total}}元

【方案2：自驾】
驾车距离：{{distance_km}}km
预计耗时：{{duration_hours}}小时
油费：{{fuel_cost}}元
高速费：{{toll_cost}}元
自驾总费用：{{driving_cost}}元（不随人数变化）"""

TRANSPORT_ESTIMATE_SCHEDULE = """请估算合理的出发时间和到达时间，以及出发/到达的大致位置（如市中心）。
输出 JSON：{{"departure_time": "HH:MM", "arrival_time": "HH:MM", "departure_location": "位置", "arrival_location": "位置"}}
只输出 JSON，不要其他文字。

自驾路线：{{from_city}} → {{to_city}}
距离：{{distance_km}}km，预计行驶：{{duration_hours}}小时
出行日期：{{travel_date}}"""

TRANSPORT_EXTRACT_FARE = """请从以下{{mode}}查询结果中，提取从 {{from_city}} 到 {{to_city}} 的最便宜票价（每人），并按用户查询中提到的人数计算该段总费用。

规则：
- 若无法识别票价，返回 0
- 只输出一个数字（总费用，单位：元），不要任何文字或符号

用户查询：{{user_query}}
查询结果（节选）：
{{text}}
"""

TRANSPORT_OVERBUDGET_PLAN = """你是友好的旅游助手。这份旅行规划已经**完整生成**（含每个城市的交通、每日行程、景点门票与酒店），
但系统已对各城市方案自动重规划最多 3 次，累计花费仍超出总预算，无法自动收敛。

请按以下要求回复：
1. 先把这份**完整规划方案**原样呈现给用户：按城市分段展示交通、每日行程、景点门票、
推荐酒店与费用，所有数字必须与上方计划数据一致，绝不编造；
2. 然后如实说明超支情况：累计花费、总预算（含弹性缓冲）、超支差额，以及主要超支的项目；
3. 说明系统自动规划/重规划已尽力，仍需要用户协助决定下一步；
4. 给出两个处理方向供用户选择：A. 调整需求（减少天数/城市数/更换目的地或出行日期等，系统可据此重新规划）；
B. 指定缩减某部分预算（如减少景点门票、降低酒店档次或住宿晚数、选择更经济的交通方式，系统可按指定部分重新规划）；
5. 结尾用一个明确的问题引导用户回复（例如：是否需要我调整需求，或者您想先缩减哪部分的预算？）。

使用 Markdown 格式，段落间用空行分隔，城市分段用 ## 标题，关键数字用 **加粗**。

用户原始需求：{{user_query}}
总预算：{{total_budget}} 元（含弹性缓冲 {{buffer_budget}} 元，即上限 {{allow_total}} 元）
累计已花（=交通+景点+酒店总和）：{{spent}} 元
超支差额：{{overrun}} 元
各段交通费用：{{transport_text}}

完整城市计划数据：
{{plans_text}}"""

TRANSPORT_OVERBUDGET_EARLY = """你是友好的旅游助手。系统已自动校验并选定交通方案，但预算仍无法满足全部行程，计划无法继续执行。
请用真诚、委婉的语气向用户说明当前预算问题，并征询用户的处理意见：
1. 完整保留所有数字与关键信息（累计费用、总预算、超支差额、超支项目等），说明超出的具体部分；
2. 告知用户自动规划/重规划已尽力，仍需用户协助决定下一步；
3. 向用户给出两个处理方向供其选择：
   A. 调整需求：例如减少旅行天数、减少城市数量、更换目的地或出行日期，系统可据此重新规划；
   B. 指定缩减预算的部分：例如减少景点门票花费、降低酒店档次或住宿晚数、选择更经济的交通方式，
系统可按用户指定的部分重新规划；
4. 结尾用一个明确的问题引导用户回复（例如：是否需要我调整需求，或者您想先缩减哪部分的预算？）。"""

SUMMARIZER_PLAN = """你是一位专业的旅游规划师。请基于已完成的多城市规划数据，为用户生成一份完整、清晰、实用的旅行方案。

{{user_preferences_str}}
{{few_shot_lines}}

核心规则：
- 严格基于下方「城市计划数据」呈现，绝不编造景点/酒店/价格
- city_plan 中可用字段：city, route_plan(含 selected_attractions/days/attractions_cost), hotels(含 name/price_per_night), selected_hotel(LLM 已选定的最佳酒店，含 name/price_per_night/selected_reason), transport_cost, hotel_cost, nights, per_city_budget
- 禁止编造 city_plan 中不存在的字段，特别是：餐饮费、杂费、前期费用、已花销等。如需提及餐饮，仅作为行程建议而非预算项
- 「累计已花」已经包含全部交通费+景点费+酒店费，是总花费，不要再拆分或额外叠加
- 按城市顺序分段展示，每段包含：交通方案、每日行程（确保每景点≥1.5h、含2h午餐休息）、景点门票、**推荐酒店（优先展示 selected_hotel，并简述其位置/价格契合度，可再附 1-2 个备选）**、该市预算
- 末尾给出总预算汇总（交通+门票+酒店）与剩余预算
- 语气友好专业，使用 emoji 与分隔符提升可读性

输出格式要求（重要）：
- 使用纯 Markdown 格式，禁止使用 HTML 标签（特别是 <br>、<br/>、<p> 等）
- 换行用 Markdown 方式：段落之间用空行分隔，列表项用 - 开头
- 用 ## 作为城市分段标题，用 **加粗** 强调关键字段

用户原始需求：{{user_query}}
总预算：{{total_budget}} 元
累计已花（=交通+景点+酒店总和，不要再叠加）：{{spent}} 元
各段交通费用：{{transport_text}}

城市计划数据：
{{plans_text}}
"""

INFO_QUERY_HISTORY = """你是友好的旅游助手。用户询问自己之前的历史行程。
请基于以下历史行程记录，用亲切、简洁的中文回答。
若记录中缺少用户问的信息（如具体酒店名），如实说明未记录，不要编造。"""

INFO_QUERY_SELECT_TOOLS = """你是信息查询助手，根据用户查询选择合适的工具。最多选{{max_tools}}个，按需选择。

当前时间：{{now}}"""

INFO_QUERY_ANSWER = """你是一个友好的旅游助手。请基于以下查询结果，用亲切、简洁的中文回答用户。
如有具体数据（温度、距离、时间、八字、五行等），务必完整保留。不要编造信息。"""

INFO_QUERY_ASK_DESTINATION = (
    "你是友好的旅游助手。用户的查询信息不足，请用友好的语气询问用户想去哪个城市。"
)

MEMORY_FLAGS = """你是旅行规划的记忆决策器。用户此前有一个正在进行的旅行规划，现在提出了新的需求。
请判断对每个已规划城市，是「keep」还是「replan」。

判定规则：
- keep：在原计划基础上继续（包括：已规划完且用户未要求修改该城；或该城只规划到一半需要接着补齐）
- replan：用户的新需求明显影响了该城（改了该城景点/酒店/天数/偏好），或该城计划需要整体重做

只输出 JSON，不要任何解释。格式：
{{"flags": {{"城市名": "keep"或"replan", ...}}}}
必须覆盖以下全部 {{city_count}} 个城市。

当前进行中的规划快照（各城状态）：
{{known_lines}}

用户最新需求：{{user_query}}
"""

FEEDBACK_ANALYZE = """你是一个用户反馈分析专家。请分析用户的反馈，提取用户偏好的更新。

当前用户档案：
{{current_profile}}

请分析用户的反馈，并只输出一个 JSON 对象，不要添加任何解释、代码块标记或其他文字：
{{
    "feedback_type": "positive|negative|neutral|core_change",
    "preference_updates": {{
        "travel_style": ["要添加的旅行风格"],
        "destination_types": ["要添加的目的地类型"],
        "budget_level": "预算水平",
        "hotel_preference": ["要添加的住宿偏好"],
        "dietary_restrictions": ["要添加的饮食禁忌"],
        "cuisine_preference": ["要添加的菜系偏好"],
        "liked_activities": ["要添加的喜欢的活动"],
        "disliked_activities": ["要添加的不喜欢的活动"],
        "transport_priority": ["交通优先级"]
    }},
    "confirmation_message": "给用户的友好确认消息，说明你记住了什么",
    "needs_replan": true/false
}}

注意：
- feedback_type 说明：
  * positive: 正向反馈（"我喜欢古镇"）
  * negative: 负向反馈（"我不喜欢寺庙"）
  * neutral: 中性反馈
  * core_change: 核心需求改变（如"预算改成2000"、"改成去杭州"、"改成玩5天"）
- needs_replan:
  * 如果是 core_change（核心需求改变），设为 true
  * 如果只是微调偏好但核心需求没变，设为 false
- 只在用户明确提到时才更新对应字段
- 列表类型的字段是追加新项，不是替换
- budget_level 可选值："经济型", "舒适型", "豪华型"
- 如果用户没有提到某个偏好，就不要包含在 preference_updates 中
- confirmation_message 要友好自然，让用户知道你记住了什么"""

# (agent, usage) → 模板正文。usage 是稳定槽位名，不是「第几次」。
SEEDS: Dict[Tuple[str, str], str] = {
    ("classify", "system"): CLASSIFY_SYSTEM,
    ("conversation_reply", "system"): CONVERSATION_REPLY_SYSTEM,
    ("feedback", "reply"): FEEDBACK_REPLY_SYSTEM,
    ("feedback", "analyze"): FEEDBACK_ANALYZE,
    ("json_fix", "system"): JSON_FIX_SYSTEM,
    ("planner", "extract"): PLANNER_SYSTEM_PROMPT,
    ("params", "parse_cities"): PARAMS_PARSE_CITIES,
    ("params", "clarify"): PARAMS_CLARIFY,
    ("attractions", "extract"): ATTRACTIONS_EXTRACT,
    ("route_distance", "parse"): ROUTE_DISTANCE_PARSE,
    ("route_plan", "plan"): ROUTE_PLAN,
    ("hotel_price", "estimate"): HOTEL_PRICE,
    ("hotel_select", "choose"): HOTEL_SELECT,
    ("budget_alloc", "allocate"): BUDGET_ALLOC,
    ("transport", "choose_tools"): TRANSPORT_CHOOSE_TOOLS,
    ("transport", "choose_mode"): TRANSPORT_CHOOSE_MODE,
    ("transport", "estimate_schedule"): TRANSPORT_ESTIMATE_SCHEDULE,
    ("transport", "extract_fare"): TRANSPORT_EXTRACT_FARE,
    ("transport", "overbudget_plan"): TRANSPORT_OVERBUDGET_PLAN,
    ("transport", "overbudget_early"): TRANSPORT_OVERBUDGET_EARLY,
    ("summarizer", "plan"): SUMMARIZER_PLAN,
    ("info_query", "history"): INFO_QUERY_HISTORY,
    ("info_query", "select_tools"): INFO_QUERY_SELECT_TOOLS,
    ("info_query", "answer"): INFO_QUERY_ANSWER,
    ("info_query", "ask_destination"): INFO_QUERY_ASK_DESTINATION,
    ("memory_flags", "decide"): MEMORY_FLAGS,
}


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def prompt_id(agent: str, usage: str) -> str:
    return f"{agent}/{usage}"


def render_prompt(template: str, **kwargs) -> str:
    """用 {{name}} 替换，不走 str.format（避免 JSON 花括号爆炸）。"""
    text = template or ""
    for key, value in kwargs.items():
        text = text.replace("{{" + key + "}}", "" if value is None else str(value))
    return text


def seed_rows() -> List[Tuple[str, str, str, str]]:
    """(agent, usage, content, version) 种子行。"""
    rows = []
    for (agent, usage), body in SEEDS.items():
        rows.append((agent, usage, body, content_hash(body)))
    return rows


def seed_prompt(agent: str, usage: str) -> Tuple[str, str]:
    body = SEEDS[(agent, usage)]
    return body, content_hash(body)


async def get_prompt(agent: str, usage: str) -> Tuple[str, str]:
    """取该槽位最新模板：(content, version)。库无行或失败则回退种子。"""
    try:
        from db import async_db_connection

        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT content, version FROM prompt_versions
                       WHERE agent=%s AND usage=%s
                       ORDER BY created_at DESC LIMIT 1""",
                    (agent, usage),
                )
                row = await cur.fetchone()
                if row and row.get("content"):
                    return row["content"], row["version"]
    except Exception as e:
        logger.warning("⚠️ get_prompt(%s/%s) 读库失败，回退种子: %s", agent, usage, e)
    try:
        return seed_prompt(agent, usage)
    except KeyError:
        raise KeyError(f"未知提示词槽位: {agent}/{usage}") from None


async def insert_prompt(agent: str, usage: str, content: str,
                        version: Optional[str] = None) -> str:
    """插入一条模板。version 默认内容 hash。已存在则忽略。返回 version。"""
    from db import async_db_connection

    ver = version or content_hash(content)
    async with async_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO prompt_versions (agent, usage, version, content, created_at)
                   VALUES (%s,%s,%s,%s,EXTRACT(EPOCH FROM NOW()))
                   ON CONFLICT (agent, usage, version) DO NOTHING""",
                (agent, usage, ver, content),
            )
    return ver


async def latest_prompt_set() -> Dict[str, str]:
    """每个 (agent, usage) 当前最新 version，供 release 快照。"""
    from db import async_db_connection

    async with async_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT DISTINCT ON (agent, usage) agent, usage, version
                   FROM prompt_versions
                   ORDER BY agent, usage, created_at DESC"""
            )
            rows = await cur.fetchall()
    return {prompt_id(r["agent"], r["usage"]): r["version"] for r in rows}
