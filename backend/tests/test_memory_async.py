"""测试脚本：Phase 1 记忆层异步化冒烟测试。

运行方式（在 backend 目录下）：
    cd travel-agent/backend
    python tests/test_memory_async.py

前置条件：PostgreSQL 已启动（docker compose up -d 起 travel-agent-pg，端口 5432）。

覆盖内容：
1. WorkingMemorySnapshot 保存/加载 round-trip（含 WorkingCityState）
2. TripEpisode 保存 → search_episodes（按 destination / 按 user_id 兜底）
3. clear_snapshot 后 load_snapshot 返回 None
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
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
from memory import episodic, working
from memory.base import CITY_DONE, TripEpisode, WorkingCityState, WorkingMemorySnapshot
from memory.storage import get_memory_storage


async def test_1_working_memory_roundtrip(session_id: str, user_id: str) -> None:
    print("\n" + "=" * 60)
    print("📗 Test 1: WorkingMemorySnapshot 保存/加载 round-trip")
    print("=" * 60)
    ms = get_memory_storage()

    snap = WorkingMemorySnapshot(
        session_id=session_id,
        user_id=user_id,
        total_budget=5000.0,
        transport_total=800.0,
        buffer_budget=200.0,
        transport_costs={"北京->上海": 553.0, "上海->苏州": 120.0},
        cities={
            "上海": WorkingCityState(city="上海", status=CITY_DONE, spent=1500.0, locked=True),
            "苏州": WorkingCityState(city="苏州", status="pending", spent=0.0, locked=False),
        },
        ttl_seconds=3600,
    )
    await ms.save_snapshot(snap)

    loaded = await ms.load_snapshot(session_id, user_id)
    assert loaded is not None, "load_snapshot 返回 None"
    assert loaded.session_id == session_id
    assert loaded.user_id == user_id
    assert abs(loaded.total_budget - 5000.0) < 1e-6
    assert abs(loaded.locked_spent - 1500.0) < 1e-6
    assert set(loaded.cities.keys()) == {"上海", "苏州"}
    assert loaded.cities["上海"].status == CITY_DONE
    assert loaded.cities["上海"].locked is True
    assert abs(loaded.cities["上海"].spent - 1500.0) < 1e-6
    assert abs(loaded.transport_costs["北京->上海"] - 553.0) < 1e-6
    print("  ✅ 工作记忆 round-trip 通过")


async def test_2_episode_save_search(user_id: str) -> None:
    print("\n" + "=" * 60)
    print("📗 Test 2: TripEpisode 保存 → search_episodes（destination / user_id 兜底）")
    print("=" * 60)

    ep = TripEpisode(
        user_id=user_id,
        session_id="_test_mem_ep_sess",
        origin="北京",
        destination="杭州",
        start_date="2026-09-01",
        end_date="2026-09-03",
        nights=2,
        total_budget=3000.0,
        total_spent=2800.0,
        transport_mode="高铁",
        transport_cost=800.0,
        hotels=[{"name": "西湖边酒店", "price_per_night": 600, "area": "西湖"}],
        attractions=[{"name": "西湖", "city": "杭州"}, {"name": "灵隐寺", "city": "杭州"}],
        summary="杭州 2 晚行程",
    )
    ep_id = await episodic.save_episode(ep)
    assert isinstance(ep_id, int) and ep_id > 0, f"save_episode 返回异常: {ep_id!r}"

    # 按 destination 检索（episodic 层会走目的地优先分支）
    by_dest = await episodic.search_episodes(user_id=user_id, destination="杭州", limit=5)
    assert any(r["id"] == ep_id for r in by_dest), "按 destination 未搜到"

    # 按 user_id 兜底检索（无过滤条件分支）
    by_user = await episodic.search_episodes(user_id=user_id, limit=5)
    assert any(r["id"] == ep_id for r in by_user), "按 user_id 兜底未搜到"
    print(f"  ✅ episode 保存/检索通过 (id={ep_id})")


async def test_3_clear_snapshot(session_id: str, user_id: str) -> None:
    print("\n" + "=" * 60)
    print("📗 Test 3: clear_snapshot 后 load_snapshot 返回 None")
    print("=" * 60)
    ms = get_memory_storage()

    await working.clear_snapshot(session_id, user_id)
    loaded = await ms.load_snapshot(session_id, user_id)
    assert loaded is None, f"clear_snapshot 后 load_snapshot 应返回 None，实际: {loaded!r}"
    print("  ✅ clear_snapshot 通过")


async def main() -> int:
    print("🚀 Phase 1 记忆层异步化 — 冒烟测试")
    print(f"   工作目录: {os.getcwd()}")
    user_id = "_test_mem_user"
    session_id = f"_test_mem_{os.getpid()}_{int(time.time() * 1000)}"
    try:
        await test_1_working_memory_roundtrip(session_id, user_id)
        await test_2_episode_save_search(user_id)
        await test_3_clear_snapshot(session_id, user_id)
    finally:
        # 必须在同一事件循环内 await 关闭（跨 loop close 会 CancelledError）
        await db_module.shutdown_async_pool()
    print("\n" + "=" * 60)
    print("🎉 全部测试通过！Phase 1 记忆层异步化验证成功")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
