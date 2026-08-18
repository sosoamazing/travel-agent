"""
MCP工具封装
- 令牌桶（aiolimiter）限速，每个 MCP 工具（服务器.工具）独立限速
- 限速参数通过 config.settings 的 MCP_RATE_LIMIT_MAX / MCP_RATE_LIMIT_WINDOW 配置，
  也可用环境变量 MCP_RATE_LIMIT_MAX / MCP_RATE_LIMIT_WINDOW 覆盖
- 默认每 0.4 秒最多 1 次（滑动窗口，留余量应对网络抖动与时钟偏差）
"""
from typing import Optional, Dict, Any, List
from contextlib import AsyncExitStack
import json
import os
import traceback
import sys
import warnings
import asyncio
import threading
import time
import logging

from aiolimiter import AsyncLimiter

# 配置日志来静默MCP客户端的警告
logging.getLogger('mcp').setLevel(logging.ERROR)
# streamable_http 在连接中断时会打 ERROR 级 "Error parsing JSON response" 全栈，
# sse 长连接空闲被服务端断开时会打 ERROR 级 "Error in sse_reader" 全栈（httpx.ReadError），
# 这两类错误都已在 _execute_with_retries 中按连接中断类关键词重试，故提到 CRITICAL 避免噪声；
# 其他 mcp 子模块的真实 ERROR 仍会显示。
logging.getLogger('mcp.client.streamable_http').setLevel(logging.CRITICAL)
logging.getLogger('mcp.client.sse').setLevel(logging.CRITICAL)
logging.getLogger('anyio').setLevel(logging.ERROR)
logging.getLogger('asyncio').setLevel(logging.ERROR)

logger = logging.getLogger(__name__)

# suppress async generator warnings and MCP client cleanup errors
warnings.filterwarnings('ignore', category=RuntimeWarning, message='.*async_generator.*')
warnings.filterwarnings('ignore', category=RuntimeWarning, message=".*generator didn't stop.*")
warnings.filterwarnings('ignore', category=RuntimeWarning, message='.*unhandled errors in a TaskGroup.*')
warnings.filterwarnings('ignore', category=RuntimeWarning, message='.*Attempted to exit cancel scope.*')
warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.filterwarnings('ignore', category=UserWarning)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import (
    PROJECT_ROOT,
    MCP_CONFIG_PATH,
    MCP_RATE_LIMIT_MAX,
    MCP_RATE_LIMIT_WINDOW,
    MCP_TIMEOUT_SEC,
)

# 尝试导入agents.mcp，如果失败提供详细错误
try:
    from agents.mcp import MCPServerSse, MCPServerStreamableHttp
except ImportError as e:
    logger.error(f"❌ 导入agents.mcp失败: {e}")
    logger.info(f"🔍 Python解释器: {sys.executable}")
    logger.info(f"🔍 sys.path前5项:")
    for i, p in enumerate(sys.path[:5]):
        logger.info(f"  {i+1}. {p}")

    # 尝试查找agents包是否存在
    try:
        import agents
        logger.info(f"✅ agents包找到: {agents.__file__}")
        logger.error(f"❌ 但agents.mcp模块不存在")
    except ImportError:
        logger.error(f"❌ agents包未安装")

    raise ImportError(
        f"\n\nopenai-agents包未正确安装或agents.mcp模块不可用\n"
        f"Python: {sys.executable}\n"
        f"请运行: pip install openai-agents"
    ) from e

# 绕过代理直连ModelScope（避免VPN代理导致SSL问题）
os.environ['NO_PROXY'] = os.environ.get('NO_PROXY', '') + ',modelscope.net,api-inference.modelscope.net'


class MCPToolManager:
    """MCP工具管理器 - 管理所有MCP服务器连接及速率限制"""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or MCP_CONFIG_PATH
        self.mcp_servers = {}
        self.exit_stack = None
        self._limiters: Dict[str, AsyncLimiter] = {}

    async def initialize(self):
        """初始化所有MCP服务器连接"""
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"MCP配置文件不存在: {self.config_path}")

        with open(self.config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        self.exit_stack = AsyncExitStack()

        for server_conf in config.get("mcp_servers", []):
            name = server_conf.get("name")
            url = server_conf.get("url")

            if not url:
                logger.warning(f"⚠️ 警告: 服务器 {name} 缺少URL，跳过")
                continue

            # 跳过占位符 URL（未配置的服务器）
            if "your-" in url or "placeholder" in url.lower():
                logger.info(f"ℹ️ 跳过未配置的服务器 [{name}] (占位符 URL): {url}")
                continue

            # 按 URL 后缀自动选择协议客户端：
            #   /sse 结尾 → 旧版 SSE 协议（MCPServerSse）
            #   /mcp 结尾（或其他）→ 新版 Streamable HTTP 协议（MCPServerStreamableHttp）
            url_lower = url.lower()
            if url_lower.endswith("/sse"):
                client_cls = MCPServerSse
                proto = "SSE"
                server_kwargs = {"name": name, "params": {"url": url}}
            else:
                client_cls = MCPServerStreamableHttp
                proto = "StreamableHTTP"
                # Streamable HTTP 默认 5s 超时太短，ModelScope 偶尔慢，拉到 30s
                server_kwargs = {
                    "name": name,
                    "params": {"url": url, "timeout": 30.0, "sse_read_timeout": 60.0},
                    "client_session_timeout_seconds": 30,
                }

            try:
                server = await self.exit_stack.enter_async_context(
                    client_cls(**server_kwargs)
                )
                self.mcp_servers[name] = server
                logger.info(
                    f"✅ MCP已连接: {name} ({url}) "
                    f"[{proto}, 限速: {MCP_RATE_LIMIT_MAX}次/{MCP_RATE_LIMIT_WINDOW}秒]"
                )
            except Exception as e:
                # 重试最多 3 次，间隔递增（2s / 4s / 6s）
                connected = False
                for attempt in range(1, 4):
                    wait = attempt * 2
                    logger.warning(
                        f"⚠️ MCP连接失败 [{name}] (第{attempt}次): {e}，{wait}s 后重试..."
                    )
                    await asyncio.sleep(wait)
                    try:
                        server = await self.exit_stack.enter_async_context(
                            client_cls(**server_kwargs)
                        )
                        self.mcp_servers[name] = server
                        logger.info(
                            f"✅ MCP已连接(重试第{attempt}次): {name} ({url}) "
                            f"[{proto}, 限速: {MCP_RATE_LIMIT_MAX}次/{MCP_RATE_LIMIT_WINDOW}秒]"
                        )
                        connected = True
                        break
                    except Exception as e2:
                        e = e2
                        logger.debug(f"重试详细错误:\n{traceback.format_exc()}")

                if not connected:
                    logger.error(f"❌ MCP连接失败 [{name}] ({proto})，已重试 3 次仍失败: {e}")

    async def call_tool(self, server_name: str, tool_name: str, max_retries: int = 2, **kwargs) -> str:
        """调用MCP工具（令牌桶限速，参数来自配置 MCP_RATE_LIMIT_MAX / MCP_RATE_LIMIT_WINDOW）。

        所有调用统一桥接到 MCP 专用事件循环执行，避免因调用方处于不同线程/事件循环
        导致连接与事件循环错绑（不同事件循环无法共享 async 对象）。

        Args:
            server_name: MCP服务器名称
            tool_name: 工具名称
            max_retries: 最大重试次数（默认2次）
            **kwargs: 工具参数
        """
        from agent_nodes._observability import start_mcp, end_mcp
        stats: Dict[str, Any] = {"retries": 0}
        span_id = await start_mcp(server_name, tool_name)
        t0 = time.perf_counter()
        try:
            if _mcp_loop is not None and asyncio.get_running_loop() is not _mcp_loop:
                fut = asyncio.run_coroutine_threadsafe(
                    self._call_tool_on_mcp_loop(server_name, tool_name, max_retries, stats, **kwargs),
                    _mcp_loop,
                )
                result = await asyncio.wait_for(
                    asyncio.wrap_future(fut), timeout=MCP_TIMEOUT_SEC
                )
            else:
                result = await asyncio.wait_for(
                    self._call_tool_on_mcp_loop(server_name, tool_name, max_retries, stats, **kwargs),
                    timeout=MCP_TIMEOUT_SEC,
                )
        except asyncio.TimeoutError:
            await end_mcp(span_id, result="timeout", error_what="mcp timeout",
                          retries=stats.get("retries", 0))
            raise
        except Exception as e:
            await end_mcp(span_id, result="error", error_what=str(e),
                          retries=stats.get("retries", 0))
            raise
        await end_mcp(span_id, result="ok", retries=stats.get("retries", 0))
        return result

    async def _call_tool_on_mcp_loop(self, server_name: str, tool_name: str, max_retries: int = 2,
                                     stats: Optional[Dict[str, Any]] = None, **kwargs) -> str:
        """call_tool 在 MCP 专用循环上的实际实现（含限速与重试）。"""
        if server_name not in self.mcp_servers:
            logger.warning(f"  📡 MCP 调用 [{server_name}] {tool_name} — 服务器未连接")
            return json.dumps({
                "error": f"MCP服务器 {server_name} 未连接",
                "available_servers": list(self.mcp_servers.keys())
            }, ensure_ascii=False)

        # 每个「服务器.工具」独立一个令牌桶（懒加载，首次调用该工具时创建）
        tool_key = f"{server_name}.{tool_name}"
        limiter = self._limiters.get(tool_key)
        if limiter is None:
            limiter = AsyncLimiter(MCP_RATE_LIMIT_MAX, MCP_RATE_LIMIT_WINDOW)
            self._limiters[tool_key] = limiter

        # 统一日志：记录所有 MCP 工具调用的服务器名和工具名
        param_summary = ", ".join(f"{k}={v}" for k, v in kwargs.items())
        if len(param_summary) > 120:
            param_summary = param_summary[:120] + "..."
        logger.info(f"  📡 MCP 调用 [{server_name}] {tool_name} ({param_summary})")

        async with limiter:
            return await self._execute_with_retries(server_name, tool_name, max_retries, stats=stats, **kwargs)

    async def _execute_with_retries(self, server_name: str, tool_name: str,
                                     max_retries: int, stats: Optional[Dict[str, Any]] = None, **kwargs) -> str:
        """执行 MCP 调用（含重试和结果解析）"""
        server = self.mcp_servers[server_name]
        last_error = None
        retries_used = 0

        for attempt in range(max_retries + 1):
            retries_used = attempt
            try:
                if attempt > 0:
                    logger.info(f"  🔄 第{attempt}次重试 {server_name}.{tool_name}...")
                    await asyncio.sleep(1 * attempt)

                result = await server.call_tool(tool_name, arguments=kwargs)

                if stats is not None:
                    stats["retries"] = retries_used

                # 处理 MCP 返回的 CallToolResult 对象
                if hasattr(result, 'content'):
                    content = result.content
                    if isinstance(content, list) and len(content) > 0:
                        if hasattr(content[0], 'text'):
                            return content[0].text
                        else:
                            return str(content[0])
                    elif isinstance(content, str):
                        return content
                    else:
                        return json.dumps(content, ensure_ascii=False, indent=2)
                else:
                    return json.dumps(result, ensure_ascii=False, indent=2, default=str)

            except Exception as e:
                last_error = e
                error_str = str(e).lower()

                is_retryable = any([
                    "peer closed connection" in error_str,
                    "incomplete chunked read" in error_str,
                    "remoteprotocolerror" in error_str,
                    "readerror" in error_str,
                    "error reading" in error_str,
                    "timeout" in error_str,
                    "connection reset" in error_str
                ])

                if is_retryable and attempt < max_retries:
                    logger.warning(f"  ⚠️ [MCP错误] {server_name}.{tool_name} - {type(e).__name__}")
                    logger.warning(f"     原因: SSE连接中断，将重试...")
                    continue
                else:
                    break

        if stats is not None:
            stats["retries"] = retries_used
        error_msg = f"工具调用失败: {str(last_error)}"
        return json.dumps({
            "error": error_msg,
            "server": server_name,
            "tool": tool_name,
            "error_type": type(last_error).__name__,
            "retries": max_retries
        }, ensure_ascii=False)

    async def list_tools(self, server_name: str) -> List[str]:
        """列出指定服务器的可用工具（桥接到 MCP 专用循环执行）。"""
        if _mcp_loop is not None and asyncio.get_running_loop() is not _mcp_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._list_tools_on_mcp_loop(server_name), _mcp_loop
            )
            # 异步等待，避免 fut.result() 同步阻塞事件循环
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=MCP_TIMEOUT_SEC)
        return await self._list_tools_on_mcp_loop(server_name)

    async def _list_tools_on_mcp_loop(self, server_name: str) -> List[str]:
        """list_tools 在 MCP 专用循环上的实际实现。"""
        if server_name not in self.mcp_servers:
            return []

        try:
            tools = await self.mcp_servers[server_name].list_tools()
            tool_names = []
            for tool in tools:
                if hasattr(tool, 'name'):
                    tool_names.append(tool.name)
                elif hasattr(tool, 'function') and hasattr(tool.function, 'name'):
                    tool_names.append(tool.function.name)
                elif isinstance(tool, dict) and 'name' in tool:
                    tool_names.append(tool['name'])
                else:
                    tool_names.append(str(tool))
            return tool_names
        except Exception as e:
            logger.error(f"获取工具列表失败: {e}")
            return []

    async def cleanup(self):
        """清理资源（关闭 MCP 连接），桥接到 MCP 专用循环执行。"""
        if _mcp_loop is not None and asyncio.get_running_loop() is not _mcp_loop:
            fut = asyncio.run_coroutine_threadsafe(self._cleanup_on_mcp_loop(), _mcp_loop)
            try:
                # 异步等待，避免 fut.result() 同步阻塞事件循环
                await asyncio.wait_for(asyncio.wrap_future(fut), timeout=MCP_TIMEOUT_SEC)
            except Exception:
                pass
            return
        await self._cleanup_on_mcp_loop()

    async def _cleanup_on_mcp_loop(self):
        """cleanup 在 MCP 专用循环上的实际实现。"""
        if self.exit_stack:
            try:
                await self.exit_stack.aclose()
            except Exception:
                pass


# ──────────────────────────────────────────────────────────
# 全局 MCP 管理：专用事件循环 + 后台线程（程序启动时连接）
# ──────────────────────────────────────────────────────────
_mcp_manager: Optional[MCPToolManager] = None
_mcp_loop: Optional[asyncio.AbstractEventLoop] = None
_mcp_thread: Optional[threading.Thread] = None
_mcp_start_lock = threading.Lock()
_mcp_init_future = None            # concurrent.futures.Future：标记初始化完成/失败
_mcp_last_init_ts = 0.0            # 上次发起初始化的时间戳（失败后冷却重试）
_MCP_INIT_COOLDOWN = 60.0          # 初始化失败后的重试冷却时间（秒）


def _handle_mcp_loop_exception(loop, context):
    """MCP 专用循环的异常处理器：静默已知的无害 MCP 清理异常。"""
    exception = context.get('exception')
    if exception:
        error_str = str(exception)
        if any(keyword in error_str.lower() for keyword in [
            'async_generator', "generator didn't stop", 'taskgroup',
            'cancel scope', 'sse_client', 'peer closed connection',
        ]):
            return
    loop.default_exception_handler(context)


def _ensure_mcp_loop_thread():
    """确保 MCP 专用事件循环与后台线程已启动（幂等）。"""
    global _mcp_loop, _mcp_thread
    with _mcp_start_lock:
        if _mcp_thread is not None and _mcp_thread.is_alive():
            return _mcp_loop
        _mcp_loop = asyncio.new_event_loop()
        _mcp_loop.set_exception_handler(_handle_mcp_loop_exception)

        def _run_loop_forever():
            asyncio.set_event_loop(_mcp_loop)
            _mcp_loop.run_forever()

        _mcp_thread = threading.Thread(
            target=_run_loop_forever, name="mcp-loop", daemon=True
        )
        _mcp_thread.start()
        logger.info("🧵 MCP 专用事件循环线程已启动")
        return _mcp_loop


def _on_mcp_init_done(fut):
    """MCP 初始化完成回调：打印最终连接状态。"""
    try:
        fut.result()
        names = list(_mcp_manager.mcp_servers.keys())
        if names:
            logger.info(f"✅ MCP 服务器已在程序启动时连接完成: {names}")
        else:
            logger.warning("⚠️ MCP 服务器启动时连接失败（当前 0 个服务器可用）")
    except Exception as e:
        logger.error(f"❌ MCP 服务器启动时初始化失败: {e}")


def start_mcp_servers() -> bool:
    """程序启动时调用：在后台线程中初始化所有 MCP 服务器连接，不阻塞 UI。

    - 幂等：可安全多次调用（Streamlit 每次 rerun 都会执行一次）
    - 连接在后台进行，状态见日志；返回 True 表示已连接成功
    """
    global _mcp_manager, _mcp_init_future, _mcp_last_init_ts
    loop = _ensure_mcp_loop_thread()
    with _mcp_start_lock:
        if _mcp_manager is None:
            _mcp_manager = MCPToolManager()
        # 已连接成功 → 无需重复初始化
        if _mcp_manager.mcp_servers:
            return True
        # 正在初始化 → 等它完成即可
        if _mcp_init_future is not None and not _mcp_init_future.done():
            return False
        # 上次初始化失败 → 冷却期内不反复重试
        now = time.monotonic()
        if _mcp_init_future is not None and (now - _mcp_last_init_ts) < _MCP_INIT_COOLDOWN:
            return False
        _mcp_last_init_ts = now
        _mcp_init_future = asyncio.run_coroutine_threadsafe(
            _mcp_manager.initialize(), loop
        )
        _mcp_init_future.add_done_callback(_on_mcp_init_done)
        logger.info("🚀 MCP 服务器初始化已在后台启动（程序启动时）")
        return False


def get_mcp_server_names() -> List[str]:
    """同步获取当前已连接成功的 MCP 服务器名称列表（供 /health 等使用）。

    不触发初始化；若管理器尚未创建或连接失败，返回空列表。
    """
    global _mcp_manager
    if _mcp_manager is None:
        return []
    try:
        return list(_mcp_manager.mcp_servers.keys())
    except Exception:
        return []


async def get_mcp_manager() -> MCPToolManager:
    """获取全局 MCP 管理器实例。

    若尚未启动则自动启动；若正在初始化则等待其完成后再返回。
    """
    global _mcp_manager
    start_mcp_servers()
    if _mcp_init_future is not None and not _mcp_init_future.done():
        try:
            await asyncio.wrap_future(_mcp_init_future)
        except Exception as e:
            logger.warning(f"⚠️ MCP 初始化未成功完成: {e}")
    return _mcp_manager
