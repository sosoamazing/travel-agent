"""高德 POI typecode 字典表：PostgreSQL 持久化 + 查询。

数据源：项目根目录 typecode 文件，每行格式 `id typecode 一级分类 二级分类 三级分类`（空格分隔）。
- import_typecode_file(path)  解析文件并 upsert 到 gaode_typecode 表（幂等，可重复执行）
- get_typecode_desc(code)     按 typecode 查中文描述（进程内缓存，DB 不可用时返回 None）
- aget_typecode_desc(code)    异步版（底层已 async，直接复用）

命令行直接运行可导入字典：
    python tools/typecode_db.py [typecode文件路径]
"""
import asyncio
import logging
import os
import sys

# 保证以脚本方式运行时 backend 目录在 sys.path 中（与 mcp_tools.py 同模式）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 上 psycopg3 的异步实现依赖 SelectorEventLoop（需要 add_reader），而 Python
# 默认使用 ProactorEventLoop（不支持 add_reader）。与 server.py 顶层处理一致；
# 必须在 asyncio.run() 之前设置。
if sys.platform == "win32":
    import selectors as _selectors

    class _SelectorLoopPolicy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self):
            return asyncio.SelectorEventLoop(_selectors.SelectSelector())

    asyncio.set_event_loop_policy(_SelectorLoopPolicy())

from db import async_db_connection  # noqa: E402

logger = logging.getLogger(__name__)

# 进程内缓存：typecode -> 中文描述，避免每次工具调用都查库
_desc_cache: dict = {}

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS gaode_typecode (
    typecode    VARCHAR(32) PRIMARY KEY,
    description TEXT NOT NULL
)
"""

_UPSERT_SQL = """
INSERT INTO gaode_typecode (typecode, description)
VALUES (%s, %s)
ON CONFLICT (typecode) DO UPDATE SET description = EXCLUDED.description
"""

_SELECT_SQL = "SELECT description FROM gaode_typecode WHERE typecode = %s"


def _parse_file(path: str) -> list:
    """解析 typecode 文件，返回 [(typecode, description), ...]。

    description = 第二列之后的全部中文（含品牌名带空格的场景，如 'Pacific Coffee Company'）。
    """
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            rows.append((parts[1], " ".join(parts[2:]).strip()))
    return rows


async def import_typecode_file(path: str) -> int:
    """解析 typecode 文件并 upsert 到 gaode_typecode 表，返回写入行数（幂等）。"""
    rows = _parse_file(path)
    if not rows:
        logger.warning(f"⚠️ typecode 文件无有效行: {path}")
        return 0

    async with async_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_TABLE_SQL)
            await cur.executemany(_UPSERT_SQL, rows)

    _desc_cache.update(rows)
    logger.info(f"💾 typecode 字典导入完成: {len(rows)} 条 -> {path}")
    return len(rows)


async def get_typecode_desc(code: str):
    """查询 typecode 的中文描述；未命中或 DB 异常返回 None。"""
    if not code:
        return None
    if code in _desc_cache:
        return _desc_cache[code]
    try:
        async with async_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_SQL, (code,))
                row = await cur.fetchone()
        if row:
            desc = row["description"]
            _desc_cache[code] = desc
            return desc
    except Exception as e:
        logger.warning(f"⚠️ typecode 查询失败 {code}: {e}")
    return None


async def aget_typecode_desc(code: str):
    """异步查询 typecode 中文描述（底层已 async，直接复用）。"""
    return await get_typecode_desc(code)


if __name__ == "__main__":
    from config.settings import PROJECT_ROOT

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    file_path = sys.argv[1] if len(sys.argv) > 1 else str(PROJECT_ROOT.parent.parent / "typecode")
    count = asyncio.run(import_typecode_file(file_path))
    print(f"导入完成: {count} 条")
