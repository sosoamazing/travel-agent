"""提示词模板目录：稳定 prompt_id + 内容 hash 版本。

模板正文存在这里（静态骨架）。一次调用实际送进模型的渲染结果走 obs_llm_payloads，
不要把用户数据写进版本表。

版本 = sha256(正文)[:12]。内容不变则版本不变；改一个标点就是新版本。
"""
from __future__ import annotations

import hashlib
from typing import Dict, Tuple

from config.prompts import PLANNER_SYSTEM_PROMPT

# ── 静态模板（后续节点可继续往这里抽）────────────────────────────────

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
- 期望的 JSON 结构: {schema_hint}"""

_PROMPTS: Dict[str, str] = {
    "classify_system": CLASSIFY_SYSTEM,
    "conversation_reply": CONVERSATION_REPLY_SYSTEM,
    "feedback_reply": FEEDBACK_REPLY_SYSTEM,
    "json_fix": JSON_FIX_SYSTEM,
    "planner_system": PLANNER_SYSTEM_PROMPT,
}


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def get_prompt(prompt_id: str) -> Tuple[str, str]:
    """返回 (正文, version)。未知 id 抛 KeyError。"""
    content = _PROMPTS[prompt_id]
    return content, content_hash(content)


def all_prompts() -> Dict[str, Tuple[str, str]]:
    """prompt_id → (content, version)。"""
    return {pid: (body, content_hash(body)) for pid, body in _PROMPTS.items()}
