"""一次性导入测试问题到数据库（test_questions 表）。

用法：
  1. 用提示词让其他模型生成 200 个问题，保存为 JSON 数组格式，
     默认文件名 test_questions.txt（放在项目根目录），每项含 question + intent：
     [{"question": "...", "intent": "travel"}, ...]
  2. python import_questions.py [问题文件路径]

自动建表、去空、去重（按 question 去重），清空旧数据后整体导入，
插入时写入 intent 字段（不参与路由，仅作为标注意图用于对比）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import psycopg2
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=True)

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", "travel_agent")
PG_PASSWORD = os.getenv("PG_PASSWORD", "travel_agent")
PG_DATABASE = os.getenv("PG_DATABASE", "travel_agent")

# 合法意图集合（非法/缺失一律置空串，不影响分类）
_VALID_INTENTS = {"travel", "information", "conversation", "feedback"}


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else str(_PROJECT_ROOT / "test_questions.txt")
    if not Path(path).exists():
        print(f"❌ 找不到问题文件: {path}\n   请先用提示词生成 200 个问题并保存为该文件（JSON 数组）。")
        return 1

    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"❌ 文件不是合法 JSON 数组: {e}")
        return 1
    if not isinstance(data, list):
        print("❌ 文件顶层必须是 JSON 数组。")
        return 1

    questions = []
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        q = (item.get("question") or "").strip()
        if not q:
            continue
        if q in seen:
            continue
        seen.add(q)
        intent = (item.get("intent") or "").strip().lower()
        if intent not in _VALID_INTENTS:
            intent = ""
        questions.append((q, intent))
    if not questions:
        print("❌ 文件中没有有效问题。")
        return 1

    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
                            password=PG_PASSWORD, dbname=PG_DATABASE)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS test_questions (
        id SERIAL PRIMARY KEY,
        question TEXT NOT NULL,
        intent TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT now()
    )""")

    # 清空旧数据，避免重复导入叠加
    cur.execute("DELETE FROM test_questions")
    inserted = 0
    for q, intent in questions:
        cur.execute("INSERT INTO test_questions (question, intent) VALUES (%s, %s)", (q, intent))
        inserted += 1

    print(f"✅ 已导入 {inserted} 个问题（去重后）到 test_questions 表\n")
    cur.execute("SELECT count(*) FROM test_questions")
    print(f"总问题数: {cur.fetchone()[0]}")
    cur.execute("SELECT intent, count(*) FROM test_questions GROUP BY intent ORDER BY intent")
    print("意图分布:", dict(cur.fetchall()))
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
