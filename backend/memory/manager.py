"""记忆系统统一入口：MemoryManager 协调工作/情景/语义三层记忆。

供各 agent 节点调用，统一读写/检索/决策入口。
"""
from typing import Dict, Any, List, Optional

from memory import base
from memory import working
from memory import episodic
from memory import semantic
from memory.storage import get_memory_storage

import logging
logger = logging.getLogger(__name__)


class MemoryManager:
    """统一记忆管理器（轻封装，内部委托各记忆子模块）。"""

    # ── 工作记忆 ──

    async def load_snapshot(self, session_id: str, user_id: str = "default_user"):
        return await working.load_snapshot(session_id, user_id)

    async def save_snapshot(self, snapshot):
        await working.save_snapshot(snapshot)

    async def clear_snapshot(self, session_id: str, user_id: str = "default_user"):
        await working.clear_snapshot(session_id, user_id)

    def build_snapshot(self, **kwargs):
        return working.build_snapshot(**kwargs)

    def update_city_state(self, snapshot, city, **kwargs):
        working.update_city_state(snapshot, city, **kwargs)

    async def decide_city_flags(self, snapshot, cities, user_query, user_id="default_user"):
        return await working.decide_city_flags(snapshot, cities, user_query, user_id)

    # ── 情景记忆 ──

    def build_episode(self, city_plans, **kwargs):
        return episodic.build_episode_from_plans(city_plans, **kwargs)

    async def save_episode(self, episode) -> Optional[int]:
        return await episodic.save_episode(episode)

    async def update_episode_feedback(self, episode_id: int, feedback: str, satisfaction: Optional[int]):
        await episodic.update_episode_feedback(episode_id, feedback, satisfaction)

    async def search_episodes(self, **kwargs) -> List[Dict[str, Any]]:
        return await episodic.search_episodes(**kwargs)

    def format_episodes_for_prompt(self, episodes: List[Dict[str, Any]]) -> str:
        return episodic.format_episodes_for_prompt(episodes)

    # ── 语义记忆 ──

    def distill_from_city_plans(self, city_plans: List[Dict[str, Any]], user_query: str = "") -> Dict[str, Any]:
        return semantic.distill_from_city_plans(city_plans, user_query)


_memory_manager: Optional[MemoryManager] = None


def get_memory_manager() -> MemoryManager:
    """获取全局记忆管理器实例。"""
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager()
    return _memory_manager


# ── 便捷导出（供节点直接使用） ──

async def get_few_shot_context(
    *,
    destination: str = "",
    origin: str = "",
    budget: float = 0.0,
    user_id: str = "default_user",
    limit: int = 2,
) -> str:
    """检索历史行程并格式化为 few-shot 参考文本（无历史返回空串）。"""
    episodes = await episodic.search_episodes(
        user_id=user_id, destination=destination, origin=origin, budget=budget, limit=limit,
    )
    return episodic.format_episodes_for_prompt(episodes)


__all__ = [
    "MemoryManager",
    "get_memory_manager",
    "get_few_shot_context",
    "base",
    "working",
    "episodic",
    "semantic",
    "get_memory_storage",
]
