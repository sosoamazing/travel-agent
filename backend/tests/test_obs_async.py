"""测试脚本：span 树观测层异步化冒烟测试。

运行方式（在 backend 目录下）：
    cd travel-agent/backend
    python tests/test_obs_async.py

前置条件：PostgreSQL 已启动（docker compose up -d 起 travel-agent-pg，端口 5432）。

覆盖内容：
1. start_task 前置插入 obs_tasks（传入业务 task_id）
2. node/llm/mcp span 开始占位 + 结束更新
3. end_task 更新任务状态
4. build_task_json 组装并断言 summary / nodes
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
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
    start_llm,
    start_mcp,
    start_node,
    end_llm,
    end_mcp,
    end_node,
    start_task,
)


async def test_obs_full_flow() -> None:
    print("\n" + "=" * 60)
    print("📗 Test 1: span 树观测全链路（start_task → spans → end_task → build_task_json）")
    print("=" * 60)

    # 1) 开始任务（传入业务 task_id）
    task_id = uuid.uuid4().hex
    await start_task(task_id, user_id="_test_obs", session_id="_test_obs_sess", user_query="测试")
    print(f"  task_id={task_id}")

    # 2) node/llm/mcp span：开始占位 + 结束更新
    #    node span（通过 node_scope async 上下文）
    async with node_scope("classify_test"):
        # llm span
        llm_span = await start_llm(agent="classify")
        await end_llm(llm_span, status="ok",
                      input_tokens=500, output_tokens=30, cached_tokens=200,
                      output='{"type":"travel"}')
        # mcp span
        mcp_span = await start_mcp(server="test-server", tool="gaode_poi_search")
        await end_mcp(mcp_span, status="ok", retries=0)

    # 额外：手动 node span 成对接口
    nspan = await start_node("extra_node")
    await end_node(nspan, status="ok")

    # 3) 结束任务
    await end_task(status="ok")

    # 4) 组装任务 json
    task_json = await build_task_json(task_id)
    summary = task_json["summary"]
    print(f"  summary: node_count={summary['node_count']}, "
          f"llm_call_count={summary['llm_call_count']}, tool_call_count={summary['tool_call_count']}")

    assert summary["node_count"] == 2, f"node_count={summary['node_count']}"
    assert summary["llm_call_count"] == 1, f"llm_call_count={summary['llm_call_count']}"
    assert summary["tool_call_count"] == 1, f"tool_call_count={summary['tool_call_count']}"
    assert summary["total_input_tokens"] == 500
    assert summary["total_output_tokens"] == 30
    assert summary["cached_input_tokens"] == 200

    nodes = task_json["nodes"]
    by_name = {n["node"]: n for n in nodes}
    assert "classify_test" in by_name, f"nodes={list(by_name.keys())}"
    assert by_name["classify_test"]["status"] == "ok"
    assert len(by_name["classify_test"]["llm"]) == 1
    assert by_name["classify_test"]["llm"][0]["agent"] == "classify"
    assert by_name["classify_test"]["llm"][0]["input_tokens"] == 500
    assert len(by_name["classify_test"]["tools"]) == 1
    assert by_name["classify_test"]["tools"][0]["tool"] == "gaode_poi_search"
    assert "extra_node" in by_name
    print("  ✅ 观测全链路通过")


async def main() -> int:
    print("🚀 span 树观测层异步化 — 冒烟测试")
    print(f"   工作目录: {os.getcwd()}")
    try:
        await test_obs_full_flow()
    finally:
        # 必须在同一事件循环内 await 关闭（跨 loop close 会 CancelledError）
        await db_module.shutdown_async_pool()
    print("\n" + "=" * 60)
    print("🎉 全部测试通过！span 树观测层验证成功")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
