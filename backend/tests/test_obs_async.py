"""测试脚本：Phase 3 观测层（obs）异步化冒烟测试。

运行方式（在 backend 目录下）：
    cd travel-agent/backend
    python tests/test_obs_async.py

前置条件：PostgreSQL 已启动（docker compose up -d 起 travel-agent-pg，端口 5432）。

覆盖内容：
1. start_task 开始观测任务
2. node_scope + record_llm + record_mcp（保持同步调用）
3. end_task 落库汇总
4. build_task_json 组装并断言 summary / nodes
"""
from __future__ import annotations

import asyncio
import os
import sys
import warnings
from pathlib import Path

# 未 await 的 coroutine 直接抛错（等效命令行 -W error::RuntimeWarning）
warnings.filterwarnings("error", category=RuntimeWarning)

# Windows 中文控制台默认 GBK 编码，无法编码 emoji（✅/🚀 等）会导致 print 抛
# UnicodeEncodeError。与 server.py / test_async_db.py 处理一致，强制切到 UTF-8。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Windows 上 psycopg3 的异步实现依赖 SelectorEventLoop（需要 add_reader），而 Python
# 默认使用 ProactorEventLoop（不支持 add_reader）。与 server.py / test_async_db.py
# 顶层处理一致；必须在 asyncio.run() 之前设置。
if sys.platform == "win32":
    import selectors as _selectors

    class _SelectorLoopPolicy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self):
            return asyncio.SelectorEventLoop(_selectors.SelectSelector())

    asyncio.set_event_loop_policy(_SelectorLoopPolicy())

# 让脚本能直接 import 项目模块
_HERE = str(Path(__file__).resolve().parents[1])
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import db as db_module
from agent_nodes._observability import (
    build_task_json,
    end_task,
    node_scope,
    record_llm,
    record_mcp,
    start_task,
)


async def test_obs_full_flow() -> None:
    print("\n" + "=" * 60)
    print("📗 Test 1: 观测全链路（start_task → record → end_task → build_task_json）")
    print("=" * 60)

    # 1) 开始任务（async）
    task_id = await start_task(
        user_id="_test_obs", session_id="_test_obs_sess", user_query="测试",
    )
    print(f"  task_id={task_id}")
    assert task_id and isinstance(task_id, str)

    # 2) 节点内累加（record_llm / record_mcp / node_scope 保持同步）
    with node_scope("classify_test"):
        record_llm(
            agent="classify", model="test-model",
            input_tokens=500, output_tokens=30, duration_ms=120.5,
            status="ok", cached_input_tokens=200,
            output='{"type":"travel"}',
        )
        record_mcp(
            server="test-server", tool="gaode_poi_search",
            duration_ms=280.0, status="ok", retries=0,
            result='[{"name":"外滩","address":"黄浦区"}]',
        )

    # 3) 结束任务（async）
    await end_task(status="ok")

    # 4) 组装任务 json（async）
    task_json = await build_task_json(task_id)
    summary = task_json["summary"]
    print(f"  summary: node_count={summary['node_count']}, "
          f"llm_call_count={summary['llm_call_count']}, tool_call_count={summary['tool_call_count']}")

    assert summary["node_count"] == 1, f"node_count={summary['node_count']}"
    assert summary["llm_call_count"] == 1, f"llm_call_count={summary['llm_call_count']}"
    assert summary["tool_call_count"] == 1, f"tool_call_count={summary['tool_call_count']}"
    assert summary["total_input_tokens"] == 500
    assert summary["total_output_tokens"] == 30
    assert summary["cached_input_tokens"] == 200
    assert summary["version"], "版本号不应为空"

    nodes = task_json["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["node"] == "classify_test"
    assert nodes[0]["status"] == "ok"
    assert len(nodes[0]["llm"]) == 1
    assert nodes[0]["llm"][0]["agent"] == "classify"
    assert nodes[0]["llm"][0]["input_tokens_avg"] == 500.0
    assert len(nodes[0]["tools"]) == 1
    assert nodes[0]["tools"][0]["tool"] == "gaode_poi_search"
    print("  ✅ 观测全链路通过")


async def main() -> int:
    print("🚀 Phase 3 观测层异步化 — 冒烟测试")
    print(f"   工作目录: {os.getcwd()}")
    try:
        await test_obs_full_flow()
    finally:
        # 必须在同一事件循环内 await 关闭（跨 loop close 会 CancelledError）
        await db_module.shutdown_async_pool()
    print("\n" + "=" * 60)
    print("🎉 全部测试通过！Phase 3 观测层异步化验证成功")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
