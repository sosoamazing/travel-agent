"""monitor 独立监控服务：直连 PostgreSQL 只读 obs_* 表，提供观测指标 REST 查询。

运行（在 travel-agent/travel-agent 目录下）：
    uvicorn monitor.main:app --host 0.0.0.0 --port 8002

说明：
- 本服务不 import backend 任何模块，只读共享的 obs_* 表做聚合，无写入。
- 面向管理员 / 监控看板，不需要 JWT；如需鉴权可在网关层加反代白名单。
"""
from __future__ import annotations

import sys
from pathlib import Path

# Windows 中文控制台默认 GBK 编码，无法编码 emoji 会导致 print 抛 UnicodeEncodeError。
# 统一把 stdout/stderr 切到 UTF-8，避免运行期崩溃。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

from monitor import analyze
from monitor.db import get_connection

app = FastAPI(title="Travel Agent Monitor", version="1.0.0")


@app.get("/health")
def health() -> Dict[str, Any]:
    """DB 探活。"""
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 AS ok")
            row = cur.fetchone()
            cur.close()
        return {"status": "ok", "db": bool(row["ok"])}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/versions")
def list_versions() -> List[str]:
    """观测数据中已记录的全部版本号（供管理员按版本筛选）。"""
    return analyze.list_versions()


@app.get("/tasks")
def list_tasks(limit: int = Query(50, ge=1, le=500),
               version: Optional[str] = Query(None)) -> List[Dict[str, Any]]:
    """最近 N 条任务的标准化观测数据（可按 version 筛选）。"""
    return analyze.load_from_db(limit, version)


@app.get("/report")
def report(limit: int = Query(50, ge=1, le=500),
           version: Optional[str] = Query(None)) -> Dict[str, Any]:
    """结构化聚合报表（耗时 / Token / 节点热图 / Agent 成本 / 工具可靠性 / 重规划 / 错误样本）。"""
    return analyze.build_report(analyze.load_from_db(limit, version))


@app.get("/report/text")
def report_text(limit: int = Query(50, ge=1, le=500),
                version: Optional[str] = Query(None)) -> PlainTextResponse:
    """纯文本报表（等价于 CLI 输出，方便直接查看）。"""
    tasks = analyze.load_from_db(limit, version)
    if not tasks:
        raise HTTPException(status_code=404, detail="no observation data")
    text = "\n".join([
        analyze.render_overview(tasks),
        analyze.render_node_heatmap(tasks),
        analyze.render_agent_cost(tasks),
        analyze.render_tool_errors(tasks),
        analyze.render_replanner_stats(tasks),
        analyze.render_error_sample(tasks),
    ])
    return PlainTextResponse(text)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("monitor.main:app", host="0.0.0.0", port=8002, reload=False)
