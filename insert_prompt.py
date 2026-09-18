"""独立程序：向 prompt_versions 插入一条模板。agent 下次调用该槽位即用最新行。

用法（在 travel-agent/travel-agent 目录下）：
    python insert_prompt.py classify system path/to/classify.txt
    python insert_prompt.py transport choose_mode --stdin
    type prompt.txt | python insert_prompt.py hotel_select choose --stdin

不经过 Redis，不写 agent_releases。要给管理员留一笔发布说明，另跑 record_release.py。
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


async def _run(agent: str, usage: str, content: str) -> str:
    from agent_nodes._obs_storage import get_obs_storage
    from config.prompt_registry import content_hash, insert_prompt
    import db as db_module

    await get_obs_storage()._ensure_init()
    ver = await insert_prompt(agent, usage, content, version=content_hash(content))
    await db_module.shutdown_async_pool()
    return ver


def main() -> int:
    parser = argparse.ArgumentParser(description="插入一条 prompt_versions 记录")
    parser.add_argument("agent", help="观测 agent 名，如 classify / transport")
    parser.add_argument("usage", help="稳定槽位名，如 system / choose_mode")
    parser.add_argument("file", nargs="?", help="模板文件；与 --stdin 二选一")
    parser.add_argument("--stdin", action="store_true", help="从 stdin 读模板正文")
    args = parser.parse_args()

    if args.stdin:
        content = sys.stdin.read()
    elif args.file:
        content = Path(args.file).read_text(encoding="utf-8")
    else:
        parser.error("请提供模板文件或 --stdin")
        return 2

    if not content.strip():
        print("模板为空", file=sys.stderr)
        return 2

    ver = asyncio.run(_run(args.agent, args.usage, content))
    print(f"已写入 {args.agent}/{args.usage} version={ver}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
