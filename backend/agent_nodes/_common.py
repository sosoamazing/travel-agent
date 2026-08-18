"""共享基础工具：LLM 构造、流式回复、统一 MCP 工具调用入口。

从原 workflow_nodes.py 拆分而来，供其余节点模块复用。
"""
from typing import Dict, Any, List, Optional
from datetime import datetime
from collections import defaultdict
import asyncio
import json
import logging
import threading
import time

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI

from config.settings import (
    QWEN3_MODEL, QWEN3_TEMPERATURE,
    OPENAI_API_KEY, OPENAI_BASE_URL, DS_FLASH_MODEL, DS_FLASH_TEMPERATURE,
    LLM_TIMEOUT_SEC, LLM_STREAM_TIMEOUT_SEC,
    get_agent_model,
)
from tools.registry import get_tool_by_name
from ._observability import start_llm, end_llm

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# Token 使用统计（全局单例，按模型累积）
# ──────────────────────────────────────────────────────────

class TokenUsageTracker:
    """按模型 + 按 agent 双层累积 token 用量，线程安全。

    维度 1：model → 汇总（input/output/total/call_count）
    维度 2：agent → model → 汇总（用于打印"哪个 agent 消耗了多少"）
    """

    def __init__(self):
        self._data: Dict[str, Dict[str, int]] = defaultdict(lambda: {
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cached_tokens": 0, "call_count": 0,
        })
        # agent -> model -> 统计
        self._agent_data: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: {
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cached_tokens": 0, "call_count": 0,
        }))
        self._lock = threading.Lock()

    def add(self, model: str, input_tokens: int, output_tokens: int, agent: str = "unknown",
            cached_input_tokens: int = 0):
        with self._lock:
            # 模型维度
            d = self._data[model]
            d["input_tokens"] += input_tokens
            d["output_tokens"] += output_tokens
            d["total_tokens"] += input_tokens + output_tokens
            d["cached_tokens"] += cached_input_tokens
            d["call_count"] += 1
            # agent 维度
            ad = self._agent_data[agent][model]
            ad["input_tokens"] += input_tokens
            ad["output_tokens"] += output_tokens
            ad["total_tokens"] += input_tokens + output_tokens
            ad["cached_tokens"] += cached_input_tokens
            ad["call_count"] += 1

    def summary(self) -> str:
        if not self._data:
            return "  (无 LLM 调用记录)"
        lines = []
        total_all = 0
        lines.append("  ── 按模型 ──")
        for model, d in self._data.items():
            rate = ""
            if d["cached_tokens"]:
                rate = f"，缓存命中 {d['cached_tokens'] * 100 / max(d['input_tokens'], 1):.1f}%"
            lines.append(
                f"    {model}: {d['call_count']}次, "
                f"输入 {d['input_tokens']} + 输出 {d['output_tokens']} = {d['total_tokens']} tokens{rate}"
            )
            total_all += d["total_tokens"]
        lines.append(f"  ── 按模型合计: {total_all} tokens")
        cached_total = sum(d["cached_tokens"] for d in self._data.values())
        input_total = sum(d["input_tokens"] for d in self._data.values())
        if input_total:
            lines.append(
                f"  ── 缓存命中率: 命中 {cached_total} / 输入 {input_total} "
                f"= {cached_total * 100 / input_total:.1f}%"
            )
        lines.append("  ── 按 Agent ──")
        for agent in sorted(self._agent_data):
            a_in = sum(v["input_tokens"] for v in self._agent_data[agent].values())
            a_out = sum(v["output_tokens"] for v in self._agent_data[agent].values())
            a_count = sum(v["call_count"] for v in self._agent_data[agent].values())
            model_detail = ", ".join(
                f"{m} {v['call_count']}次" for m, v in self._agent_data[agent].items()
            )
            lines.append(
                f"    {agent}: {a_count}次, "
                f"输入 {a_in} + 输出 {a_out} = {a_in + a_out} tokens  [{model_detail}]"
            )
        return "\n".join(lines)

    def reset(self):
        with self._lock:
            self._data.clear()
            self._agent_data.clear()


_token_tracker = TokenUsageTracker()


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


def _make_tracked_llm(kwargs: Dict[str, Any], agent: str = "unknown") -> ChatOpenAI:
    """构造带 token 追踪的 ChatOpenAI：包装 ainvoke/astream 自动提取 token usage 并记录 agent 归属。"""
    base = ChatOpenAI(**kwargs)

    # 保存原始 ainvoke
    _orig_ainvoke = base.ainvoke
    _orig_astream = base.astream

    async def _tracked_ainvoke(input, config=None, **kw):
        t0 = time.perf_counter()
        span_id = await start_llm(agent)
        try:
            msg = await asyncio.wait_for(
                _orig_ainvoke(input, config=config, **kw),
                timeout=LLM_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            await end_llm(span_id, status="error",
                          error=f"timeout: {LLM_TIMEOUT_SEC:g}s")
            raise
        except Exception as e:
            await end_llm(span_id, status="error", error=str(e))
            raise
        inp, out, cached = _extract_usage(msg)
        model = _extract_model(msg, kwargs.get("model", "unknown"))
        content = getattr(msg, "content", "") or ""
        if not (inp or out):
            # DeepSeek 等非流式接口可能不返回 usage，按字符估算兜底
            if content:
                out = max(1, int(len(str(content)) / 2.5))
        if inp or out:
            _token_tracker.add(model, inp, out, agent=agent, cached_input_tokens=cached)
        await end_llm(span_id, status="ok",
                      input_tokens=inp, output_tokens=out, cached_tokens=cached,
                      output=str(content))
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
            await end_llm(span_id, status="error",
                          error=f"timeout: {LLM_STREAM_TIMEOUT_SEC:g}s")
            raise
        except Exception as e:
            await end_llm(span_id, status="error", error=str(e))
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
            _token_tracker.add(model, inp, out, agent=agent, cached_input_tokens=cached)
            await end_llm(span_id, status="ok",
                          input_tokens=inp, output_tokens=out, cached_tokens=cached,
                          output=all_text)

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
                 extra_body: Optional[Dict[str, Any]] = None):
        self.agent = agent
        self.model_type = model_type
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
        # 可选：非标准 OpenAI 参数（如 DeepSeek 的 thinking 开关）必须走 extra_body，
        # 直接放 kwargs/model_kwargs 会被 openai SDK 当未知顶层参数抛 TypeError。
        if extra_body:
            kwargs["extra_body"] = extra_body
        if streaming:
            kwargs["streaming"] = True
            kwargs["tags"] = tags or ["stream_to_user"]
        # 底层 ChatOpenAI 的 ainvoke/astream 已被 _make_tracked_llm 包装（token 统计 + 观测落库）
        self._llm = _make_tracked_llm(kwargs, agent=agent)

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


async def _stream_reply(system_prompt: str, user_text: str, agent: str = "unknown") -> str:
    """流式生成给用户的回复，返回完整文本（同时被 astream_events 捕获）"""
    llm = _LLM(agent=agent, streaming=True)
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
