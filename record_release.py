"""独立程序：给管理员记一笔 agent_releases（不参与 agent 取词）。

快照当时每个 (agent, usage) 的最新 prompt version + 当前 git。
agent 干活仍只查 prompt_versions 最新行。

用法（在 travel-agent/travel-agent 目录下）：
    python record_release.py --note "调整 classify 分类规则"
    python record_release.py --version 42-abc1234 --note "发版"
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    import selectors as _selectors

    class _SelectorLoopPolicy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self):
            return asyncio.SelectorEventLoop(_selectors.SelectSelector())

    asyncio.set_event_loop_policy(_SelectorLoopPolicy())

_ROOT = Path(__file__).resolve().parent
_BACKEND = _ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


async def _run(version: str, note: str, git_commit: str) -> str:
    from agent_nodes._obs_storage import get_obs_storage
    from config.prompt_registry import latest_prompt_set
    from config.settings import AGENT_VERSION
    from db import async_db_connection
    from psycopg.types.json import Json
    import db as db_module
    import time

    await get_obs_storage()._ensure_init()
    ver = (version or "").strip() or AGENT_VERSION
    prompt_set = await latest_prompt_set()
    prev = None
    async with async_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT version FROM agent_releases ORDER BY created_at DESC LIMIT 1"
            )
            row = await cur.fetchone()
            if row:
                prev = row["version"]
            await cur.execute(
                """INSERT INTO agent_releases
                   (version, prev_version, git_commit, note, prompt_set, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (version) DO UPDATE SET
                     prev_version = EXCLUDED.prev_version,
                     git_commit = EXCLUDED.git_commit,
                     note = EXCLUDED.note,
                     prompt_set = EXCLUDED.prompt_set,
                     created_at = EXCLUDED.created_at""",
                (ver, prev, git_commit or AGENT_VERSION, note, Json(prompt_set), time.time()),
            )
    await db_module.shutdown_async_pool()
    return ver


def main() -> int:
    parser = argparse.ArgumentParser(description="记录一条 agent_releases")
    parser.add_argument("--version", default="", help="发布号，默认当前 AGENT_VERSION")
    parser.add_argument("--note", default="", help="这次改了什么")
    parser.add_argument("--git-commit", default="", help="覆盖 git 记录，默认 AGENT_VERSION")
    args = parser.parse_args()

    ver = asyncio.run(_run(args.version, args.note, args.git_commit))
    print(f"已记录发布 {ver}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
