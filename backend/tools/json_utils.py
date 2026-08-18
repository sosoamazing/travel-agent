"""
JSON 解析工具 - 从 LLM 输出中提取合法 JSON
统一处理 ```json 代码块剥离 + 花括号截断，避免各模块重复实现

同时提供全局 DeepSeek Flash 客户端，用于 JSON 格式修正这类轻量任务。
"""
import logging
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage

from agent_nodes._common import _LLM
from config.settings import JSON_FIX_MAX_ATTEMPTS

logger = logging.getLogger(__name__)

# ── 全局 DFS Flash 客户端（模块级单例，复用连接池；走 _LLM 以纳入 token 统计） ──
_ds_flash_llm = _LLM(agent="json_fix", model_type="flash")


def extract_json_block(text: str) -> str:
    """从 LLM 输出文本中提取 JSON 内容。

    处理两种情况：
    1. 被 ```json ... ``` 或 ``` ... ``` 代码块包裹
    2. JSON 后面有多余文字（通过花括号配对截断）

    Returns:
        清理后的纯 JSON 字符串（如果文本不以 '{' 开头则原样返回.strip()）
    """
    content = text.strip()

    # 剥离 Markdown 代码块
    if "```json" in content:
        s = content.find("```json") + 7
        e = content.find("```", s)
        if e != -1:
            content = content[s:e]
    elif "```" in content:
        s = content.find("```") + 3
        e = content.find("```", s)
        if e != -1:
            content = content[s:e]

    # 花括号截断：去掉 JSON 后多余的说明文字
    if content.startswith("{"):
        depth = 0
        for i, ch in enumerate(content):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    content = content[:i + 1]
                    break

    return content.strip()


async def fix_json_with_flash(
    bad_output: str,
    schema_hint: str = "合法的 JSON 对象",
    max_attempts: Optional[int] = None,
) -> dict:
    """用 DeepSeek Flash 修正格式不合法的 JSON 输出。

    Args:
        bad_output: 上次的失败输出（可能含代码块、多余文字、语法错误）
        schema_hint: 期望的 JSON 结构简述，帮助模型理解格式
        max_attempts: 最大重试次数；None 时取配置 JSON_FIX_MAX_ATTEMPTS（.env 可调，默认 3）

    Returns:
        解析后的 dict

    Raises:
        ValueError: 如果 max_attempts 次修正后仍无法得到合法 JSON
    """
    import json

    if max_attempts is None:
        max_attempts = JSON_FIX_MAX_ATTEMPTS

    last = bad_output.strip()
    for attempt in range(1, max_attempts + 1):
        # 用 SystemMessage/HumanMessage 直接构造：schema_hint 常含 JSON 花括号字面量，
        # 走 ChatPromptTemplate 会二次插值报 "unmatched '{' in format spec"（项目硬约束）
        messages = [
            SystemMessage(content=f"""你是一个 JSON 格式修正器。
请将用户提供的内容转换为合法的 JSON，严格按以下规则：
- 只输出 JSON，不要添加任何解释、代码块标记或其他文字
- 确保所有引号、括号正确闭合
- 字符串用双引号，不要用单引号
- 期望的 JSON 结构: {schema_hint}"""),
            HumanMessage(content=f"需要修正的内容：\n{last}"),
        ]
        resp = await _ds_flash_llm.ainvoke(messages)

        content = extract_json_block(resp.content)
        last = content  # 本轮失败则下一轮基于本轮输出再修

        try:
            result = json.loads(content)
            logger.info(f"   ✅ Flash 第 {attempt} 次修正成功")
            return result
        except Exception as e:
            logger.warning(f"   ⚠️ Flash 第 {attempt} 次修正仍失败: {e}")

    raise ValueError(f"Flash 修正 {max_attempts} 次后仍无法得到合法 JSON")
