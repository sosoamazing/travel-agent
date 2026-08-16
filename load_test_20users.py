"""N 用户并发压力测试脚本。

场景：默认 30 个测试用户，每个用户提交 1 个旅行问题（同时并发），走 gateway 完整链路
（注册/登录 → JWT → 建会话 → 提交 /chat → 轮询任务结果），验证：
  - 并发信号量（TASK_CONCURRENCY）下任务能否全部正常完成
  - 同会话并发拦截（每个用户独立会话，不应触发 409）
  - 每个任务的端到端耗时

前置条件：
  1) backend 已启动：   uvicorn server:app --host 0.0.0.0 --port 8001 --app-dir .
  2) gateway 已启动：   uvicorn gateway.main:app --host 0.0.0.0 --port 8000
  3) DB / MCP 正常（health 接口 status=ok）

运行：
  python load_test_20users.py [gateway_url] [--from-db N]

参数：
  --from-db N   从数据库 test_questions 表随机取 N 个问题（默认 N=30），
                否则使用脚本内置的 QUESTIONS 列表

输出：每个用户的任务结果 + 汇总统计（成功/失败/耗时分布）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any, Dict, List, Optional

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import httpx

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
PASSWORD = "Test@12345"
POLL_INTERVAL = 2.0
TASK_TIMEOUT = 600.0  # 单任务最长等待（秒）

# 20 个测试问题：每个用户一个，覆盖 planning / information / conversation 类型
QUESTIONS: List[str] = [
    "我想12月去杭州玩3天，从上海出发，预算3000，帮我规划一下",
    "帮我规划春节从广州去三亚5天4晚的行程，预算5000",
    "北京到西安的高铁二等座票价是多少",
    "成都市区有什么必打卡的景点",
    "推荐杭州西湖边性价比高的酒店",
    "我想6月去青岛玩4天，从济南出发，预算2000",
    "重庆到成都怎么去最划算",
    "西安有什么美食推荐",
    "帮我规划上海周末2日游，预算1000",
    "昆明到丽江的火车票多少钱",
    "三亚亲子游适合住哪个区域",
    "我想国庆去南京玩3天，预算2500，从武汉出发",
    "故宫门票需要提前预约吗",
    "帮我规划厦门鼓浪屿一日游",
    "哈尔滨冬天穿什么合适",
    "我想下个月去拉萨，有什么注意事项",
    "苏州园林哪个最值得去",
    "帮我规划天津到北京的1日游，预算800",
    "桂林阳朔怎么玩比较好",
    "帮我安排一个周末的放松行程",
]


async def register_or_login(client: httpx.AsyncClient, username: str) -> str:
    """注册（已存在则忽略）→ 登录，返回 JWT。"""
    r = await client.post("/auth/register", json={"username": username, "password": PASSWORD})
    if r.status_code not in (200, 201, 409):
        raise RuntimeError(f"register {username} → HTTP {r.status_code}: {r.text[:200]}")
    r = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
    if r.status_code != 200:
        raise RuntimeError(f"login {username} → HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["access_token"]


async def run_one_user(
    client: httpx.AsyncClient,
    idx: int,
    question: str,
    intent: Optional[str] = None,
) -> Dict[str, Any]:
    """一个测试用户跑完整链路：建会话 → 提交问题 → 轮询到结束。"""
    username = f"test_user_{idx:02d}"
    t_start = time.perf_counter()
    try:
        token = await register_or_login(client, username)
        headers = {"Authorization": f"Bearer {token}"}

        r = await client.post("/sessions", headers=headers, json={})
        if r.status_code >= 400:
            return {"user": username, "status": "session_fail", "error": r.text[:150], "cost": 0}
        session_id = r.json()["session_id"]

        body = {"user_query": question, "session_id": session_id}
        if intent:
            body["intent"] = intent
        r = await client.post("/chat", headers=headers, json=body)
        if r.status_code == 409:
            return {"user": username, "status": "conflict_409", "error": r.text[:150], "cost": 0}
        if r.status_code >= 400:
            return {"user": username, "status": "chat_fail", "error": r.text[:150], "cost": 0}
        task_id = r.json()["task_id"]

        # 轮询任务结果
        deadline = time.time() + TASK_TIMEOUT
        while time.time() < deadline:
            r = await client.get(f"/tasks/{task_id}", headers=headers)
            if r.status_code != 200:
                return {"user": username, "status": "poll_fail", "error": r.text[:150], "cost": 0}
            snap = r.json()
            st = snap.get("status")
            if st == "succeeded":
                result = snap.get("result") or {}
                ans = (result.get("final_answer") or "")[:80]
                return {
                    "user": username,
                    "status": "ok",
                    "qtype": result.get("query_type"),
                    "answer": ans,
                    "cost": time.perf_counter() - t_start,
                }
            if st == "failed":
                return {
                    "user": username,
                    "status": "failed",
                    "error": snap.get("error"),
                    "cost": time.perf_counter() - t_start,
                }
            await asyncio.sleep(POLL_INTERVAL)
        return {"user": username, "status": "timeout", "cost": TASK_TIMEOUT}
    except Exception as e:
        return {"user": username, "status": "exception", "error": str(e)[:200],
                "cost": time.perf_counter() - t_start}


def load_questions_from_db(n: int = 30) -> List[Dict[str, str]]:
    """从数据库 test_questions 表随机取 n 个 (question, intent) 对。"""
    import psycopg2
    from dotenv import load_dotenv
    from pathlib import Path

    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=True)
    conn = psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", "5432")),
        user=os.getenv("PG_USER", "travel_agent"),
        password=os.getenv("PG_PASSWORD", "travel_agent"),
        dbname=os.getenv("PG_DATABASE", "travel_agent"),
    )
    cur = conn.cursor()
    cur.execute("SELECT question, COALESCE(intent, '') FROM test_questions ORDER BY random() LIMIT %s", (n,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"question": r[0], "intent": r[1]} for r in rows]


async def main() -> None:
    # ── 问题来源：--from-db N 从数据库随机取，否则用内置列表 ──
    questions = [{"question": q, "intent": ""} for q in QUESTIONS]
    if "--from-db" in sys.argv:
        idx = sys.argv.index("--from-db")
        n = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 30
        try:
            questions = load_questions_from_db(n)
        except Exception as e:
            print(f"⚠️ 从数据库取题失败（{e}），退回内置问题列表")

    print(f"🎯 目标: {BASE_URL} | 用户数={len(questions)} | 并发提交\n")
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        # 健康检查
        try:
            h = await client.get("/health")
            print(f"health: HTTP {h.status_code} {h.text[:120]}\n")
        except Exception as e:
            print(f"⚠️ health 检查失败（确认 gateway 已启动）: {e}\n")

        t0 = time.perf_counter()
        results = await asyncio.gather(
            *[run_one_user(client, i + 1, q["question"], q.get("intent") or "")
              for i, q in enumerate(questions)]
        )
        total_cost = time.perf_counter() - t0

    # ── 汇总 ──
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    print("=" * 70)
    print(f"总计: {len(results)} 个任务 | 并发墙钟耗时 {total_cost:.1f}s")
    print(f"✅ 成功: {len(ok)} | ❌ 失败: {len(failed)}")
    if ok:
        costs = sorted(r["cost"] for r in ok)
        print(f"   成功任务耗时: 最小 {costs[0]:.1f}s / 中位 {costs[len(costs)//2]:.1f}s / 最大 {costs[-1]:.1f}s")
    print("-" * 70)
    for r in sorted(results, key=lambda x: x["user"]):
        if r["status"] == "ok":
            print(f"  ✅ {r['user']} ({r.get('qtype')}) {r['cost']:.1f}s → {r['answer'][:40]}")
        else:
            print(f"  ❌ {r['user']} [{r['status']}] {(r.get('error') or '')[:100]}")
    print("=" * 70)
    if failed:
        print("有失败任务，请检查 backend 日志与 MCP/DB 状态。")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
