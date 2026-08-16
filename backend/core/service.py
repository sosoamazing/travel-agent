"""核心业务服务层：纯业务逻辑，不依赖 Streamlit。

职责：
- 封装 LangGraph 工作流（travel_graph）的执行（非流式 + 流式）
- 封装会话历史（chat_history_manager）、用户档案、MCP / Memory 的初始化
- 维护「task_id → 任务状态/进度/结果」的内存任务注册表，供 FastAPI 轮询与 SSE 流式
- 对外暴露观测（observability）查询

设计要点：
- 提交任务（submit_chat）立即返回 task_id，后台用 asyncio 任务异步执行，长任务不阻塞请求。
- 任务执行过程中，把「节点进度」与「stream_to_user 的 LLM token」写入该任务的 asyncio.Queue，
  FastAPI 侧通过 SSE 消费该队列。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

# 保证无论从哪个 cwd 启动，都能以 backend 为包根导入 config / graph / db 等
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import AIMessage, HumanMessage

from config.settings import PROJECT_ROOT, setup_logging
from graph.state import GlobalState
from graph.workflow import create_travel_planning_graph, travel_graph
from chat_history_manager import get_chat_history_manager
from memory.manager import get_memory_manager

setup_logging()

logger = logging.getLogger(__name__)

# 固定工作流中的节点名，用于把 astream_events 的节点事件映射为进度
_NODE_NAMES = {
    "classify", "conversation_reply", "handle_feedback", "information_query",
    "extract_params", "ask_clarification", "simple_rag_search", "transport_check",
    "transport_select", "budget_fail", "city_budget_allocation", "summarizer",
}

# 已完成任务的内存保留时间（秒），超过后在新建任务时惰性清理，避免内存无限增长
_TASK_TTL_SEC = 3600.0

_DEFAULT_USER_ID = "default_user"


def _json_safe(obj: Any) -> Any:
    """把任意对象转成 JSON 可序列化结构（不安全的类型用 str 兜底）。"""
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def _default_state(user_query: str, session_id: Optional[str], user_id: str) -> GlobalState:
    """构建固定工作流的初始输入状态（对齐原 app.py 的字段重置逻辑）。"""
    return {
        "user_query": user_query,
        "messages": [],
        "session_id": session_id,
        "user_id": user_id,
        "final_answer": None,
        "current_agent": None,
        "next_agent": None,
        "is_complete": False,
        "query_type": None,
        "planner_context": {},
        "cities": [],
        "current_city_index": 0,
        "current_city": None,
        "has_next_city": False,
        "total_budget": 0.0,
        "spent_budget": 0.0,
        "spent_before_city": 0.0,
        "transport_costs": {},
        "over_budget": False,
        "budget_message": None,
        "current_attractions": [],
        "current_route_plan": {},
        "current_hotels": [],
        "replan_count": 0,
        "city_plans": [],
        "tool_results": [],
        "rag_results_history": [],
    }


async def _load_history_messages(session_id: str) -> List[Any]:
    """从会话历史加载 LangChain 消息，用于多轮上下文注入。"""
    if not session_id:
        return []
    try:
        msgs = await get_chat_history_manager().get_session_messages(session_id)
    except Exception as e:
        logger.warning(f"加载会话 {session_id} 历史失败，按空历史继续: {e}")
        return []
    out: List[Any] = []
    for m in msgs:
        if m.message_type == "user":
            out.append(HumanMessage(content=m.content))
        else:
            out.append(AIMessage(content=m.content))
    return out


def _extract_final_answer(result: Dict[str, Any]) -> Optional[str]:
    """从工作流结果中提取最终回答（优先 final_answer，其次最后一条 AI 消息）。"""
    final_answer = result.get("final_answer")
    if final_answer:
        return final_answer
    if result.get("is_complete") and result.get("messages"):
        for msg in reversed(result["messages"]):
            try:
                if isinstance(msg, AIMessage) and msg.content:
                    return msg.content
            except Exception:
                continue
    return None


def _write_result_json(result: Dict[str, Any]) -> None:
    """把业务结果写入 data/result.json（与观测 observability.json 分离，兼容旧行为）。"""
    try:
        out_dir = os.path.join(str(PROJECT_ROOT), "data")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "result.json")
        payload = {
            "user_query": result.get("user_query"),
            "query_type": result.get("query_type"),
            "final_answer": result.get("final_answer"),
            "cities": result.get("cities", []),
            "city_plans": result.get("city_plans", []),
            "total_budget": result.get("total_budget"),
            "spent_budget": result.get("spent_budget"),
            "over_budget": result.get("over_budget"),
            "budget_message": result.get("budget_message"),
            "transport_costs": result.get("transport_costs", {}),
            "tool_results": result.get("tool_results", []),
            "rag_results_history": result.get("rag_results_history", []),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"📄 [结果] 业务数据已写入 {path}")
    except Exception as e:
        logger.warning(f"写入 result.json 失败（不影响主流程）: {e}")


class TaskRecord:
    """一次对话任务的运行时记录。"""

    def __init__(self, task_id: str, user_query: str, session_id: Optional[str], user_id: str):
        self.task_id = task_id
        self.user_query = user_query
        self.session_id = session_id
        self.user_id = user_id
        self.status = "pending"  # pending | running | succeeded | failed
        self.current_node: Optional[str] = None
        self.progress_message: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.obs_task_id: Optional[str] = None  # 观测系统的根 span id（与业务 task_id 解耦）
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        # SSE 事件队列：元素为 dict 事件，结束用 None 哨兵
        self.queue: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue()
        # 持有 asyncio.Task 引用，避免被 GC 中断
        self._asyncio_task: Optional[asyncio.Task] = None

    def snapshot(self) -> Dict[str, Any]:
        """转成可 JSON 序列化的任务状态视图。"""
        return {
            "task_id": self.task_id,
            "status": self.status,
            "current_node": self.current_node,
            "progress_message": self.progress_message,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class TaskManager:
    """内存任务注册表（单进程内共享，多 uvicorn worker 各自独立）。"""

    def __init__(self):
        self._tasks: Dict[str, TaskRecord] = {}
        self._lock = threading.Lock()

    def create(self, user_query: str, session_id: Optional[str], user_id: str) -> TaskRecord:
        with self._lock:
            self._prune_locked()
            task_id = uuid.uuid4().hex
            record = TaskRecord(task_id, user_query, session_id, user_id)
            self._tasks[task_id] = record
            return record

    def create_with_id(self, task_id: str, user_query: str,
                       session_id: Optional[str], user_id: str) -> TaskRecord:
        """用指定 ID 创建任务记录（断点续跑时 thread_id == task_id，需沿用原 ID）。"""
        with self._lock:
            self._prune_locked()
            if task_id in self._tasks:
                raise ValueError(f"task_id 已存在: {task_id}")
            record = TaskRecord(task_id, user_query, session_id, user_id)
            self._tasks[task_id] = record
            return record

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            return self._tasks.get(task_id)

    def has_active_for_session(self, session_id: str) -> bool:
        """该会话是否有进行中的任务（pending/running），用于拒绝同会话并发提交。"""
        with self._lock:
            return any(
                r.session_id == session_id and r.status in ("pending", "running")
                for r in self._tasks.values()
            )

    def _prune_locked(self) -> None:
        """清理已结束且过期的任务，控制内存占用。"""
        now = time.time()
        expired = [
            tid for tid, r in self._tasks.items()
            if r.finished_at is not None and (now - r.finished_at) > _TASK_TTL_SEC
        ]
        for tid in expired:
            self._tasks.pop(tid, None)


class TravelService:
    """业务服务门面：初始化外部依赖 + 执行工作流 + 会话/观测查询。"""

    def __init__(self):
        self.tasks = TaskManager()
        self.chat_manager = get_chat_history_manager()
        # 全局并发上限：同时执行的工作流任务数（超出排队等待），
        # 避免高并发下打爆 LLM rate limit / 数据库连接池 / 内存
        self._concurrency_sem = asyncio.Semaphore(int(os.getenv("TASK_CONCURRENCY", "10")))
        # 预热 memory 单例（lazy 单例，首次访问即初始化，避免首问额外延迟）
        get_memory_manager()
        self._init_mcp()
        # 默认使用无 checkpointer 的图；异步 setup() 成功后再切换到带 checkpointer 的图
        self.graph = travel_graph
        self.checkpointer = None

    @staticmethod
    def _init_mcp() -> None:
        """启动 MCP 服务器（后台线程连接，幂等，不阻塞）。"""
        try:
            from tools.mcp_tools import start_mcp_servers
            start_mcp_servers()
        except Exception as e:
            logger.warning(f"⚠️ MCP 服务器启动失败，稍后工具调用时会自动重试: {e}")

    async def setup(self) -> None:
        """异步初始化：创建 Postgres checkpointer 并用其重新编译图。

        失败时降级为无 checkpointer 图，服务仍可用（仅失去断点续跑能力）。
        """
        try:
            from core.checkpoint import create_checkpointer
            self.checkpointer = await create_checkpointer()
            self.graph = create_travel_planning_graph(checkpointer=self.checkpointer)
            logger.info("✅ [service] 已启用 LangGraph Postgres checkpointer（断点续跑）")
        except Exception as e:
            logger.warning(
                f"⚠️ [service] 初始化 checkpointer 失败，降级为无 checkpointer 模式: {e}"
            )
            self.checkpointer = None
            self.graph = travel_graph

    @staticmethod
    def _graph_config(thread_id: str) -> Dict[str, Any]:
        """构造 LangGraph 调用 config：thread_id 即 checkpoint 流主键。"""
        return {"configurable": {"thread_id": thread_id}}

    async def close(self) -> None:
        """进程退出前关闭 checkpoint 连接池（幂等）。"""
        try:
            from core.checkpoint import close_checkpointer
            await close_checkpointer()
        except Exception as e:
            logger.warning(f"关闭 checkpointer 失败: {e}")

    # ── 会话 / 消息 ────────────────────────────────────────

    async def create_session(self, user_id: Optional[str] = None, title: Optional[str] = None) -> str:
        return await self.chat_manager.create_session(user_id=user_id or _DEFAULT_USER_ID, title=title)

    async def list_sessions(self, user_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        sessions = await self.chat_manager.get_user_sessions(user_id=user_id or _DEFAULT_USER_ID, limit=limit)
        return [
            {
                "session_id": s.session_id,
                "user_id": s.user_id,
                "title": s.title,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "message_count": s.message_count,
            }
            for s in sessions
        ]

    async def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        msgs = await self.chat_manager.get_session_messages(session_id)
        return [
            {
                "role": "user" if m.message_type == "user" else "assistant",
                "content": m.content,
                "message_type": m.message_type,
                "timestamp": m.timestamp,
            }
            for m in msgs
        ]

    async def add_message(self, session_id: str, message_type: str, content: str,
                          user_id: Optional[str] = None) -> int:
        return await self.chat_manager.add_message(
            session_id=session_id,
            message_type=message_type,
            content=content,
            user_id=user_id or _DEFAULT_USER_ID,
        )

    async def delete_session(self, session_id: str) -> bool:
        """删除会话及其全部消息。"""
        try:
            await self.chat_manager.delete_session(session_id)
            return True
        except Exception as e:
            logger.warning(f"删除会话 {session_id} 失败: {e}")
            return False

    # ── 对话任务 ──────────────────────────────────────────

    async def submit_chat(self, user_query: str, session_id: Optional[str] = None,
                          user_id: Optional[str] = None) -> str:
        """提交一轮对话：持久化用户消息 → 创建任务 → 后台异步执行 → 返回 task_id。"""
        user_id = user_id or _DEFAULT_USER_ID
        if not session_id:
            session_id = await self.create_session(user_id=user_id)
        # 同会话有进行中的任务时拒绝新请求，避免历史并发写冲突（检查先于消息持久化）
        if self.tasks.has_active_for_session(session_id):
            raise RuntimeError("该会话有进行中的任务，请等待完成后再发送")
        await self.add_message(session_id=session_id, message_type="user",
                               content=user_query, user_id=user_id)

        record = self.tasks.create(user_query, session_id, user_id)
        record._asyncio_task = asyncio.create_task(self._run_task(record))
        return record.task_id

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        record = self.tasks.get(task_id)
        return record.snapshot() if record else None

    async def resume_task(self, task_id: str) -> str:
        """断点续跑：以同一 task_id（=thread_id）从最近 checkpoint 继续执行。

        首次执行用完整 input 跑图，续跑则 input=None；LangGraph 依据 checkpoint 的
        versions_seen 跳过已完成节点，仅重跑失败节点及其下游。返回原 task_id。
        """
        if self.checkpointer is None:
            raise RuntimeError("checkpointer 未启用，无法断点续跑（请先安装 langgraph-checkpoint-postgres 并启动）")

        config = self._graph_config(task_id)
        snapshot = await self.graph.aget_state(config)
        if snapshot is None or not getattr(snapshot, "values", None):
            raise RuntimeError(f"未找到 task_id={task_id} 的 checkpoint，无法续跑")

        values = snapshot.values
        session_id = values.get("session_id")
        user_id = values.get("user_id") or _DEFAULT_USER_ID
        user_query = values.get("user_query") or ""

        record = self.tasks.get(task_id)
        if record is None:
            # 进程重启后内存任务表已清空，用原 task_id 重建记录
            record = self.tasks.create_with_id(task_id, user_query, session_id, user_id)
        else:
            # 内存记录仍存在：复位运行态并重置 SSE 队列，准备重新流式
            record.user_query = user_query
            record.session_id = session_id
            record.user_id = user_id
            record.result = None
            record.error = None
            record.obs_task_id = None
            record.finished_at = None
            record.queue = asyncio.Queue()

        record._asyncio_task = asyncio.create_task(self._run_task(record, resume=True))
        return task_id

    async def _run_task(self, record: TaskRecord, resume: bool = False) -> None:
        """后台执行工作流：流式产出 token / 进度，最终写入结果并持久化 AI 回答。

        Args:
            record: 任务记录（task_id 同时作为 checkpoint 的 thread_id）。
            resume: True 表示断点续跑——以 input=None + 同一 thread_id 重新 invoke，
                LangGraph 会跳过已成功节点、从最近 checkpoint 继续。
        """
        from agent_nodes._common import _token_tracker
        from agent_nodes._observability import (
            reset_observability, start_task, end_task,
        )

        record.status = "running"
        record.started_at = time.time()
        record.progress_message = "正在从 checkpoint 续跑" if resume else "任务已开始"

        config = self._graph_config(record.task_id)

        accumulated = ""
        result: Optional[Dict[str, Any]] = None
        ok = False
        started_obs = False
        sem_acquired = False
        try:
            # 并发上限：超出后排队等待（acquire 在 try 内，被取消时不会误 release）
            await self._concurrency_sem.acquire()
            sem_acquired = True
            # 观测上下文：contextvars 保证与其它并发任务隔离
            reset_observability()
            _token_tracker.reset()
            obs_task_id = await start_task(
                user_id=record.user_id or "",
                session_id=record.session_id or "",
                user_query=record.user_query,
            )
            record.obs_task_id = obs_task_id
            started_obs = True

            if resume:
                # 断点续跑：不重新注入输入，用同一 thread_id 从最近 checkpoint 继续
                input_state = None
            else:
                input_state = _default_state(record.user_query, record.session_id, record.user_id)
                # 注入多轮历史（含刚持久化的本轮用户消息）
                input_state["messages"] = await _load_history_messages(record.session_id or "")

            async for event in self.graph.astream_events(input_state, config=config, version="v2"):
                kind = event.get("event")
                name = event.get("name")

                if kind == "on_chain_start" and name in _NODE_NAMES:
                    record.current_node = name
                    record.progress_message = f"正在执行 {name}"
                    await record.queue.put({"type": "progress", "node": name})

                elif kind == "on_chat_model_stream":
                    # 仅推送标记为 stream_to_user 的 LLM 输出到前端
                    if "stream_to_user" not in event.get("tags", []):
                        continue
                    chunk_data = event["data"]["chunk"]
                    token = chunk_data.content if hasattr(chunk_data, "content") else ""
                    if token:
                        accumulated += token
                        record.progress_message = "生成回复中"
                        # 只发增量 token，避免 SSE 全量重传的 O(n²) 浪费（前端自行拼接）
                        await record.queue.put({"type": "token", "text": token})

                elif kind == "on_chain_end" and name == "LangGraph":
                    result = event["data"]["output"]

            ok = True

            # 续跑场景：若进程重启导致 record 元信息丢失，从恢复出的 state 中补齐
            if result and resume:
                record.session_id = record.session_id or result.get("session_id")
                record.user_id = record.user_id or result.get("user_id") or _DEFAULT_USER_ID
                record.user_query = record.user_query or result.get("user_query") or ""

            final_answer = _extract_final_answer(result) if result else None
            if final_answer and result is not None:
                already_added = any(
                    isinstance(msg, AIMessage) and getattr(msg, "content", "") == final_answer
                    for msg in result.get("messages", [])
                )
                if not already_added:
                    result["messages"].append(AIMessage(content=final_answer))

            if result is not None:
                _write_result_json(result)

            serialized = _json_safe({
                "user_query": record.user_query,
                "query_type": result.get("query_type") if result else None,
                "final_answer": final_answer,
                "cities": result.get("cities", []) if result else [],
                "city_plans": result.get("city_plans", []) if result else [],
                "total_budget": result.get("total_budget") if result else None,
                "spent_budget": result.get("spent_budget") if result else None,
                "over_budget": result.get("over_budget") if result else None,
                "budget_message": result.get("budget_message") if result else None,
                "transport_costs": result.get("transport_costs", {}) if result else {},
                "tool_results": result.get("tool_results", []) if result else [],
                "rag_results_history": result.get("rag_results_history", []) if result else [],
                "planner_context": result.get("planner_context", {}) if result else {},
            })

            record.result = serialized
            record.status = "succeeded"
            await record.queue.put({
                "type": "final",
                "text": accumulated,
                "state": serialized,
            })

            if final_answer:
                await self.add_message(session_id=record.session_id or "",
                                       message_type="ai", content=final_answer, user_id=record.user_id)

        except Exception as e:
            logger.exception(f"任务 {record.task_id} 执行失败")
            record.status = "failed"
            record.error = str(e)
            if started_obs:
                await end_task("error", str(e))
            try:
                await record.queue.put({"type": "error", "text": str(e)})
            except Exception:
                pass
        finally:
            if sem_acquired:
                self._concurrency_sem.release()
            if ok and started_obs:
                await end_task("ok")
            record.finished_at = time.time()
            record.current_node = None
            record.progress_message = None
            await record.queue.put(None)  # SSE 结束哨兵

    # ── 观测 ──────────────────────────────────────────────

    async def get_obs(self, task_id: str) -> Optional[Dict[str, Any]]:
        """查询某业务 task 对应的观测追踪详情（返回 None 表示任务或观测不存在）。"""
        record = self.tasks.get(task_id)
        if record is None or not record.obs_task_id:
            return None
        from agent_nodes._observability import build_task_json
        return await build_task_json(record.obs_task_id)

    # ── 健康检查 ──────────────────────────────────────────

    async def health(self) -> Dict[str, Any]:
        from db import check_pg3_db_connectivity
        db_status = await check_pg3_db_connectivity()
        mcp_status: Dict[str, Any] = {"initialized": False, "servers": []}
        try:
            from tools.mcp_tools import start_mcp_servers, get_mcp_server_names
            mcp_status["initialized"] = bool(start_mcp_servers())
            mcp_status["servers"] = get_mcp_server_names()
        except Exception as e:
            mcp_status["error"] = str(e)
        return {
            "status": "ok" if db_status.get("ok") else "degraded",
            "db": db_status,
            "mcp": mcp_status,
            "time": time.time(),
        }


_service: Optional[TravelService] = None
_service_lock = threading.Lock()


def get_service() -> TravelService:
    """获取全局业务服务单例（进程内共享）。"""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = TravelService()
    return _service
