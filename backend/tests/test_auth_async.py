"""测试脚本：Phase 4 认证层（auth.py）异步化冒烟测试。

运行方式（在 backend 目录下）：
    cd travel-agent/backend
    python tests/test_auth_async.py

前置条件：PostgreSQL 已启动（docker compose up -d 起 travel-agent-pg，端口 5432）。

覆盖内容：
1. _ensure_users_table 幂等（连调两次不报错）
2. register_user → authenticate_user 正确密码/错误密码
3. list_admins 返回 list（可能含 seed 的 superadmin）
4. 测试末尾清理测试用户
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
from auth import (
    _ensure_users_table,
    authenticate_user,
    list_admins,
    register_user,
)


async def test_auth_flow() -> str:
    print("\n" + "=" * 60)
    print("📗 Test 1: 认证层异步化（建表 → 注册 → 登录 → 管理员列表）")
    print("=" * 60)

    # 1) 建表幂等：连续调用两次不报错
    await _ensure_users_table()
    await _ensure_users_table()
    print("  ✅ _ensure_users_table 幂等通过")

    # 2) 注册唯一测试用户
    username = f"_test_auth_{int(time.time() * 1000)}"
    password = "test_pass_123"
    created = await register_user(username, password)
    assert created["username"] == username
    assert created["role"] == "user"
    assert created["id"] > 0
    print(f"  注册成功: {username} (id={created['id']})")

    # 3) 正确密码登录成功
    user = await authenticate_user(username, password)
    assert user is not None, "正确密码应登录成功"
    assert user["username"] == username
    assert user["role"] == "user"
    print(f"  登录成功: role={user['role']}")

    # 4) 错误密码登录失败
    bad = await authenticate_user(username, "wrong_password")
    assert bad is None, "错误密码应返回 None"
    print("  错误密码返回 None ✅")

    # 5) 管理员列表
    admins = await list_admins()
    assert isinstance(admins, list), f"list_admins 应返回 list，实际 {type(admins)}"
    # 可能为空（尚未 seed superadmin 时），但必须是 list
    print(f"  管理员数量: {len(admins)}")
    if admins:
        print(f"  管理员: {[a['username'] for a in admins][:5]}")
    print("  ✅ 认证流程通过")

    return username


async def main() -> int:
    print("🚀 Phase 4 认证层异步化 — 冒烟测试")
    print(f"   工作目录: {os.getcwd()}")
    username = ""
    try:
        username = await test_auth_flow()
    finally:
        # 清理测试用户
        if username:
            try:
                async with db_module.async_db_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("DELETE FROM users WHERE username = %s", (username,))
                print(f"  🧹 已清理测试用户: {username}")
            except Exception as e:
                print(f"  ⚠️ 清理测试用户失败: {e}")
        # 必须在同一事件循环内 await 关闭（跨 loop close 会 CancelledError）
        await db_module.shutdown_async_pool()
    print("\n" + "=" * 60)
    print("🎉 全部测试通过！Phase 4 认证层异步化验证成功")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
