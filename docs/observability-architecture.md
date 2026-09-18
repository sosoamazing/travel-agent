# 观测系统架构决策记录（ADR）

> **已废止（2026-09）。** 本文记录的是「聚合平均 + Langfuse」阶段，与现行 **uuid span 树** 不符。
> 现行方案：`docs/observability-design.md` + `docs/runtime-storage-and-mq.md`。
> 下文仅作历史，勿按此实现。

## 背景与规模前提

- **并发上限约 100**（测试脚本 `--from-db N`，token 有限不会真打满）
- **每版本测试任务数：几十到几百**（当前 35/50 一批）
- 观测数据量：每版本每节点几百行明细，PG 毫秒级
- 目的：展示高并发/可观测性设计能力 + 测试数据分析

## 核心决策

### 1. 观测用「聚合模型」，不用 span 树

- 运行期用 contextvars + 内存累加器（`_accumulators`）按维度聚合
- `end_task` 时批量落库到 4 张表：`obs_tasks` / `obs_node_metrics` / `obs_llm_metrics` / `obs_mcp_metrics`
- span 树（`obs_spans` / `obs_llm_outputs` / `obs_mcp_results`）已废弃删除，代码里 shim（`get_current_llm_id` / `correction_scope`）已清理
- **单任务链路追踪交给 Langfuse**（后续接入），本系统只做跨任务聚合统计

### 2. P95 用「明细表 + PG percentile_cont」，不用滑动窗口 / t-digest

- 分位数必须基于原始样本，固定聚合表（sum/count）推不出 P95
- 当前规模（每版本几百行明细）下，PG 原生 `percentile_cont(0.95) WITHIN GROUP (ORDER BY ...)` 毫秒级返回**精确** P95
- 滑动窗口 / t-digest 是给"海量数据 + 实时看板"设计的（每秒百万条），本项目用不上，引入反而增加复杂度 + 近似误差
- **面试话术**："我评估过，当前规模 PG 原生函数足够；架构要匹配真实规模，不为炫技引入复杂度"

### 3. 不用 Redis 做跨进程指标累加

- 当前 backend 单进程（uvicorn 无 workers），进程内 dict 累加天然正确
- Redis 累加只在"多实例 / 实时跨进程指标"才有价值，本项目不需要
- **如果未来扩多实例**（`--workers N`），任务级观测仍正确（每任务完整落库到共享 PG），只有实时瞬时指标才需要 Redis

### 4. 观测落库：批量插入，不逐条

- `_obs_storage.py` 的 insert_* 方法用 `executemany` 批量插入（先构造参数列表，一次协议往返）
- 避免 `for r in rows: execute()` 的逐条往返（原来一个任务几十次 DB 写）

### 5. monitor 读取：批量 IN 查询，不做 N+1

- `load_from_db` 先查任务列表，再用 `WHERE task_id IN (...)` 一次取回三张子表，内存分组
- 避免原 N+1（每任务循环查 3 次 = 1 + N×3 次查询）

### 6. 连接池：启动预热 + 复用

- psycopg3 `AsyncConnectionPool`（min 2 / max 10）全局单例
- backend `server.py` lifespan 启动时 `_ensure_async_pool()` 预热，避免首个请求冷启动
- monitor 是独立低频只读服务，每次新建 psycopg2 连接可接受（不引入池）

### 7. 意图识别：请求带 intent，聚合对比

- 前端 /chat 携带可选 `intent`（travel/information/conversation/feedback）
- 意图**不参与路由**（classify 照常 LLM 分类），只作为"标注答案"记录
- obs_tasks 记录 `intent`（标注）+ `query_type`（实际分类），monitor 计算识别成功率

## 主动放弃的方案（及原因）

| 方案 | 为什么不做 |
|---|---|
| 滑动窗口算 P95 | 当前规模 PG percentile_cont 够用且精确；窗口有边界跳变/序列化/多进程冲突问题 |
| t-digest 近似分位数 | 引入算法依赖，近似误差，规模不匹配 |
| Redis 跨进程累加 | 单进程下进程内 dict 已正确；多实例再说 |
| 固定聚合表（version×node 一行） | 失去单任务下钻 + P95 需样本；当前明细表几百行无压力 |
| DB 操作监控指标 | DB 非瓶颈（LLM 秒级 vs DB 毫秒级）；PG 自带 `pg_stat_statements` / 慢查询日志更权威 |
| 自研 span 树 | 已砍，单任务链路用 Langfuse |

## 未来扩展路径（按需，非现在）

1. 接入 Langfuse：`_LLM` 挂 callback，`.env` 开关（测试开/生产关），看单任务 trace
2. 多实例部署：`--workers N`，任务级观测仍正确；实时指标才考虑 Redis
3. 数据量大到明细存不下：再上 t-digest 或 Prometheus（自带 histogram），不要自研
