"""一次性脚本：清空当前数据库里的所有观测指标（obs_* 表）。

用途：
- 清空当前 span 树观测表（含 LLM payload）以及历史遗留 obs_* 表。
- 不删 prompt_versions / agent_releases（模板目录与发布清单，不是任务日志）。
- 下次 backend 启动时 _obs_storage._ensure_init 会用 IF NOT EXISTS 自动重建。

运行（在 travel-agent/travel-agent 目录下）：
    python reset_observability.py

不可恢复：请确认后再执行。
"""
from __future__ import annotations

import os
import sys

# Windows 中文控制台编码兜底
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import psycopg2
from dotenv import load_dotenv
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=True)

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", "travel_agent")
PG_PASSWORD = os.getenv("PG_PASSWORD", "travel_agent")
PG_DATABASE = os.getenv("PG_DATABASE", "travel_agent")

# 全部 obs_* 表名（当前新版 span 树模型 + 历史遗留旧表），统一 DROP
ALL_OBS_TABLES = [
    # ── 当前版本：通用 span 树模型（uuid span_id + parent_id 通用层级）──
    "obs_llm_payloads",
    "obs_node_spans",
    "obs_llm_spans",
    "obs_mcp_spans",
    "obs_tasks",
    # ── 历史遗留旧表（早期模型，确保一并清空）──
    "obs_spans",
    "obs_llm_metrics",
    "obs_llm_outputs",
    "obs_mcp_metrics",
    "obs_mcp_results",
    "obs_node_metrics",
]


def main() -> int:
    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER,
        password=PG_PASSWORD, dbname=PG_DATABASE,
    )
    conn.autocommit = True
    try:
        cur = conn.cursor()

        # 先列出现有的 obs_* 表及其行数
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name LIKE 'obs_%' "
            "ORDER BY table_name"
        )
        existing = [r[0] for r in cur.fetchall()]
        print(f"当前存在的 obs_* 表：{existing if existing else '(无)'}")
        for t in existing:
            cur.execute(f"SELECT count(*) FROM {t}")
            print(f"  {t}: {cur.fetchone()[0]} 行")

        dropped = []
        for t in ALL_OBS_TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
            dropped.append(t)

        print("\n已 DROP 表：")
        for t in dropped:
            print(f"  - {t}")

        # 校验：确认已无 obs_* 表
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name LIKE 'obs_%'"
        )
        remaining = [r[0] for r in cur.fetchall()]
        if remaining:
            print(f"\n⚠️  仍有残留 obs_* 表：{remaining}")
            return 1
        print("\n✅ 所有观测指标已清空（obs_* 表已全部删除）。")
        print("   下次 backend 启动时 _init_db 会自动重建新表。")
        cur.close()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
