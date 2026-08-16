"""情景记忆：历史行程（episode）的抽取落库 + 检索 + few-shot 格式化。

- 写入：summarizer 完成后，从 city_plans + final_answer 结构化提取
- 检索：按 目的地 > 预算区间 > 出发地 优先级，最多注入 1-2 条 few-shot
- 直接问答：用户问"上次去X住哪/花了多少"时从 episode 检索返回
"""
from typing import Dict, Any, List, Optional

from memory.base import TripEpisode
from memory.storage import get_memory_storage

import logging
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
# 写入
# ══════════════════════════════════════════════════════════

def build_episode_from_plans(
    city_plans: List[Dict[str, Any]],
    *,
    session_id: str,
    user_id: str = "default_user",
    origin: str = "",
    total_budget: float = 0.0,
    total_spent: float = 0.0,
    transport_costs: Optional[Dict[str, float]] = None,
    summary: str = "",
) -> Optional[TripEpisode]:
    """从一次规划结果构建情景记忆事件。

    结构化字段（origin/destination/nights/budget/hotels/attractions/transport）
    直接从 city_plans JSON 提取；summary 由调用方（summarizer）传入。
    返回 None 表示无有效城市计划（不落库）。
    """
    if not city_plans:
        return None

    transport_costs = transport_costs or {}
    destination = city_plans[0].get("city", "")
    nights = 0
    hotels: List[Dict[str, Any]] = []
    attractions: List[Dict[str, Any]] = []
    transport_cost = round(sum(float(v or 0) for v in transport_costs.values()), 2)

    for cp in city_plans:
        city = cp.get("city", "")
        nights += int(cp.get("nights", 0) or 0)
        # 目的地 = 多个城市时拼接
        if city and city != destination and len(city_plans) > 1:
            destination = f"{destination},{city}"
        sh = cp.get("selected_hotel") or {}
        if sh.get("name"):
            hotels.append({
                "name": sh.get("name", ""),
                "price_per_night": float(sh.get("price_per_night", 0) or 0),
                "area": (sh.get("address") or "")[:50],
            })
        rp = cp.get("route_plan") or {}
        for name in (rp.get("selected_attractions") or []):
            attractions.append({"name": name, "city": city})

    # 解析出行日期区间（start_date 来自 planner_context.travel_date，这里可留空由外部补）
    episode = TripEpisode(
        user_id=user_id,
        session_id=session_id,
        origin=origin,
        destination=destination,
        nights=nights,
        total_budget=round(float(total_budget or 0), 2),
        total_spent=round(float(total_spent or 0), 2),
        transport_cost=transport_cost,
        transport_mode="",
        hotels=hotels,
        attractions=attractions,
        summary=(summary or "")[:800],
    )
    return episode


async def save_episode(episode: TripEpisode) -> Optional[int]:
    """保存一条情景记忆，返回 id。"""
    if not episode or not episode.destination:
        return None
    return await get_memory_storage().save_episode(episode)


async def update_episode_feedback(episode_id: int, feedback: str, satisfaction: Optional[int]):
    """更新历史行程的反馈与满意度。"""
    await get_memory_storage().update_episode_feedback(episode_id, feedback, satisfaction)


# ══════════════════════════════════════════════════════════
# 检索
# ══════════════════════════════════════════════════════════

async def search_episodes(
    *,
    user_id: str = "default_user",
    destination: str = "",
    origin: str = "",
    budget: float = 0.0,
    limit: int = 2,
) -> List[Dict[str, Any]]:
    """按 目的地 > 预算区间 > 出发地 优先级检索历史行程。

    先精确目的地；无匹配再放宽为预算区间；再无出发地；
    全部无匹配（或无过滤条件）时按 user_id 返回最近行程。最多 limit 条。
    """
    storage = get_memory_storage()

    # 1) 目的地优先
    if destination:
        hits = await storage.search_episodes(
            user_id=user_id, destination=destination, limit=limit,
        )
        if hits:
            return hits

    # 2) 预算区间兜底（±30%）
    if budget > 0:
        lo = round(budget * 0.7, 2)
        hi = round(budget * 1.3, 2)
        hits = await storage.search_episodes(user_id=user_id, min_budget=lo, max_budget=hi, limit=limit)
        if hits:
            return hits

    # 3) 出发地兜底
    if origin:
        hits = await storage.search_episodes(user_id=user_id, origin=origin, limit=limit)
        if hits:
            return hits

    # 4) 无过滤条件：返回该用户最近行程（跨会话"上次去哪/住哪"直接问答、反馈回写用）
    return await storage.search_episodes(user_id=user_id, limit=limit)


# ══════════════════════════════════════════════════════════
# few-shot 格式化
# ══════════════════════════════════════════════════════════

def format_episodes_for_prompt(episodes: List[Dict[str, Any]]) -> str:
    """将历史行程格式化为注入规划 prompt 的 few-shot 参考文本。

    无历史行程时返回空串（调用方可跳过注入）。
    """
    if not episodes:
        return ""
    lines = ["【历史行程参考（来自情景记忆，可参考其偏好与花费）】"]
    for i, ep in enumerate(episodes, 1):
        lines.append(f"- [{i}] {ep.get('origin', '?')} → {ep.get('destination', '?')}："
                     f"{ep.get('nights', 0)}晚，预算{ep.get('total_budget', 0):.0f}元，"
                     f"实际花费{ep.get('total_spent', 0):.0f}元，"
                     f"交通{ep.get('transport_cost', 0):.0f}元")
        if ep.get("hotels"):
            names = "、".join(f"{h.get('name','')}({h.get('price_per_night',0):.0f}元/晚)" for h in ep["hotels"][:3])
            lines.append(f"  住宿：{names}")
        if ep.get("attractions"):
            names = "、".join(a.get("name", "") for a in ep["attractions"][:6])
            lines.append(f"  景点：{names}")
        if ep.get("feedback"):
            lines.append(f"  反馈：{ep['feedback']}")
    return "\n".join(lines)
