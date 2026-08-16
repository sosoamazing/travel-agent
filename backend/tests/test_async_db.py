"""测试脚本：Phase 0 psycopg3 异步连接池基建验证。

运行方式（在 backend 目录下）：
    cd travel-agent/backend
    python tests/test_async_db.py

前置条件：PostgreSQL 已启动（docker compose up -d 起 travel-agent-pg，端口 5432）。

覆盖内容：
1. check_pg3_db_connectivity() 探活（SELECT 1 + dict_row）
2. 20 协程 × 5 次并发，验证 dict_row 的 row["col"] 访问 + pg_sleep 真实并发
3. 异常自动回滚，且不影响后续正常使用
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import warnings
from pathlib import Path
from typing import Any, Dict, cast

# 未 await 的 coroutine 直接抛错（等效命令行 -W error::RuntimeWarning）
warnings.filterwarnings("error", category=RuntimeWarning)

# Windows 中文控制台默认 GBK 编码，无法编码 emoji（✅/🚀 等）会导致 print 抛
# UnicodeEncodeError。与 server.py 处理一致，把 stdout/stderr 强制切到 UTF-8。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Windows 上 psycopg3 的异步实现依赖 SelectorEventLoop（需要 add_reader），而 Python
# 默认使用 ProactorEventLoop（不支持 add_reader），会报 "Psycopg cannot use
# ProactorEventLoop"。与 server.py 顶层处理一致；必须在 asyncio.run() 之前设置。
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
from db import async_db_connection, check_pg3_db_connectivity


async def test_1_probe() -> None:
    print("\n" + "=" * 60)
    print("📗 Test 1: psycopg3 探活 check_pg3_db_connectivity()")
    print("=" * 60)
    probe = await check_pg3_db_connectivity(timeout_sec=5.0)
    print(f"  探活结果: ok={probe['ok']}, latency_ms={probe.get('latency_ms')}, error={probe.get('error')}")
    if not probe["ok"]:
        raise RuntimeError(f"探活失败: {probe.get('error')}")
    print("  ✅ Test 1 通过")


async def test_2_concurrency() -> None:
    print("\n" + "=" * 60)
    print('📗 Test 2: 20 协程 × 5 次并发（dict_row row["col"] 访问）')
    print("=" * 60)
    N_TASKS = 20
    ITER_PER_TASK = 5

    async def worker(tid: int) -> None:
        for i in range(ITER_PER_TASK):
            async with async_db_connection() as conn:
                cur = await conn.execute(
                    "SELECT %s AS tid, %s AS iter, pg_sleep(0.02)",
                    (tid, i),
                )
                row = cast(Dict[str, Any], await cur.fetchone())
                assert row["tid"] == tid, f"row['tid']={row['tid']!r} != {tid}"
                assert row["iter"] == i, f"row['iter']={row['iter']!r} != {i}"

    t0 = time.perf_counter()
    await asyncio.gather(*(worker(t) for t in range(N_TASKS)))
    cost_ms = (time.perf_counter() - t0) * 1000
    print(
        f"  耗时: {cost_ms:.0f}ms，协程数: {N_TASKS}，每协程迭代: {ITER_PER_TASK}"
        f"（共 {N_TASKS * ITER_PER_TASK} 次真实往返）"
    )
    print("  ✅ Test 2 通过")


async def test_3_rollback() -> None:
    print("\n" + "=" * 60)
    print("📗 Test 3: 异常自动回滚 + 后续正常使用不受影响")
    print("=" * 60)
    # 故意抛异常：async_db_connection 应自动 rollback 并归还连接（不泄漏 aborted 连接）
    try:
        async with async_db_connection() as conn:
            await conn.execute("SELECT 1")
            raise RuntimeError("故意抛出的异常（测试回滚）")
    except RuntimeError as e:
        print(f"  捕获预期异常: {e}")
    # 回滚后连接应可继续正常使用（池内无 aborted 连接残留）
    async with async_db_connection() as conn:
        cur = await conn.execute("SELECT 1 AS ping")
        row = cast(Dict[str, Any], await cur.fetchone())
        assert row["ping"] == 1, f"row['ping']={row['ping']!r}"
    print("  ✅ Test 3 通过")


async def main() -> int:
    print("🚀 Phase 0 psycopg3 异步连接池 — 自动化测试")
    print(f"   工作目录: {os.getcwd()}")
    try:
        await test_1_probe()
        await test_2_concurrency()
        await test_3_rollback()
    finally:
        # 必须在同一事件循环内 await 关闭（跨 loop close 会 CancelledError）
        await db_module.shutdown_async_pool()
    print("\n" + "=" * 60)
    print("🎉 全部测试通过！psycopg3 异步连接池基建验证成功")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
