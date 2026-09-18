"""共享基础工具：LLM 构造、流式回复、统一 MCP 工具调用入口。

从原 workflow_nodes.py 拆分而来，供其余节点模块复用。
"""
from typing import Dict, Any, List, Optional
from datetime import datetime
import asyncio
import json
import logging
import time

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI

from config.settings import (
    QWEN3_MODEL, QWEN3_TEMPERATURE,
    OPENAI_API_KEY, OPENAI_BASE_URL, DS_FLASH_MODEL, DS_FLASH_TEMPERATURE,
    LLM_TIMEOUT_SEC, LLM_STREAM_TIMEOUT_SEC, LLM_PAYLOAD_MAX_CHARS,
    get_agent_model,
)
from tools.registry import get_tool_by_name
from ._observability import start_llm, end_llm

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# 基础工具
# ──────────────────────────────────────────────────────────

_WEEKDAYS_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def _now_str() -> str:
    """返回当前时间字符串，格式：YYYY-MM-DD HH:MM 星期X

    供各 agent 提示词注入，让 LLM 感知当前时点（用于判断淡旺季、节假日、早晚语境等）。
    """
    now = datetime.now()
    return f"{now.strftime('%Y-%m-%d %H:%M')} {_WEEKDAYS_CN[now.weekday()]}"


def _time_anchors(now=None) -> str:
    """目标时间计算器：把相对时间（今天/明天/后天/下周X/下个月/节假日）解析成具体日期锚点。

    用于注入「参数提取」等需要解析日期的提示词，避免 LLM 自行推算日期出错。
    返回多行文本，例如：
        当前时间：2026-08-15 周六
        今天=2026-08-15(周六)
        明天=2026-08-16(周日)
        下周一=2026-08-17(周一)
        下个月=2026-09-15(周二)
        国庆=2026-10-01(周四)
    """
    import calendar
    from datetime import datetime, timedelta
    now = now or datetime.now()
    today = now.date()
    weekday = today.weekday()  # 0=周一 ... 6=周日
    CN = "一二三四五六日"

    def fmt(d):
        return f"{d.strftime('%Y-%m-%d')}(周{CN[d.weekday()]})"

    lines = [
        f"当前时间：{now.strftime('%Y-%m-%d')} 周{CN[weekday]}",
        f"今天={fmt(today)}",
        f"明天={fmt(today + timedelta(days=1))}",
        f"后天={fmt(today + timedelta(days=2))}",
        f"大后天={fmt(today + timedelta(days=3))}",
    ]
    # 下周一 ~ 下周日（严格「下周」的周一为起点；今天周一则下周一是 7 天后）
    next_monday = today + timedelta(days=(7 - weekday))
    for i in range(7):
        lines.append(f"下周{CN[i]}={fmt(next_monday + timedelta(days=i))}")
    # 下个月同一天（处理月末溢出）
    def add_months(d, n):
        m = d.month - 1 + n
        y = d.year + m // 12
        m = m % 12 + 1
        day = min(d.day, calendar.monthrange(y, m)[1])
        return d.replace(year=y, month=m, day=day)
    lines.append(f"下个月={fmt(add_months(today, 1))}")
    # 固定节假日（今年已过则取明年）
    def next_holiday(month, day, name):
        d = today.replace(month=month, day=day)
        if d < today:
            d = d.replace(year=today.year + 1)
        return f"{name}={fmt(d)}"
    lines.append(next_holiday(10, 1, "国庆"))
    lines.append(next_holiday(1, 1, "元旦"))
    lines.append(next_holiday(5, 1, "五一"))
    return "\n".join(lines)


def _extract_usage(msg) -> tuple:
    """从 AIMessage/AIMessageChunk 提取 (input_tokens, output_tokens, cached_input_tokens)。

    前两者优先 LangChain 标准 usage_metadata，其次 response_metadata.token_usage / usage。
    cached_input_tokens：服务端上下文缓存命中的输入 token 数（未返回缓存信息时为 0）：
    - DeepSeek:        usage.prompt_cache_hit_tokens
    - OpenAI/Qwen 兼容: usage.input_tokens_details.cached_tokens / prompt_tokens_details.cached_tokens
    """
    try:
        um = getattr(msg, "usage_metadata", None) or {}
        inp = um.get("input_tokens", 0) or 0
        out = um.get("output_tokens", 0) or 0
        # 缓存命中：优先 LangChain usage_metadata 的 input_token_details.cache_read
        # （流式路径 response_metadata 为空，缓存信息只在这里；DeepSeek 的
        #   prompt_tokens_details.cached_tokens 会被 LangChain 映射为 cache_read）
        cached = (um.get("input_token_details") or {}).get("cache_read")
        if cached is None:
            cached = (um.get("prompt_token_details") or {}).get("cached_tokens")
        # 其次从 response_metadata 的原始 usage 提取（非流式路径）
        rm = getattr(msg, "response_metadata", None) or {}
        usage = rm.get("token_usage") or rm.get("usage") or {}
        if cached is None:
            cached = usage.get("prompt_cache_hit_tokens")
        if cached is None:
            details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
            cached = details.get("cached_tokens")
        if inp or out:
            return inp, out, int(cached or 0)
        inp = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
        out = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
        return inp, out, int(cached or 0)
    except Exception:
        return 0, 0, 0


def _extract_model(msg, fallback: str) -> str:
    """从 AIMessage 提取模型名。"""
    try:
        rm = getattr(msg, "response_metadata", None) or {}
        return rm.get("model_name") or rm.get("model") or fallback
    except Exception:
        return fallback


def _serialize_llm_input(messages) -> tuple:
    """把送进模型的 messages 序列化成可落库文本；超长截断。

    返回 (text, truncated)。失败时退回 str(messages)。
    """
    try:
        if isinstance(messages, (list, tuple)):
            parts = []
            for m in messages:
                role = getattr(m, "type", None) or getattr(m, "role", None) or m.__class__.__name__
                content = getattr(m, "content", m)
                parts.append({"role": str(role), "content": content})
            text = json.dumps(parts, ensure_ascii=False, default=str)
        else:
            text = json.dumps(messages, ensure_ascii=False, default=str)
    except Exception:
        text = str(messages)
    truncated = len(text) > LLM_PAYLOAD_MAX_CHARS
    if truncated:
        text = text[:LLM_PAYLOAD_MAX_CHARS]
    return text, truncated


def _make_tracked_llm(kwargs: Dict[str, Any], agent: str = "unknown",
                      parse_json: bool = False,
                      prompt_id: Optional[str] = None,
                      prompt_version: Optional[str] = None) -> ChatOpenAI:
    """构造带 token 追踪的 ChatOpenAI：包装 ainvoke/astream 自动提取 token usage 并记录 agent 归属。"""
    base = ChatOpenAI(**kwargs)

    # 保存原始 ainvoke
    _orig_ainvoke = base.ainvoke
    _orig_astream = base.astream

    async def _finish_llm(span_id: str, result: str, *, error_what: Optional[str] = None,
                          input_tokens: int = 0, output_tokens: int = 0,
                          cached_tokens: int = 0, output: str = "",
                          messages=None) -> None:
        input_text = None
        truncated = False
        if messages is not None:
            input_text, truncated = _serialize_llm_input(messages)
        await end_llm(
            span_id, result=result, error_what=error_what,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cached_tokens=cached_tokens, output=output,
            prompt_id=prompt_id, prompt_version=prompt_version,
            input_text=input_text, input_truncated=truncated,
        )

    async def _tracked_ainvoke(input, config=None, **kw):
        t0 = time.perf_counter()
        span_id = await start_llm(agent)
        try:
            msg = await asyncio.wait_for(
                _orig_ainvoke(input, config=config, **kw),
                timeout=LLM_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            await _finish_llm(span_id, "timeout",
                              error_what=f"timeout: {LLM_TIMEOUT_SEC:g}s",
                              messages=input)
            raise
        except Exception as e:
            await _finish_llm(span_id, "error", error_what=str(e), messages=input)
            raise
        inp, out, cached = _extract_usage(msg)
        model = _extract_model(msg, kwargs.get("model", "unknown"))
        content = getattr(msg, "content", "") or ""
        if not (inp or out):
            # DeepSeek 等非流式接口可能不返回 usage，按字符估算兜底
            if content:
                out = max(1, int(len(str(content)) / 2.5))

        # parse_json: 自动解析 JSON，失败用 flash 矫正（矫正发生在主 LLM span 还在栈上时，
        # 矫正的 ainvoke 会以主 LLM span 为 parent，成为其子 span）。
        if parse_json:
            try:
                from tools.json_utils import extract_json_block, fix_json_with_flash
                cleaned = extract_json_block(content)
                parsed = json.loads(cleaned)
                parsed_obj = _AIMessageFromDict(parsed, model=model)
                await _finish_llm(span_id, "ok",
                                  input_tokens=inp, output_tokens=out, cached_tokens=cached,
                                  output=str(content), messages=input)
                return parsed_obj
            except Exception as _jerr:
                try:
                    corrected = await fix_json_with_flash(content)
                    await _finish_llm(span_id, "ok",
                                      input_tokens=inp, output_tokens=out, cached_tokens=cached,
                                      output=str(content), messages=input)
                    return _AIMessageFromDict(corrected, model=model)
                except Exception as _cfail:
                    await _finish_llm(span_id, "parse_error", error_what=str(_cfail),
                                      messages=input)
                    raise ValueError(f"{agent} JSON 解析+矫正均失败: {_cfail}") from _cfail

        await _finish_llm(span_id, "ok",
                          input_tokens=inp, output_tokens=out, cached_tokens=cached,
                          output=str(content), messages=input)
        return msg

    async def _tracked_astream(input, config=None, **kw):
        """astream 逐 chunk 追 usage（若有流式 usage chunk），否则按字符估算兜底。

        注意：astream_events(v2) 驱动下，模型内部即使代码调 ainvoke 也会走
        stream=True 的 HTTP 流，所以必须开启 stream_usage 让服务端返回 usage chunk。
        流式调用只在开始时占位，流式过程中不写库，结束才 end_llm 补全。
        """
        span_id = await start_llm(agent)
        full = None
        all_text = ""
        best_usage = None  # 流式过程中任一分片携带的 usage (input, output, cached)
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(LLM_STREAM_TIMEOUT_SEC):
                async for chunk in _orig_astream(input, config=config, **kw):
                    full = chunk
                    if hasattr(chunk, "content") and chunk.content:
                        all_text += chunk.content
                    inp, out, cached = _extract_usage(chunk)
                    if inp or out:
                        best_usage = (inp, out, cached)
                    yield chunk
        except asyncio.TimeoutError:
            await _finish_llm(span_id, "timeout",
                              error_what=f"timeout: {LLM_STREAM_TIMEOUT_SEC:g}s",
                              messages=input)
            raise
        except Exception as e:
            await _finish_llm(span_id, "error", error_what=str(e), messages=input)
            raise
        if full:
            if best_usage:
                inp, out, cached = best_usage
            else:
                inp, out, cached = _extract_usage(full)
            model = _extract_model(full, kwargs.get("model", "unknown"))
            if not (inp or out):
                # 流式仍拿不到 usage 时，按字符估算（中英文混合 ~2.5 字/token）
                out = max(1, int(len(all_text) / 2.5))
            await _finish_llm(span_id, "ok",
                              input_tokens=inp, output_tokens=out, cached_tokens=cached,
                              output=all_text, messages=input)

    object.__setattr__(base, "ainvoke", _tracked_ainvoke)
    object.__setattr__(base, "astream", _tracked_astream)
    return base


class _LLM:
    """统一 LLM 客户端（中间类）：封装模型选择 + 调用接口 + 观测记录 + 内容返回。

    使用类型在构造时通过 agent（用途）+ model_type（pro/flash）指定，观测指标据此落库。

    用法：
        llm = _LLM(agent="attractions", temperature=0.3)                  # 复杂任务，pro 模型
        llm = _LLM(agent="hotel_price", model_type="flash")               # 简单任务，flash 模型
        llm = _LLM(agent="summarizer", model_type="flash", streaming=True)  # 流式（自动打 stream_to_user tag）
        data = await llm.ainvoke([...])
        data = await llm.with_structured_output(Schema).ainvoke([...])
        async for chunk in llm.astream([...]): ...
    """
    def __init__(self, agent: str = "unknown", model_type: str = "pro",
                 temperature: Optional[float] = None, streaming: bool = False,
                 tags: Optional[List[str]] = None,
                 max_tokens: Optional[int] = None,
                 extra_body: Optional[Dict[str, Any]] = None,
                 parse_json: bool = False,
                 prompt_id: Optional[str] = None,
                 prompt_version: Optional[str] = None):
        """统一 LLM 客户端。

        Args:
            agent: 用途名（观测/模型覆盖用）。
            model_type: 'pro' | 'flash'。
            parse_json: True 时，ainvoke 返回后自动 `extract_json_block` + `json.loads`，
                失败用 flash 矫正（矫正成为主 LLM 的子 span）。成功返回 dict，多次矫正失败抛 ValueError。
            prompt_id / prompt_version: 可选模板目录指针，写入 obs_llm_spans。
        """
        self.agent = agent
        self.model_type = model_type
        self.parse_json = parse_json
        self.prompt_id = prompt_id
        self.prompt_version = prompt_version
        if model_type == "flash":
            model = get_agent_model(agent, DS_FLASH_MODEL)
            temp = temperature if temperature is not None else DS_FLASH_TEMPERATURE
        else:
            model = get_agent_model(agent, QWEN3_MODEL)
            temp = temperature if temperature is not None else QWEN3_TEMPERATURE
        kwargs: Dict[str, Any] = dict(
            model=model,
            base_url=OPENAI_BASE_URL,
            api_key=OPENAI_API_KEY,
            temperature=temp,
            stream_usage=True,
        )
        # 可选：输出 token 上限（DeepSeek 推理模型 max_tokens 含思维链 token）
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        # 可选：非标准 [OI] 参数（如 DeepSeek 的 thinking 开关）必须走 extra_body，
        # 直接放 kwargs/model_kwargs 会被 openai SDK 当未知顶层参数抛 TypeError。
        if extra_body:
            kwargs["extra_body"] = extra_body
        if streaming:
            kwargs["streaming"] = True
            kwargs["tags"] = tags or ["stream_to_user"]
        # 底层 Chat[OI] 的 ainvoke/astream 已被 _make_tracked_llm 包装（token 统计 + 观测落库）
        self._llm = _make_tracked_llm(
            kwargs, agent=agent, parse_json=parse_json,
            prompt_id=prompt_id, prompt_version=prompt_version,
        )

    def ainvoke(self, messages, **kw):
        return self._llm.ainvoke(messages, **kw)

    def astream(self, messages, **kw):
        return self._llm.astream(messages, **kw)

    def with_structured_output(self, schema, **kw):
        # DeepSeek V4 不支持 json_schema 结构化输出（response_format 仅 text/json_object），
        # 统一改用 function_calling（v4-pro/flash 非思考模式均支持，schema 严格匹配）。
        # 使用方须确保对应 agent 走非思考模式（thinking disabled），
        # 避免思考模式下强制 tool_choice 不稳定 + reasoning_content 回传 400 问题。
        kw.setdefault("method", "function_calling")
        return self._llm.with_structured_output(schema, **kw)

    def bind_tools(self, tools, **kw):
        # 供 info_query 等 bind_tools 选工具场景使用（委托给已包装的底层 ChatOpenAI）
        return self._llm.bind_tools(tools, **kw)


async def _stream_reply(system_prompt: str, user_text: str, agent: str = "unknown",
                       prompt_id: Optional[str] = None,
                       prompt_version: Optional[str] = None) -> str:
    """流式生成给用户的回复，返回完整文本（同时被 astream_events 捕获）"""
    llm = _LLM(agent=agent, streaming=True, prompt_id=prompt_id, prompt_version=prompt_version)
    text = ""
    async for chunk in llm.astream([SystemMessage(content=system_prompt), HumanMessage(content=user_text)]):
        text += chunk.content
    return text


async def _call_mcp_tool(tool_name: str, **params) -> str:
    """统一工具调用入口：本地工具走 handler，MCP 工具走 manager.call_tool"""
    tool_def = get_tool_by_name(tool_name)
    if tool_def is None:
        return f"Unknown tool: {tool_name}"
    try:
        # 本地工具：直接调用 handler
        if tool_def.handler is not None:
            return await tool_def.handler(**params)
        # MCP 工具：通过 manager 调用远端服务器
        if tool_def.server_name and tool_def.mcp_tool_name:
            from tools.mcp_tools import get_mcp_manager
            mgr = await get_mcp_manager()
            return await mgr.call_tool(tool_def.server_name, tool_def.mcp_tool_name, **params)
        return f"Tool {tool_name} has no handler or MCP mapping"
    except Exception as e:
        logger.warning(f"工具调用失败 {tool_name}: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


class _AIMessageFromDict:
    """parse_json=True 时，把解析出的 dict 包装成轻量对象。

    - .content: 原始 dict 的 JSON 字符串（调用方可 json.loads 继续用）
    - .get(key): 直接取字段（更便捷）
    - .data: 原始 dict
    - .model: 记录模型名（供观测反查）
    """
    def __init__(self, data: dict, model: str = ""):
        self.data = data
        self.model = model
        self.content = json.dumps(data, ensure_ascii=False)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]

    def __contains__(self, key):
        return key in self.data

    def __str__(self):
        return self.content
