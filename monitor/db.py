"""monitor 独立 DB 读取层：直连 PostgreSQL，只读 obs_* 系列表。

不 import backend 的任何模块，仅加载同一份 .env 读取 PG 连接配置。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=True)

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", "travel_agent")
PG_PASSWORD = os.getenv("PG_PASSWORD", "travel_agent")
PG_DATABASE = os.getenv("PG_DATABASE", "travel_agent")


@contextmanager
def get_connection() -> Iterator:
    """获取一个同步连接（RealDictCursor），用完自动关闭。

    monitor 为低频只读场景，每次调用新建连接即可，无需维护常驻连接池。
    """
    conn = psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        dbname=PG_DATABASE,
        cursor_factory=RealDictCursor,
    )
    try:
        yield conn
    finally:
        conn.close()
