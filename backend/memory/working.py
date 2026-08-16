"""工作记忆：会话内规划状态快照的读写 + keep/replan 判定。

- 读写走 MemoryStorage（PostgreSQL），TTL 24h
- decide_city_flags：LLM 对比「现有快照 + 新需求」输出每城 keep/replan
"""
from typing import Dict, Any, List, Optional

from memory.base import (
    WorkingMemorySnapshot, WorkingCityState,
    CITY_PENDING, CITY_PARTIAL, CITY_DONE,
    FLAG_KEEP, FLAG_REPLAN, DEFAULT_TTL_SECONDS,
)
from memory.storage import get_memory_storage

import logging
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
# 读写
# ══════════════════════════════════════════════════════════

async def load_snapshot(session_id: str, user_id: str = "default_user") -> Optional[WorkingMemorySnapshot]:
    """加载会话的工作记忆快照（已过期自动忽略）。"""
    if not session_id:
        return None
    return await get_memory_storage().load_snapshot(session_id, user_id)


async def save_snapshot(snapshot: WorkingMemorySnapshot):
    """持久化工作记忆快照。"""
    if not snapshot.session_id:
        return
    await get_memory_storage().save_snapshot(snapshot)


async def clear_snapshot(session_id: str, user_id: str = "default_user"):
    """清除会话的工作记忆。"""
    if session_id:
        await get_memory_storage().delete_snapshot(session_id, user_id)


def build_snapshot(
    session_id: str,
    user_id: str,
    total_budget: float,
    transport_costs: Dict[str, float],
    buffer_budget: float,
) -> WorkingMemorySnapshot:
    """从一次规划的状态字段构建新快照（各城默认 pending）。"""
    snap = WorkingMemorySnapshot(
        session_id=session_id,
        user_id=user_id,
        total_budget=round(float(total_budget or 0), 2),
        transport_costs=dict(transport_costs or {}),
        buffer_budget=round(float(buffer_budget or 0), 2),
    )
    snap.transport_total = round(sum(float(v or 0) for v in snap.transport_costs.values()), 2)
    return snap


def update_city_state(
    snapshot: WorkingMemorySnapshot,
    city: str,
    *,
    status: str = CITY_DONE,
    spent: float = 0.0,
    locked: bool = True,
    plan: Optional[Dict[str, Any]] = None,
    budget: Optional[Dict[str, Any]] = None,
):
    """更新快照中某城市的状态（不存在则新建）。"""
    prev = snapshot.cities.get(city)
    if prev is None:
        prev = WorkingCityState(city=city)
    prev.status = status
    prev.spent = round(float(spent or 0), 2)
    prev.locked = locked
    if plan is not None:
        prev.plan = plan
    if budget is not None:
        prev.budget = budget
    snapshot.cities[city] = prev


# ══════════════════════════════════════════════════════════
# keep/replan 判定
# ══════════════════════════════════════════════════════════

async def decide_city_flags(
    snapshot: Optional[WorkingMemorySnapshot],
    cities: List[str],
    user_query: str,
    user_id: str = "default_user",
) -> Dict[str, str]:
    """为每个城市输出 keep/replan 判定。

    - 无快照 / 快照过期：全部 replan（等价正常规划）
    - 有快照：LLM 对比现有计划摘要与最新需求，逐城判定
    """
    flags: Dict[str, str] = {c: FLAG_REPLAN for c in cities}

    if not snapshot or not snapshot.cities or not cities:
        return flags

    # 只对快照中存在的城市判定；新城市一律 replan
    known = [c for c in cities if c in snapshot.cities]
    if not known:
        return flags

    # 快照中已 done 的城市摘要
    def _brief(city: str) -> str:
        st = snapshot.cities.get(city)
        if st is None:
            return "（无历史计划）"
        plan = st.plan or {}
        hotels = plan.get("selected_hotel") or {}
        attrs = (plan.get("route_plan") or {}).get("selected_attractions") or []
        return (
            f"状态={st.status}, 已花费={st.spent:.0f}元, "
            f"景点={','.join(attrs)[:120] or '无'}, "
            f"酒店={hotels.get('name', '无')}({hotels.get('price_per_night', 0)}元/晚)"
        )

    known_lines = "\n".join(f"- {c}: {_brief(c)}" for c in known)

    from langchain_core.messages import HumanMessage
    from agent_nodes._common import _LLM
    llm = _LLM(agent="memory_flags", temperature=0.0)

    prompt = f"""你是旅行规划的记忆决策器。用户此前有一个正在进行的旅行规划，现在提出了新的需求。
请判断对每个已规划城市，是「keep」还是「replan」。

判定规则：
- keep：在原计划基础上继续（包括：已规划完且用户未要求修改该城；或该城只规划到一半需要接着补齐）
- replan：用户的新需求明显影响了该城（改了该城景点/酒店/天数/偏好），或该城计划需要整体重做

只输出 JSON，不要任何解释。格式：
{{"flags": {{"城市名": "keep"或"replan", ...}}}}
必须覆盖以下全部 {len(known)} 个城市。

当前进行中的规划快照（各城状态）：
{known_lines}

用户最新需求：{user_query}
"""
    try:
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        from tools.json_utils import extract_json_block
        import json
        content = extract_json_block(resp.content)
        data = json.loads(content)
        llm_flags = data.get("flags") or {}
        for c in known:
            f = str(llm_flags.get(c, "")).strip().lower()
            flags[c] = FLAG_KEEP if f == "keep" else FLAG_REPLAN
    except Exception as e:
        logger.warning(f"🧠 [记忆判定] LLM 失败，默认全部 replan: {e}")

    logger.info("🧠 [记忆判定] " + ", ".join(f"{c}={v}" for c, v in flags.items() if c in known))
    return flags
