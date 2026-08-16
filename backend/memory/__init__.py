"""记忆系统模块：工作记忆 / 情景记忆 / 语义记忆 的统一入口。

- base.py     基础数据结构（快照、事件、常量）
- storage.py  PostgreSQL 持久化（working_memory / trip_episodes 双表）
- working.py  工作记忆：会话内规划快照 + keep/replan 判定
- episodic.py 情景记忆：历史行程抽取落库 + 检索 + few-shot
- semantic.py 语义记忆：偏好蒸馏回写用户档案
- manager.py  统一管理器
"""
from memory.manager import (
    MemoryManager,
    get_memory_manager,
    get_few_shot_context,
    get_memory_storage,
)

__all__ = [
    "MemoryManager",
    "get_memory_manager",
    "get_few_shot_context",
    "get_memory_storage",
]
