"""观测指标分析报表（monitor 独立服务核心）。

数据源（按优先级）：
  1. PostgreSQL obs_* 系列表：聚合历史 N 条任务，看长期趋势（推荐）
  2. 本地 data/observability.json + result.json：仅分析最新一轮任务

用法（在 travel-agent/travel-agent 目录下）：
  # 默认分析 DB 最近 50 条任务
  python -m monitor.analyze

  # 指定分析条数
  python -m monitor.analyze --limit 200

  # 强制用本地 JSON（DB 连不上时）
  python -m monitor.analyze --json-only

  # 导出 CSV（方便粘到 Excel/飞书做图）
  python -m monitor.analyze --csv reports/obs_report.csv

本模块不 import backend 的任何代码：直接读取共享 obs_* 表并在此实现只读聚合。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ──────────────────────────────────────────────────────────────
# 路径 & 依赖
# ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monitor.db import get_connection  # noqa: E402


# ──────────────────────────────────────────────────────────────
# 模型参数反查（agent → 真实模型名）
# ──────────────────────────────────────────────────────────────
# 设计文档：LLM 模型名不存表（obs_llm_spans 只存 agent），合并展示时按配置反查。
# 此处直接读取 backend/config/settings.py 的 get_agent_model（含 .env 的 LLM_MODEL_<AGENT> 覆盖），
# 与观测层 build_task_json 的 model_map 保持同源一致。
# monitor 独立服务虽不 import backend 业务代码，但 config 仅含 os.getenv 常量，无重依赖，可安全读取。
_BACKEND_DIR = PROJECT_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# 主模型 / flash 模型（懒加载，加载失败时降级为空，反查回退为 agent 名）
_MAIN_MODEL: str = ""
_FLASH_MODEL: str = ""
_FLASH_AGENTS = {"summarizer", "json_fix", "hotel_price"}


def _load_model_config() -> bool:
    """懒加载 backend/config/settings 的模型常量；失败返回 False（monitor 独立运行不致命）。"""
    global _MAIN_MODEL, _FLASH_MODEL
    if _MAIN_MODEL:
        return True
    try:
        from config import settings as _cfg  # noqa: WPS433  (懒加载，独立服务避免启动强依赖)
        _MAIN_MODEL = _cfg.QWEN3_MODEL
        _FLASH_MODEL = _cfg.DS_FLASH_MODEL
        return True
    except Exception:
        return False


def get_agent_model(agent: str) -> str:
    """反查 agent 对应的真实模型名；无配置或加载失败时回退为 agent 名本身。

    覆盖规则（与 backend/config/settings.get_agent_model 一致）：
      1. .env 中 LLM_MODEL_<AGENT> 显式覆盖优先；
      2. summarizer / json_fix / hotel_price 默认 flash；
      3. 其余默认主模型（QWEN3_MODEL）。
    """
    if not _load_model_config():
        return agent
    try:
        from config import settings as _cfg  # noqa: WPS433
        default = _FLASH_MODEL if agent in _FLASH_AGENTS else _MAIN_MODEL
        return _cfg.get_agent_model(agent, default)
    except Exception:
        return agent



# backend 运行期写入观测 JSON 的目录（仅作 JSON 回退数据源，不 import backend）
DATA_DIR = PROJECT_ROOT / "backend" / "data"
OBS_JSON_PATH = DATA_DIR / "observability.json"
RESULT_JSON_PATH = DATA_DIR / "result.json"


# ──────────────────────────────────────────────────────────────
# 统计工具
# ──────────────────────────────────────────────────────────────
def percentile(values: List[float], p: int) -> float:
    """P50 / P95 / P99 分位数（空列表返回 0）。"""
    if not values:
        return 0.0
    vs = sorted(values)
    k = (len(vs) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(vs[int(k)], 2)
    return round(vs[f] + (vs[c] - vs[f]) * (k - f), 2)
def stats(values: List[float]) -> Dict[str, float]:
    """一次性输出 count / mean / P50 / P95 / P99 / max。

    P95 决策：当前规模（每版本几百行明细）下直接用明细样本算精确百分位，
    不用滑动窗口 / t-digest（那是海量+实时场景才需要的，见 docs/observability-architecture.md）。
    """
    if not values:
        return {"count": 0, "mean": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0}
    return {
        "count": len(values),
        "mean": round(statistics.mean(values), 2),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": round(max(values), 2),
    }


def _span_dur(row: Dict[str, Any]) -> float:
    """从 span 行 start_ts/end_ts 推导耗时（毫秒）；缺失返回 0。"""
    try:
        s = float(row.get("start_ts") or 0)
        e = float(row.get("end_ts") or 0)
        if s and e:
            return round((e - s) * 1000, 2)
    except Exception:
        pass
    return 0.0


def fmt_ms(ms: float) -> str:
    """把毫秒格式化成易读的字符串：<1s 显示 ms，>=1s 显示 s。"""
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms/1000:.2f}s"


def fmt_tokens(n: int) -> str:
    if n >= 10000:
        return f"{n/1000:.1f}K"
    return f"{n}"


def bar(value: float, max_value: float, width: int = 20) -> str:
    """简易 ASCII 柱状图。"""
    if max_value <= 0 or value <= 0:
        return "·" * width
    fill = int(value / max_value * width)
    return "█" * fill + "·" * (width - fill)


# ──────────────────────────────────────────────────────────────
# 只读聚合：span 列表 → summary（复制自 backend agent_nodes._observability._summary）
# ──────────────────────────────────────────────────────────────
def _summary(spans: List[Dict[str, Any]]) -> Dict[str, Any]:
    llm_spans = [s for s in spans if s["span_type"] in ("llm", "correction")]
    mcp_spans = [s for s in spans if s["span_type"] == "mcp"]
    node_spans = [s for s in spans if s["span_type"] == "node"]
    total_input = sum(int(s.get("input_tokens") or 0) for s in llm_spans)
    total_output = sum(int(s.get("output_tokens") or 0) for s in llm_spans)
    total_cached = sum(int(s.get("cached_input_tokens") or 0) for s in llm_spans)
    starts = [s["start_ts"] for s in spans if s.get("start_ts")]
    ends = [s["end_ts"] for s in spans if s.get("end_ts")]
    wall_clock_ms = round((max(ends) - min(starts)) * 1000, 2) if starts and ends else 0.0
    return {
        "node_count": len(node_spans),
        "llm_call_count": len(llm_spans),
        "tool_call_count": len(mcp_spans),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "cached_input_tokens": total_cached,
        "cache_hit_rate": round(total_cached / total_input, 4) if total_input > 0 else None,
        "llm_duration_ms": round(sum((s.get("duration_ms") or 0) for s in llm_spans), 2),
        "tool_duration_ms": round(sum((s.get("duration_ms") or 0) for s in mcp_spans), 2),
        "wall_clock_ms": wall_clock_ms,
    }


def _merge_per_node(base: Dict[str, Any], other: Dict[str, Any]) -> Dict[str, Any]:
    """把另一个 per_node 条目合并进 base（用于同名节点聚合，如 city_plan:*）。

    duration_ms 取 max（城市并发执行取 makespan），其余指标求和；
    invocations 相加（反映该节点在单次任务内的实际调用次数）；
    llm 子 dict 数值相加后重算 cache_hit_rate；tools 按 (server, tool) 合并。
    """
    base["invocations"] = base.get("invocations", 1) + other.get("invocations", 1)
    base["llm_calls"] += other["llm_calls"]
    base["mcp_calls"] += other["mcp_calls"]
    base["duration_ms"] = max(base["duration_ms"], other["duration_ms"])
    if other["status"] == "error":
        base["status"] = "error"

    bl, ol = base["llm"], other["llm"]
    for k in ("calls", "input_tokens", "output_tokens", "cached_input_tokens", "duration_ms", "error_count"):
        bl[k] = bl.get(k, 0) + ol.get(k, 0)
    bl["cache_hit_rate"] = round(bl["cached_input_tokens"] / bl["input_tokens"], 4) if bl["input_tokens"] > 0 else None

    tool_map: Dict[Tuple[str, str], Dict[str, Any]] = {
        (t["server"], t["tool"]): t for t in base["tools"]
    }
    for t in other["tools"]:
        key = (t["server"], t["tool"])
        if key in tool_map:
            b = tool_map[key]
            b["calls"] += t["calls"]
            b["errors"] += t["errors"]
            b["duration_ms"] += t["duration_ms"]
            b["retries"] += t["retries"]
        else:
            tool_map[key] = dict(t)
    merged_tools = list(tool_map.values())
    for t in merged_tools:
        t["duration_ms"] = round(t["duration_ms"], 2)
    merged_tools.sort(key=lambda x: x["calls"], reverse=True)
    base["tools"] = merged_tools
    return base


# ──────────────────────────────────────────────────────────────
# 数据源 1：PostgreSQL 历史任务
# ──────────────────────────────────────────────────────────────
def _version_sort_key(v: str) -> int:
    m = re.match(r"^(\d+)", v or "")
    return int(m.group(1)) if m else -1


def list_versions() -> List[str]:
    """返回 obs_tasks 中已记录的全部版本号（按 commit 序号数值降序）。"""
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT version FROM obs_tasks "
                "WHERE version IS NOT NULL AND version != ''"
            )
            versions = [r["version"] for r in cur.fetchall()]
            cur.close()
        versions.sort(key=_version_sort_key, reverse=True)
        return versions
    except Exception as e:
        print(f"⚠️  list_versions 失败（{e}）")
        return []


def load_from_db(limit: int = 50, version: Optional[str] = None) -> List[Dict[str, Any]]:
    """从 DB 读取最近 N 条任务（可按 version 筛选），组装成标准化的任务列表。

    每条任务格式：
    {
      "task_id": str, "user_query": str, "user_id": str, "version": str,
      "start_ts": float, "duration_ms": float, "status": "ok"|"error", "error": str|None,
      "summary": { 节点数/LLM 数/工具数/Token 汇总/耗时汇总 },
      "per_node": { node_name: { invocations, duration_ms, status, llm_calls, mcp_calls,
                                 llm: {...}, tools: [...] } },
      "per_agent": { agent: { input_tokens, output_tokens, call_count, duration_ms } },
      "per_model": { model: { input_tokens, output_tokens, cached_input_tokens, call_count } },
      "per_tool": { "server/tool": { call_count, error_count, duration_ms, retries } },
      "llm_errors":  [ {agent, model, error} ],
      "tool_errors": [ {server, tool, error} ],
    }
    """
    query = "SELECT * FROM obs_tasks"
    params: List[Any] = []
    if version:
        query += " WHERE version = %s"
        params.append(version)
    query += " ORDER BY start_ts DESC LIMIT %s"
    params.append(limit)

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(query, tuple(params))
            tasks = [dict(r) for r in cur.fetchall()]
            cur.close()
    except Exception as e:
        print(f"⚠️  DB 数据源初始化失败（{e}），请改用 --json-only")
        return []

    if not tasks:
        return []

    # 批量取回三个 span 子表：每个表只查一次，WHERE task_id IN (...)，内存按 task_id 分组
    # 避免 N+1（原实现：每个 task_id 循环查 3 次 = 1 + 50×3 = 151 次查询）
    task_ids = [t["task_id"] for t in tasks]
    placeholders = ",".join(["%s"] * len(task_ids))
    rows_by_task: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        tid: {"node": [], "llm": [], "mcp": []} for tid in task_ids
    }
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            for table, key in (("obs_node_spans", "node"),
                               ("obs_llm_spans", "llm"),
                               ("obs_mcp_spans", "mcp")):
                cur.execute(
                    f"SELECT * FROM {table} WHERE task_id IN ({placeholders})",
                    tuple(task_ids),
                )
                for row in cur.fetchall():
                    rows_by_task.setdefault(row["task_id"], {"node": [], "llm": [], "mcp": []})[key].append(dict(row))
            cur.close()
    except Exception as e:
        print(f"⚠️  观测子表批量读取失败（{e}）")
        return []

    results: List[Dict[str, Any]] = []
    for t in tasks:
        task_id = t["task_id"]
        node_rows = rows_by_task.get(task_id, {}).get("node", [])
        llm_rows = rows_by_task.get(task_id, {}).get("llm", [])
        mcp_rows = rows_by_task.get(task_id, {}).get("mcp", [])

        # per_node：从 span 表逐调用聚合
        per_node: Dict[str, Dict[str, Any]] = {}
        llm_by_node: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        mcp_by_node: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for l in llm_rows:
            llm_by_node[l["node"]].append(l)
        for m in mcp_rows:
            mcp_by_node[m["node"]].append(m)

        for nr in node_rows:
            name = nr["node"]
            child_llm = llm_by_node.get(name, [])
            child_mcp = mcp_by_node.get(name, [])
            llm_calls = len(child_llm)
            llm_input = sum(int(l.get("input_tokens") or 0) for l in child_llm)
            llm_output = sum(int(l.get("output_tokens") or 0) for l in child_llm)
            llm_cached = sum(int(l.get("cached_tokens") or 0) for l in child_llm)
            llm_dur = sum(_span_dur(l) for l in child_llm)
            llm_errs = sum(1 for l in child_llm if l.get("result_kind") == "error")

            tool_agg: Dict[str, Dict[str, Any]] = {}
            for m in child_mcp:
                server = m.get("server") or "?"
                tool = m.get("tool") or "?"
                key = f"{server}/{tool}"
                if key not in tool_agg:
                    tool_agg[key] = {
                        "server": server, "tool": tool,
                        "calls": 0, "errors": 0, "duration_ms": 0.0, "retries": 0,
                    }
                item = tool_agg[key]
                item["calls"] += 1
                item["duration_ms"] += _span_dur(m)
                item["retries"] += int(m.get("retries") or 0)
                if m.get("result_kind") == "error":
                    item["errors"] += 1
            node_tools = list(tool_agg.values())
            for item in node_tools:
                item["duration_ms"] = round(item["duration_ms"], 2)
            node_tools.sort(key=lambda x: x["calls"], reverse=True)

            # 节点内 per-agent LLM 明细（透传，供前端按 agent 下钻）
            _agent_acc: Dict[str, Dict[str, Any]] = {}
            for l in child_llm:
                agent = l.get("agent") or "unknown"
                a = _agent_acc.setdefault(agent, {
                    "agent": agent, "model": get_agent_model(agent), "calls": 0,
                    "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
                    "duration_ms": 0.0, "error_count": 0,
                })
                a["calls"] += 1
                a["input_tokens"] += int(l.get("input_tokens") or 0)
                a["output_tokens"] += int(l.get("output_tokens") or 0)
                a["cached_input_tokens"] += int(l.get("cached_tokens") or 0)
                a["duration_ms"] += _span_dur(l)
                if l.get("result_kind") == "error":
                    a["error_count"] += 1
            node_agents = list(_agent_acc.values())
            for a in node_agents:
                a["cache_hit_rate"] = round(a["cached_input_tokens"] / a["input_tokens"] * 100, 1) if a["input_tokens"] > 0 else 0.0
                a["duration_ms"] = round(a["duration_ms"], 2)
            node_agents.sort(key=lambda x: x["input_tokens"] + x["output_tokens"], reverse=True)

            per_node[name] = {
                "invocations": 1,
                "duration_ms": _span_dur(nr),
                "status": nr.get("result_kind") or "ok",
                "llm_calls": llm_calls,
                "mcp_calls": len(child_mcp),
                "llm": {
                    "calls": llm_calls,
                    "input_tokens": int(llm_input),
                    "output_tokens": int(llm_output),
                    "cached_input_tokens": int(llm_cached),
                    "duration_ms": round(llm_dur, 2),
                    "error_count": llm_errs,
                    "cache_hit_rate": round(llm_cached / llm_input, 4) if llm_input > 0 else None,
                },
                "tools": node_tools,
                "llm_agents": node_agents,
            }

        # per_agent / per_model / llm_errors
        per_agent: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"input_tokens": 0, "output_tokens": 0, "call_count": 0, "duration_ms": 0.0}
        )
        per_model: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "call_count": 0}
        )
        llm_errors: List[Dict[str, str]] = []
        for l in llm_rows:
            agent = l.get("agent") or "unknown"
            inp = int(l.get("input_tokens") or 0)
            out = int(l.get("output_tokens") or 0)
            dur = _span_dur(l)
            per_agent[agent]["input_tokens"] += inp
            per_agent[agent]["output_tokens"] += out
            per_agent[agent]["call_count"] += 1
            per_agent[agent]["duration_ms"] += dur
            # 模型名不存表（obs_llm_spans 只存 agent），展示时按配置反查真实模型名（get_agent_model）
            model = get_agent_model(agent)
            per_model[model]["input_tokens"] += inp
            per_model[model]["output_tokens"] += out
            per_model[model]["cached_input_tokens"] += int(l.get("cached_tokens") or 0)
            per_model[model]["call_count"] += 1
            if l.get("result_kind") == "error" and l.get("error_what"):
                llm_errors.append({"agent": agent, "model": model, "error": l.get("error_what") or ""})

        # per_tool / tool_errors
        per_tool: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"call_count": 0, "error_count": 0, "duration_ms": 0.0, "retries": 0}
        )
        tool_errors: List[Dict[str, str]] = []
        for m in mcp_rows:
            server = m.get("server") or "?"
            tool = m.get("tool") or "?"
            key = f"{server}/{tool}"
            per_tool[key]["call_count"] += 1
            per_tool[key]["duration_ms"] += _span_dur(m)
            per_tool[key]["retries"] += int(m.get("retries") or 0)
            if m.get("result_kind") == "error":
                per_tool[key]["error_count"] += 1
                tool_errors.append({"server": server, "tool": tool, "error": m.get("error_what") or ""})

        # summary：从 span 表动态聚合
        llm_rows_sum = llm_rows
        mcp_rows_sum = mcp_rows
        total_input = sum(int(l.get("input_tokens") or 0) for l in llm_rows_sum)
        total_output = sum(int(l.get("output_tokens") or 0) for l in llm_rows_sum)
        total_cached = sum(int(l.get("cached_tokens") or 0) for l in llm_rows_sum)
        llm_dur_sum = sum(_span_dur(l) for l in llm_rows_sum)
        tool_dur_sum = sum(_span_dur(m) for m in mcp_rows_sum)
        summary = {
            "node_count": len(node_rows),
            "llm_call_count": len(llm_rows_sum),
            "tool_call_count": len(mcp_rows_sum),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "cached_input_tokens": total_cached,
            "cache_hit_rate": round(total_cached / total_input, 4) if total_input > 0 else None,
            "llm_duration_ms": round(llm_dur_sum, 2),
            "tool_duration_ms": round(tool_dur_sum, 2),
            "wall_clock_ms": round((float(t.get("end_ts") or 0) - float(t.get("start_ts") or 0)) * 1000, 2) if t.get("end_ts") and t.get("start_ts") else 0.0,
        }

        results.append({
            "task_id": task_id,
            "user_query": t.get("user_query") or "",
            "user_id": t.get("user_id") or "",
            "version": t.get("version") or "",
            "start_ts": float(t.get("start_ts") or 0),
            "duration_ms": float(t.get("duration_ms") or 0),
            "status": t.get("result_kind") or "?",
            "error": t.get("error_what"),
            "intent": t.get("intent") or "",
            "query_type": t.get("query_type") or "",
            "client_duration_ms": t.get("client_duration_ms"),
            "summary": summary,
            "per_node": per_node,
            "per_agent": dict(per_agent),
            "per_model": dict(per_model),
            "per_tool": dict(per_tool),
            "llm_errors": llm_errors,
            "tool_errors": tool_errors,
        })

    results.sort(key=lambda x: x["start_ts"])
    return results


# ──────────────────────────────────────────────────────────────
# 数据源 2：本地 JSON（最新任务）
# ──────────────────────────────────────────────────────────────
def load_from_json() -> List[Dict[str, Any]]:
    """读 data/observability.json + result.json，映射成标准任务结构（仅一条）。"""
    if not OBS_JSON_PATH.exists():
        print(f"⚠️  未找到 {OBS_JSON_PATH}，请先跑一次规划再分析。")
        return []

    try:
        obs = json.loads(OBS_JSON_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ 读取 observability.json 失败：{e}")
        return []

    nodes = obs.get("nodes", [])
    summary = obs.get("summary", {})

    per_agent: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"input_tokens": 0, "output_tokens": 0, "call_count": 0, "duration_ms": 0.0}
    )
    per_model: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "call_count": 0}
    )
    per_tool: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"call_count": 0, "error_count": 0, "duration_ms": 0.0, "retries": 0}
    )
    per_node: Dict[str, Dict[str, Any]] = {}
    llm_errors: List[Dict[str, str]] = []
    tool_errors: List[Dict[str, str]] = []

    def _walk_node(node: Dict[str, Any]):
        name = node.get("node") or "?"
        # 城市城内规划节点归一化：city_plan:杭州 / city_plan:上海 … 都聚合成一个 city_plan
        if name.startswith("city_plan:"):
            name = "city_plan"
        llm_list = node.get("llm", [])
        tool_list = node.get("tools", [])
        llm_input = sum(int(l.get("input_tokens") or 0) for l in llm_list)
        llm_cached = sum(int(l.get("cached_input_tokens") or 0) for l in llm_list)
        tool_agg: Dict[str, Dict[str, Any]] = {}
        for t in tool_list:
            server = t.get("server") or "?"
            tool = t.get("tool") or "?"
            key = f"{server}/{tool}"
            if key not in tool_agg:
                tool_agg[key] = {
                    "server": server, "tool": tool,
                    "calls": 0, "errors": 0, "duration_ms": 0.0, "retries": 0,
                }
            item = tool_agg[key]
            item["calls"] += 1
            item["duration_ms"] += float(t.get("duration_ms") or 0)
            item["retries"] += int(t.get("retries") or 0)
            if t.get("status") == "error":
                item["errors"] += 1
        node_tools = list(tool_agg.values())
        for item in node_tools:
            item["duration_ms"] = round(item["duration_ms"], 2)
        node_tools.sort(key=lambda x: x["calls"], reverse=True)
        entry = {
            "invocations": 1,
            "duration_ms": float(node.get("duration_ms") or 0),
            "status": node.get("status") or "?",
            "llm_calls": len(llm_list),
            "mcp_calls": len(tool_list),
            "llm": {
                "calls": len(llm_list),
                "input_tokens": llm_input,
                "output_tokens": sum(int(l.get("output_tokens") or 0) for l in llm_list),
                "cached_input_tokens": llm_cached,
                "duration_ms": round(sum(float(l.get("duration_ms") or 0) for l in llm_list), 2),
                "error_count": sum(1 for l in llm_list if l.get("status") == "error"),
                "cache_hit_rate": round(llm_cached / llm_input, 4) if llm_input > 0 else None,
            },
            "tools": node_tools,
        }
        if name in per_node:
            # 同名节点（如各城市 city_plan）合并观测指标
            _merge_per_node(per_node[name], entry)
        else:
            per_node[name] = entry
        for l in llm_list:
            agent = l.get("agent") or "unknown"
            model = l.get("model") or "unknown"
            inp = int(l.get("input_tokens") or 0)
            out = int(l.get("output_tokens") or 0)
            dur = float(l.get("duration_ms") or 0)
            per_agent[agent]["input_tokens"] += inp
            per_agent[agent]["output_tokens"] += out
            per_agent[agent]["call_count"] += 1
            per_agent[agent]["duration_ms"] += dur
            per_model[model]["input_tokens"] += inp
            per_model[model]["output_tokens"] += out
            per_model[model]["cached_input_tokens"] += int(l.get("cached_input_tokens") or 0)
            per_model[model]["call_count"] += 1
            if l.get("status") == "error":
                llm_errors.append({"agent": agent, "model": model, "error": l.get("error") or ""})
            corr = l.get("correction")
            if corr and corr.get("calls"):
                for cl in corr["calls"]:
                    _walk_llm_sub(cl)
        for t in tool_list:
            server = t.get("server") or "?"
            tool = t.get("tool") or "?"
            key = f"{server}/{tool}"
            dur = float(t.get("duration_ms") or 0)
            per_tool[key]["call_count"] += 1
            per_tool[key]["duration_ms"] += dur
            per_tool[key]["retries"] += int(t.get("retries") or 0)
            if t.get("status") == "error":
                per_tool[key]["error_count"] += 1
                tool_errors.append({"server": server, "tool": tool, "error": t.get("error") or ""})
        for sn in node.get("sub_nodes", []):
            _walk_node(sn)

    def _walk_llm_sub(l: Dict[str, Any]):
        agent = l.get("agent") or "json_fix"
        model = l.get("model") or "unknown"
        inp = int(l.get("input_tokens") or 0)
        out = int(l.get("output_tokens") or 0)
        dur = float(l.get("duration_ms") or 0)
        per_agent[agent]["input_tokens"] += inp
        per_agent[agent]["output_tokens"] += out
        per_agent[agent]["call_count"] += 1
        per_agent[agent]["duration_ms"] += dur
        per_model[model]["input_tokens"] += inp
        per_model[model]["output_tokens"] += out
        per_model[model]["cached_input_tokens"] += int(l.get("cached_input_tokens") or 0)
        per_model[model]["call_count"] += 1

    for n in nodes:
        _walk_node(n)

    budget_info = {}
    if RESULT_JSON_PATH.exists():
        try:
            res = json.loads(RESULT_JSON_PATH.read_text(encoding="utf-8"))
            budget_info = {
                "query_type": res.get("query_type"),
                "total_budget": res.get("total_budget"),
                "spent_budget": res.get("spent_budget"),
                "over_budget": res.get("over_budget"),
                "cities_count": len(res.get("cities", []) or []),
            }
        except Exception:
            pass

    task = {
        "task_id": obs.get("task_id") or "local-json",
        "user_query": obs.get("user_query") or "",
        "user_id": obs.get("user_id") or "",
        "start_ts": float(obs.get("start_ts") or 0),
        "duration_ms": float(summary.get("wall_clock_ms") or 0),
        "status": "ok" if not obs.get("error") else "error",
        "error": obs.get("error"),
        "intent": obs.get("intent") or "",
        "query_type": obs.get("query_type") or "",
        "summary": summary,
        "per_node": per_node,
        "per_agent": dict(per_agent),
        "per_model": dict(per_model),
        "per_tool": dict(per_tool),
        "llm_errors": llm_errors,
        "tool_errors": tool_errors,
    }
    task.update(budget_info)
    return [task]


# ──────────────────────────────────────────────────────────────
# 结构化报表（供 REST /report 使用）
# ──────────────────────────────────────────────────────────────
def build_report(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把任务列表聚合成结构化 JSON 报表。"""
    if not tasks:
        return {"task_count": 0}

    total = len(tasks)
    ok_count = sum(1 for t in tasks if t["status"] == "ok")
    error_count = sum(1 for t in tasks if t["status"] == "error")
    running_count = sum(1 for t in tasks if t["status"] == "running")
    durations = [t["duration_ms"] for t in tasks if t["duration_ms"] > 0]
    wall = stats(durations)

    # 端到端耗时（客户端测试请求发出 → 收到结果，仅统计有回写的任务）
    client_durations = [t.get("client_duration_ms") for t in tasks
                        if t.get("client_duration_ms") and float(t["client_duration_ms"]) > 0]
    client_stats = stats(client_durations) if client_durations else {}

    total_input = sum(t["summary"].get("total_input_tokens", 0) for t in tasks)
    total_output = sum(t["summary"].get("total_output_tokens", 0) for t in tasks)
    total_cached = sum(t["summary"].get("cached_input_tokens", 0) for t in tasks)
    total_llm_calls = sum(t["summary"].get("llm_call_count", 0) for t in tasks)
    total_tool_calls = sum(t["summary"].get("tool_call_count", 0) for t in tasks)

    # 节点耗时热图（含节点内 LLM 微观指标）
    node_stats: Dict[str, List[float]] = defaultdict(list)
    node_err: Dict[str, int] = defaultdict(int)
    node_invocations: Dict[str, int] = defaultdict(int)
    node_llm: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"llm_calls": 0, "mcp_calls": 0, "input": 0, "output": 0,
                 "cached": 0, "dur": 0.0, "errors": 0}
    )
    # 节点内 per-agent LLM 明细（跨任务按节点名聚合，key 用 agent 名）
    node_agents: Dict[str, Dict[str, Any]] = defaultdict(dict)
    for t in tasks:
        for name, d in t["per_node"].items():
            node_stats[name].append(d["duration_ms"])
            # 调用次数语义：单次任务内该节点被调用的次数（city_plan = 并发城市数）
            node_invocations[name] += d.get("invocations", 1)
            if d["status"] != "ok":
                node_err[name] += 1
            llm = d.get("llm", {})
            node_llm[name]["llm_calls"] += llm.get("calls", 0)
            node_llm[name]["mcp_calls"] += d.get("mcp_calls", 0)
            node_llm[name]["input"] += llm.get("input_tokens", 0)
            node_llm[name]["output"] += llm.get("output_tokens", 0)
            node_llm[name]["cached"] += llm.get("cached_input_tokens", 0)
            node_llm[name]["dur"] += llm.get("duration_ms", 0)
            node_llm[name]["errors"] += llm.get("error_count", 0)
            for a in d.get("llm_agents", []):
                agent = a.get("agent") or "unknown"
                item = node_agents[name].get(agent)
                if item is None:
                    item = {
                        "agent": agent, "model": a.get("model") or get_agent_model(agent),
                        "calls": 0, "input_tokens": 0, "output_tokens": 0,
                        "cached_input_tokens": 0, "duration_ms": 0.0, "error_count": 0,
                    }
                    node_agents[name][agent] = item
                item["calls"] += a.get("calls", 0)
                item["input_tokens"] += a.get("input_tokens", 0)
                item["output_tokens"] += a.get("output_tokens", 0)
                item["cached_input_tokens"] += a.get("cached_input_tokens", 0)
                item["duration_ms"] += a.get("calls", 0) * a.get("duration_ms", 0)
                item["error_count"] += a.get("error_count", 0)
    node_heatmap = []
    for name, durs in node_stats.items():
        s = stats(durs)
        l = node_llm[name]
        # 该节点的 per-agent 明细：耗时取整到 2 位、重算缓存命中率、按 token 量降序
        agents = list(node_agents.get(name, {}).values())
        for ag in agents:
            ag["duration_ms"] = round(ag["duration_ms"] / ag["calls"], 2) if ag["calls"] else 0.0
            ag["cache_hit_rate"] = round(ag["cached_input_tokens"] / ag["input_tokens"] * 100, 1) if ag["input_tokens"] else 0.0
        agents.sort(key=lambda x: x["input_tokens"] + x["output_tokens"], reverse=True)
        node_heatmap.append({
            "node": name, "mean_ms": s["mean"], "p95_ms": s["p95"],
            "count": node_invocations.get(name, s["count"]), "errors": node_err.get(name, 0),
            "llm_calls": l["llm_calls"],
            "mcp_calls": l["mcp_calls"],
            "llm_input_tokens": l["input"],
            "llm_output_tokens": l["output"],
            "llm_cached_tokens": l["cached"],
            "llm_duration_ms": round(l["dur"] / l["llm_calls"], 2) if l["llm_calls"] else 0.0,
            "llm_errors": l["errors"],
            "llm_cache_hit_rate": round(l["cached"] / l["input"] * 100, 1) if l["input"] else 0.0,
            "llm_agents": agents,
        })
    node_heatmap.sort(key=lambda x: x["mean_ms"], reverse=True)

    # 节点 MCP 工具调用聚合（跨任务，按 node 名 + server/tool 分组）
    agg_node_tool: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "dur": 0.0, "retries": 0}
    )
    for t in tasks:
        for name, d in t["per_node"].items():
            for tool in d.get("tools", []):
                key = (name, f"{tool['server']}/{tool['tool']}")
                agg_node_tool[key]["calls"] += tool.get("calls", 0)
                agg_node_tool[key]["errors"] += tool.get("errors", 0)
                agg_node_tool[key]["dur"] += tool.get("duration_ms", 0)
                agg_node_tool[key]["retries"] += tool.get("retries", 0)
    node_tools = []
    for (name, tool), v in agg_node_tool.items():
        node_tools.append({
            "node": name,
            "tool": tool,
            "calls": int(v["calls"]),
            "errors": int(v["errors"]),
            "error_rate": round(v["errors"] / v["calls"] * 100, 1) if v["calls"] else 0.0,
            "total_ms": round(v["dur"], 2),
            "avg_ms": round(v["dur"] / v["calls"], 2) if v["calls"] else 0.0,
            "retries": int(v["retries"]),
        })
    node_tools.sort(key=lambda x: (x["node"], -x["calls"]))

    # Agent Token 成本
    agg_agent: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"input": 0, "output": 0, "calls": 0, "dur": 0.0}
    )
    for t in tasks:
        for agent, d in t["per_agent"].items():
            agg_agent[agent]["input"] += d["input_tokens"]
            agg_agent[agent]["output"] += d["output_tokens"]
            agg_agent[agent]["calls"] += d["call_count"]
            agg_agent[agent]["dur"] += d["duration_ms"]
    total_tokens = sum(v["input"] + v["output"] for v in agg_agent.values())
    agent_cost = []
    for agent, v in agg_agent.items():
        tok = v["input"] + v["output"]
        agent_cost.append({
            "agent": agent,
            "tokens": int(tok),
            "pct": round(tok / total_tokens * 100, 1) if total_tokens else 0.0,
            "calls": int(v["calls"]),
            "duration_ms": round(v["dur"], 2),
        })
    agent_cost.sort(key=lambda x: x["tokens"], reverse=True)

    # 模型 Token 成本（跨任务聚合，按总 token 降序）
    agg_model: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cached": 0, "calls": 0}
    )
    for t in tasks:
        for model, d in t["per_model"].items():
            agg_model[model]["input"] += d["input_tokens"]
            agg_model[model]["output"] += d["output_tokens"]
            agg_model[model]["cached"] += d.get("cached_input_tokens", 0)
            agg_model[model]["calls"] += d["call_count"]
    model_cost = []
    for model, v in agg_model.items():
        inp = int(v["input"])
        model_cost.append({
            "model": model,
            "input_tokens": inp,
            "output_tokens": int(v["output"]),
            "cached_tokens": int(v["cached"]),
            "total_tokens": inp + int(v["output"]),
            "calls": int(v["calls"]),
            "cache_hit_rate": round(v["cached"] / inp * 100, 1) if inp else 0.0,
        })
    model_cost.sort(key=lambda x: x["total_tokens"], reverse=True)

    # 工具可靠性
    agg_tool: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "dur": 0.0, "retries": 0}
    )
    for t in tasks:
        for key, v in t["per_tool"].items():
            agg_tool[key]["calls"] += v["call_count"]
            agg_tool[key]["errors"] += v["error_count"]
            agg_tool[key]["dur"] += v["duration_ms"]
            agg_tool[key]["retries"] += v.get("retries", 0)
    tool_reliability = []
    for key, v in agg_tool.items():
        tool_reliability.append({
            "tool": key,
            "calls": int(v["calls"]),
            "errors": int(v["errors"]),
            "error_rate": round(v["errors"] / v["calls"] * 100, 1) if v["calls"] else 0.0,
            "avg_duration_ms": round(v["dur"] / v["calls"], 2) if v["calls"] else 0.0,
            "retries": int(v["retries"]),
        })
    tool_reliability.sort(key=lambda x: (-x["error_rate"], -x["calls"]))

    # 重规划统计
    fix_calls = 0
    route_calls = 0
    for t in tasks:
        for agent, d in t["per_agent"].items():
            if agent == "json_fix":
                fix_calls += d["call_count"]
            if agent == "route_plan":
                route_calls += d["call_count"]
    plan_tasks = sum(1 for t in tasks if t["per_agent"].get("route_plan"))
    replan_count = max(0, route_calls - plan_tasks)
    replan_rate = round(replan_count / plan_tasks * 100, 1) if plan_tasks else 0.0

    # 意图识别统计：有 intent 标注的任务中，classify 分类结果 query_type 与标注一致的比例
    intent_total = 0
    intent_match = 0
    intent_by_type: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "match": 0})
    for t in tasks:
        it = (t.get("intent") or "").strip()
        if not it:
            continue
        intent_total += 1
        qt = (t.get("query_type") or "").strip()
        if qt == it:
            intent_match += 1
        intent_by_type[it]["total"] += 1
        if qt == it:
            intent_by_type[it]["match"] += 1
    intent_success_rate = round(intent_match / intent_total * 100, 1) if intent_total else 0.0
    intent_detail = [
        {
            "intent": it,
            "total": v["total"],
            "match": v["match"],
            "rate": round(v["match"] / v["total"] * 100, 1) if v["total"] else 0.0,
        }
        for it, v in sorted(intent_by_type.items())
    ]

    # 错误样本
    samples = []
    for t in tasks:
        ts = datetime.fromtimestamp(t["start_ts"]).strftime("%m-%d %H:%M") if t["start_ts"] else "?"
        if t.get("error"):
            samples.append({"time": ts, "type": "TASK", "message": (t["error"] or "")[:200]})
        for e in t["llm_errors"][:3]:
            samples.append({"time": ts, "type": f"LLM:{e['agent']}", "message": e["error"][:200]})
        for e in t["tool_errors"][:3]:
            samples.append({"time": ts, "type": f"TOOL:{e['tool']}", "message": e["error"][:200]})

    return {
        "task_count": total,
        "success_count": ok_count,
        "error_count": error_count,
        "running_count": running_count,
        "success_rate": round(ok_count / total * 100, 1),
        "duration": wall,
        "client_duration": client_stats,
        "tokens": {
            "input": total_input,
            "output": total_output,
            "cached": total_cached,
            "total": total_input + total_output,
            "cache_hit_rate": round(total_cached / total_input * 100, 1) if total_input else 0.0,
        },
        "intent_recognition": {
            "total": intent_total,
            "match": intent_match,
            "success_rate": intent_success_rate,
            "detail": intent_detail,
        },
        "llm_call_count": total_llm_calls,
        "tool_call_count": total_tool_calls,
        "node_heatmap": node_heatmap,
        "node_tools": node_tools,
        "agent_cost": agent_cost,
        "model_cost": model_cost,
        "agent_total_tokens": int(total_tokens),
        "tool_reliability": tool_reliability,
        "replanner": {
            "json_fix_calls": fix_calls,
            "route_plan_calls": route_calls,
            "plan_task_count": plan_tasks,
            "replan_count": replan_count,
            "replan_rate": replan_rate,
        },
        "error_samples": samples[-20:],
    }


# ──────────────────────────────────────────────────────────────
# 报表渲染（文本，供 CLI 使用）
# ──────────────────────────────────────────────────────────────
def render_overview(tasks: List[Dict[str, Any]]) -> str:
    if not tasks:
        return "（无数据）"
    total = len(tasks)
    ok_count = sum(1 for t in tasks if t["status"] == "ok")
    err_count = total - ok_count
    durations = [t["duration_ms"] for t in tasks if t["duration_ms"] > 0]
    wall = stats(durations)

    total_input = sum(t["summary"].get("total_input_tokens", 0) for t in tasks)
    total_output = sum(t["summary"].get("total_output_tokens", 0) for t in tasks)
    total_cached = sum(t["summary"].get("cached_input_tokens", 0) for t in tasks)
    cache_rate = round(total_cached / total_input * 100, 1) if total_input else 0.0
    total_llm_calls = sum(t["summary"].get("llm_call_count", 0) for t in tasks)
    total_tool_calls = sum(t["summary"].get("tool_call_count", 0) for t in tasks)
    avg_llm_per_task = round(total_llm_calls / total, 1) if total else 0
    avg_tool_per_task = round(total_tool_calls / total, 1) if total else 0

    lines = [
        "═" * 64,
        "📊 观测分析报表（总览）",
        "═" * 64,
        f"  任务样本数：{total}  "
        f"（✅ {ok_count} / ❌ {err_count}，成功率 {ok_count/total*100:.1f}%）",
        f"  时间范围：  "
        f"{datetime.fromtimestamp(tasks[0]['start_ts']).strftime('%m-%d %H:%M') if tasks[0]['start_ts'] else '?'} "
        f"→ "
        f"{datetime.fromtimestamp(tasks[-1]['start_ts']).strftime('%m-%d %H:%M') if tasks[-1]['start_ts'] else '?'}",
        "",
        "  ── 任务耗时（wall-clock）──",
        f"    平均 {fmt_ms(wall['mean'])}  |  P50 {fmt_ms(wall['p50'])}  "
        f"|  P95 {fmt_ms(wall['p95'])}  |  P99 {fmt_ms(wall['p99'])}  |  最长 {fmt_ms(wall['max'])}",
        "",
        "  ── Token 消耗 ──",
        f"    输入 {fmt_tokens(total_input)} + 输出 {fmt_tokens(total_output)} "
        f"= 共 {fmt_tokens(total_input+total_output)}  tokens",
        f"    缓存命中 {fmt_tokens(total_cached)} / 输入 {fmt_tokens(total_input)} "
        f"= 命中率 {cache_rate:.1f}%",
        f"    平均每任务：{fmt_tokens((total_input+total_output)//total)} tokens  "
        f"（{avg_llm_per_task} 次 LLM 调用 / {avg_tool_per_task} 次工具调用）",
    ]

    budget_tasks = [t for t in tasks if t.get("total_budget", 0) > 0]
    if budget_tasks:
        over = sum(1 for t in budget_tasks if t.get("over_budget"))
        total_b = sum(t.get("total_budget", 0) for t in budget_tasks)
        spent_b = sum(t.get("spent_budget", 0) for t in budget_tasks)
        lines += [
            "",
            "  ── 预算执行（仅规划类任务）──",
            f"    样本 {len(budget_tasks)} 条，超预算触发 {over} 次（触发率 {over/len(budget_tasks)*100:.1f}%）",
            f"    预算合计 {total_b:.0f} 元，实际花费 {spent_b:.0f} 元，"
            f"执行率 {spent_b/total_b*100:.1f}%",
        ]
    return "\n".join(lines)


def render_node_heatmap(tasks: List[Dict[str, Any]]) -> str:
    if not tasks:
        return ""
    node_stats: Dict[str, List[float]] = defaultdict(list)
    node_err: Dict[str, int] = defaultdict(int)
    for t in tasks:
        for name, d in t["per_node"].items():
            node_stats[name].append(d["duration_ms"])
            if d["status"] != "ok":
                node_err[name] += 1
    if not node_stats:
        return ""

    rows = []
    for name, durs in node_stats.items():
        s = stats(durs)
        rows.append((name, s["mean"], s["p95"], s["count"], node_err.get(name, 0)))
    rows.sort(key=lambda x: x[1], reverse=True)

    max_mean = rows[0][1] if rows else 1
    lines = [
        "",
        "═" * 64,
        "🔥 节点耗时热图（按平均耗时排序）",
        "═" * 64,
        f"  {'节点':<28} {'平均':>8} {'P95':>8} {'次数':>6} {'错误':>5}  耗时分布",
        f"  {'─'*28} {'─'*8} {'─'*8} {'─'*6} {'─'*5}  {'─'*20}",
    ]
    for name, mean, p95, count, errs in rows:
        lines.append(
            f"  {name:<28} {fmt_ms(mean):>8} {fmt_ms(p95):>8} "
            f"{count:>6} {errs:>5}  {bar(mean, max_mean)}"
        )
    return "\n".join(lines)


def render_agent_cost(tasks: List[Dict[str, Any]]) -> str:
    if not tasks:
        return ""
    agg: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"input": 0, "output": 0, "calls": 0, "dur": 0.0}
    )
    for t in tasks:
        for agent, d in t["per_agent"].items():
            agg[agent]["input"] += d["input_tokens"]
            agg[agent]["output"] += d["output_tokens"]
            agg[agent]["calls"] += d["call_count"]
            agg[agent]["dur"] += d["duration_ms"]
    if not agg:
        return ""

    total_tokens = sum(v["input"] + v["output"] for v in agg.values())
    rows = []
    for agent, v in agg.items():
        tok = v["input"] + v["output"]
        pct = tok / total_tokens * 100 if total_tokens else 0
        rows.append((agent, tok, pct, v["calls"], v["dur"]))
    rows.sort(key=lambda x: x[1], reverse=True)

    max_tok = rows[0][1]
    lines = [
        "",
        "═" * 64,
        "💰 Token 成本按 Agent 拆解（降序）",
        "═" * 64,
        f"  {'Agent':<24} {'Tokens':>8} {'占比':>6} {'调用':>6} {'累计耗时':>10}  成本分布",
        f"  {'─'*24} {'─'*8} {'─'*6} {'─'*6} {'─'*10}  {'─'*20}",
    ]
    for agent, tok, pct, calls, dur in rows:
        lines.append(
            f"  {agent:<24} {fmt_tokens(int(tok)):>8} {pct:>5.1f}% "
            f"{calls:>6} {fmt_ms(dur):>10}  {bar(tok, max_tok)}"
        )
    lines.append(f"\n  合计 Token：{fmt_tokens(int(total_tokens))}")
    return "\n".join(lines)


def render_tool_errors(tasks: List[Dict[str, Any]]) -> str:
    if not tasks:
        return ""
    agg: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "dur": 0.0}
    )
    for t in tasks:
        for key, v in t["per_tool"].items():
            agg[key]["calls"] += v["call_count"]
            agg[key]["errors"] += v["error_count"]
            agg[key]["dur"] += v["duration_ms"]
    if not agg:
        return ""

    rows = []
    for key, v in agg.items():
        err_rate = v["errors"] / v["calls"] * 100 if v["calls"] else 0
        avg_dur = v["dur"] / v["calls"] if v["calls"] else 0
        rows.append((key, int(v["calls"]), int(v["errors"]), err_rate, avg_dur))
    rows.sort(key=lambda x: (-x[3], -x[1]))

    lines = [
        "",
        "═" * 64,
        "🛠  MCP 工具可靠性（错误率降序）",
        "═" * 64,
        f"  {'工具':<30} {'调用':>6} {'错误':>6} {'错误率':>7} {'平均耗时':>9}",
        f"  {'─'*30} {'─'*6} {'─'*6} {'─'*7} {'─'*9}",
    ]
    for key, calls, errs, rate, dur in rows:
        flag = " ⚠️" if rate >= 5 else ""
        lines.append(
            f"  {key:<30} {calls:>6} {errs:>6} {rate:>6.1f}% {fmt_ms(dur):>9}{flag}"
        )
    return "\n".join(lines)


def render_error_sample(tasks: List[Dict[str, Any]]) -> str:
    samples: List[Tuple[str, str, str]] = []
    for t in tasks:
        ts = datetime.fromtimestamp(t["start_ts"]).strftime("%m-%d %H:%M") if t["start_ts"] else "?"
        if t.get("error"):
            samples.append((ts, "TASK", t["error"][:80]))
        for e in t["llm_errors"][:3]:
            samples.append((ts, f"LLM:{e['agent']}", e["error"][:80]))
        for e in t["tool_errors"][:3]:
            samples.append((ts, f"TOOL:{e['tool']}", e["error"][:80]))
    if not samples:
        return "\n✅ 没有任何错误记录，系统运行良好！"

    samples = samples[-10:]
    lines = [
        "",
        "═" * 64,
        "❌ 最近 10 条错误样本",
        "═" * 64,
    ]
    for ts, kind, msg in samples:
        lines.append(f"  [{ts}] {kind:<14} {msg}")
    return "\n".join(lines)


def render_replanner_stats(tasks: List[Dict[str, Any]]) -> str:
    if not tasks:
        return ""
    fix_calls = 0
    route_calls = 0
    for t in tasks:
        for agent, d in t["per_agent"].items():
            if agent == "json_fix":
                fix_calls += d["call_count"]
            if agent == "route_plan":
                route_calls += d["call_count"]
    plan_tasks = sum(1 for t in tasks if t["per_agent"].get("route_plan"))
    replan_rate = 0.0
    if plan_tasks and route_calls > plan_tasks:
        replan_rate = (route_calls - plan_tasks) / plan_tasks * 100
    lines = [
        "",
        "═" * 64,
        "🔄 重试 & 重规划统计",
        "═" * 64,
        f"  JSON 格式修正触发：{fix_calls} 次（覆盖 {len(tasks)} 任务，平均每任务 {fix_calls/max(len(tasks),1):.1f} 次）",
        f"  路线规划重规划触发：{max(0, route_calls - plan_tasks)} 次 "
        f"（规划任务 {plan_tasks} 条，重规划率 {replan_rate:.1f}%）",
    ]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# CSV 导出
# ──────────────────────────────────────────────────────────────
def export_csv(tasks: List[Dict[str, Any]], out_path: Path):
    """按「每行 = 一个任务」导出平铺 CSV。"""
    import csv
    out_path.parent.mkdir(parents=True, exist_ok=True)
    headers = [
        "task_id", "start_time", "user_query", "status", "duration_ms",
        "node_count", "llm_call_count", "tool_call_count",
        "total_input_tokens", "total_output_tokens", "total_tokens",
        "cache_hit_rate", "llm_duration_ms", "tool_duration_ms",
        "over_budget", "total_budget", "spent_budget",
        "llm_error_count", "tool_error_count",
    ]
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for t in tasks:
            s = t["summary"]
            st = datetime.fromtimestamp(t["start_ts"]).strftime("%Y-%m-%d %H:%M:%S") if t["start_ts"] else ""
            w.writerow([
                t["task_id"], st, (t["user_query"] or "")[:100], t["status"],
                int(t["duration_ms"]),
                s.get("node_count", 0), s.get("llm_call_count", 0), s.get("tool_call_count", 0),
                s.get("total_input_tokens", 0), s.get("total_output_tokens", 0),
                s.get("total_tokens", 0), s.get("cache_hit_rate", ""),
                s.get("llm_duration_ms", 0), s.get("tool_duration_ms", 0),
                t.get("over_budget", ""), t.get("total_budget", ""), t.get("spent_budget", ""),
                len(t["llm_errors"]), len(t["tool_errors"]),
            ])
    print(f"\n📄 CSV 已导出到：{out_path}（{len(tasks)} 行）")


# ──────────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="旅行代理可观测数据分析")
    parser.add_argument("--limit", type=int, default=50, help="DB 模式读取最近 N 条任务（默认 50）")
    parser.add_argument("--json-only", action="store_true", help="只分析本地 JSON（不连 DB）")
    parser.add_argument("--csv", type=str, default=None, help="导出 CSV 文件路径")
    args = parser.parse_args()

    if args.json_only:
        print("📂 数据源：本地 JSON（仅最新任务）")
        tasks = load_from_json()
    else:
        print(f"🗄️  数据源：PostgreSQL 最近 {args.limit} 条任务")
        tasks = load_from_db(limit=args.limit)
        if not tasks:
            print("→ DB 无数据，回退到本地 JSON")
            tasks = load_from_json()

    if not tasks:
        print("❌ 没有可分析的数据。请先运行至少一次规划，或检查 DB 连接。")
        sys.exit(1)

    print(render_overview(tasks))
    print(render_node_heatmap(tasks))
    print(render_agent_cost(tasks))
    print(render_tool_errors(tasks))
    print(render_replanner_stats(tasks))
    print(render_error_sample(tasks))
    print("\n" + "═" * 64)

    if args.csv:
        export_csv(tasks, Path(args.csv))


if __name__ == "__main__":
    main()
