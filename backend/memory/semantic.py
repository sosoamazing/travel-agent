"""语义记忆：从对话/计划中蒸馏用户偏好，增量回写用户档案（JSON）。

- 触发时机：summarizer 规划完成后
- 回写目标：复用 user_profile_manager 现有字段，去重后追加
- 不引入 Neo4j，档案 JSON 即语义记忆存储
"""
from typing import Dict, Any, List, Optional
import logging

logger = logging.getLogger(__name__)

# 允许回写的档案字段（与 user_profile_manager 默认结构一致）
_DISTILLABLE_LIST_FIELDS = [
    "travel_style",
    "destination_types",
    "hotel_preference",
    "room_type_preference",
    "transport_priority",
    "preferred_transport",
    "dietary_restrictions",
    "cuisine_preference",
    "liked_activities",
    "disliked_activities",
    "travel_season_preference",
]
_DISTILLABLE_SCALAR_FIELDS = ["budget_level", "daily_schedule_preference"]


def _load_profile() -> Dict[str, Any]:
    """加载当前用户档案（失败返回空 dict）。"""
    try:
        from user_profile_manager import get_profile_manager
        return get_profile_manager().load_profile()
    except Exception as e:
        logger.warning(f"🧠 [语义记忆] 加载档案失败: {e}")
        return {}


def _save_profile(profile: Dict[str, Any]):
    try:
        from user_profile_manager import get_profile_manager
        get_profile_manager().save_profile(profile)
    except Exception as e:
        logger.warning(f"🧠 [语义记忆] 保存档案失败: {e}")


def merge_preferences(updates: Dict[str, Any]) -> Dict[str, Any]:
    """将提取的偏好增量合并进用户档案（列表去重追加，标量覆盖）。

    Args:
        updates: {"travel_style": [...], "budget_level": "舒适型", ...}

    Returns:
        实际被更新的字段: {字段: 新值}
    """
    profile = _load_profile()
    if not profile:
        return {}

    changed: Dict[str, Any] = {}
    for key, value in (updates or {}).items():
        if key not in profile:
            continue
        if key in _DISTILLABLE_LIST_FIELDS and isinstance(value, list):
            merged = list(profile.get(key) or [])
            added = False
            for item in value:
                item = str(item).strip()
                if item and item not in merged:
                    merged.append(item)
                    added = True
            if added:
                profile[key] = merged
                changed[key] = merged
        elif key in _DISTILLABLE_SCALAR_FIELDS and value:
            if profile.get(key) != value:
                profile[key] = value
                changed[key] = value

    if changed:
        _save_profile(profile)
        logger.info(f"🧠 [语义记忆] 档案更新: {list(changed.keys())} = {changed}")
    return changed


def distill_from_city_plans(city_plans: List[Dict[str, Any]], user_query: str = "") -> Dict[str, Any]:
    """从一次已完成的规划中蒸馏偏好（无额外 LLM 调用，纯结构化规则）。

    - 用户查询中显式的酒店档次/菜系/活动词 → 直接提取
    - 依据选定酒店价位推断 budget_level
    """
    updates: Dict[str, Any] = {}

    # 从用户查询中提取显式偏好词
    if user_query:
        style_kw = ["古镇", "古城", "海岛", "海滨", "爬山", "徒步", "自驾", "亲子", "美食", "文化", "自然", "度假"]
        liked = [kw for kw in style_kw if kw in user_query]
        if liked:
            updates["liked_activities"] = list(dict.fromkeys(updates.get("liked_activities", []) + liked))

        cuisine_kw = ["川菜", "粤菜", "湘菜", "火锅", "烧烤", "海鲜", "小吃", "日料", "西餐", "本帮菜", "鲁菜", "东北菜"]
        cuisines = [kw for kw in cuisine_kw if kw in user_query]
        if cuisines:
            updates["cuisine_preference"] = cuisines

        hotel_kw = ["民宿", "五星", "四星", "豪华", "经济", "连锁", "海景", "温泉"]
        hotels = [kw for kw in hotel_kw if kw in user_query]
        if hotels:
            updates["hotel_preference"] = hotels

    # 依据选定酒店均价推断 budget_level（仅在查询未指定时）
    prices = []
    for cp in (city_plans or []):
        sh = cp.get("selected_hotel") or {}
        p = float(sh.get("price_per_night", 0) or 0)
        if p > 0:
            prices.append(p)
    if prices:
        avg = sum(prices) / len(prices)
        if avg >= 500:
            updates["budget_level"] = "豪华型"
        elif avg >= 250:
            updates["budget_level"] = "舒适型"
        else:
            updates["budget_level"] = "经济型"

    return merge_preferences(updates)
