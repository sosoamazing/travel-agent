"""测试脚本：Phase 2 会话层异步化冒烟测试。

运行方式（在 backend 目录下）：
    cd travel-agent/backend
    python tests/test_chat_async.py

前置条件：PostgreSQL 已启动（docker compose up -d 起 travel-agent-pg，端口 5432）。

覆盖内容：
1. create_session 创建会话
2. add_message 写 2 条消息（user + ai），get_session_messages 断言 2 条
3. get_user_sessions 断言 message_count 正确 + update_session_title + get_last_session
4. delete_session 后 get_user_sessions 不再包含该会话
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
from chat_history_manager import get_chat_history_manager


async def test_session_lifecycle() -> None:
    print("\n" + "=" * 60)
    print("📗 Test 1: 会话生命周期（create → add_message×2 → 查询 → 删除）")
    print("=" * 60)
    cm = get_chat_history_manager()
    user_id = "_test_chat_user"

    # 1) 创建会话
    sess_id = await cm.create_session(user_id=user_id, title="[异步化测试] 上海行程")
    print(f"  创建会话: {sess_id}")
    assert sess_id and isinstance(sess_id, str)

    # 2) 写入两条消息（参数用关键字，避免位置参数顺序错误）
    mid_user = await cm.add_message(
        session_id=sess_id, message_type="user",
        content="帮我规划上海 3 天行程，预算 5000", user_id=user_id,
    )
    mid_ai = await cm.add_message(
        session_id=sess_id, message_type="ai",
        content="好的，为您规划上海 3 天 5000 元行程方案...", user_id=user_id,
    )
    assert isinstance(mid_user, int) and mid_user > 0
    assert isinstance(mid_ai, int) and mid_ai > mid_user
    print(f"  消息写入: user id={mid_user}, ai id={mid_ai}")

    # 3) 读取消息，断言 2 条且顺序正确
    msgs = await cm.get_session_messages(sess_id)
    assert len(msgs) == 2, f"期望 2 条消息，实际 {len(msgs)}"
    assert msgs[0].message_type == "user"
    assert msgs[1].message_type == "ai"
    print(f"  消息读取: {len(msgs)} 条")

    # 4) get_user_sessions 校验 message_count
    sessions = await cm.get_user_sessions(user_id=user_id)
    s = next((x for x in sessions if x.session_id == sess_id), None)
    assert s is not None, "会话未出现在用户会话列表中"
    assert s.message_count == 2, f"message_count 应为 2，实际 {s.message_count}"
    print(f"  message_count = {s.message_count}")

    # 5) update_session_title
    await cm.update_session_title(sess_id, "[异步化测试] 已完成")
    s2 = next((x for x in await cm.get_user_sessions(user_id=user_id) if x.session_id == sess_id), None)
    assert s2 is not None and s2.title == "[异步化测试] 已完成", f"标题未更新: {s2.title if s2 else None}"

    # 6) get_last_session（当前最新会话即本会话）
    last = await cm.get_last_session(user_id=user_id)
    assert last is not None and last.session_id == sess_id

    # 7) 删除会话，断言不再出现
    await cm.delete_session(sess_id)
    sessions_after = await cm.get_user_sessions(user_id=user_id)
    assert not any(x.session_id == sess_id for x in sessions_after), "删除后会话仍存在"
    print("  ✅ 删除后会话已移除")


async def main() -> int:
    print("🚀 Phase 2 会话层异步化 — 冒烟测试")
    print(f"   工作目录: {os.getcwd()}")
    try:
        await test_session_lifecycle()
    finally:
        # 必须在同一事件循环内 await 关闭（跨 loop close 会 CancelledError）
        await db_module.shutdown_async_pool()
    print("\n" + "=" * 60)
    print("🎉 全部测试通过！Phase 2 会话层异步化验证成功")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
