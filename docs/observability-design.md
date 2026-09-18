# 可观测性设计：通用 Span 树 + 双格式输出（最终方案）

> 状态：**已落地（2026-09 对齐运行态存储）**
> 权威补充：`docs/runtime-storage-and-mq.md`（增量查询、LLM 版本/载荷、Redis 热状态、MQ 裁剪）
> 关联：`docs/traceability-design.md`（早期草稿，以本文 + runtime 文档为准）
> 已废止：`docs/observability-architecture.md`（聚合模型 ADR，与现行 span 树不符）
> 目标读者：项目作者 / 面试评审 / 协作者

---

## 1. 背景与动机

### 1.1 核心需求：可追溯 + 可扩展

做旅行规划这种多 Agent、多 LLM 调用、多 MCP 外部依赖的系统，需要：

- **可追溯**：每个用户问题，能看清它走了哪些节点、每个节点调用了哪些 LLM/MCP、每次调用的 token / 耗时 / 结果 / 异常。
- **可扩展**：当前是 `task → node → llm/mcp` 三层；未来可能扩展（矫正嵌套、子规划嵌套、逻辑分组），**结构必须支持任意层级**。

### 1.2 演进历史

| 阶段 | 方案 | 结果 |
|---|---|---|
| 早期 | 自研 span 树（`obs_spans` 等） | 曾实现，后废弃 |
| 中期 | 评估 Langfuse / LangSmith（本地自托管） | 资源约束（Docker 7.6G）+ 镜像获取难，不适用 |
| 现在 | **自研通用 span 树（uuid span_id + parent_id + 双格式输出）** | 本方案 |

### 1.3 为什么最终不选 Langfuse 容器部署

1. **资源约束（硬性）**：Langfuse 依赖 web + worker + ClickHouse + Postgres + Redis + S3，ClickHouse 是 OLAP 库，当前机器 Docker 配额 7.6G 无法承载。
2. **规模不匹配**：每版本几百条任务，自建窄表足够，ClickHouse 是过度设计。
3. **数据自主 + 可扩展**：需要一个轻量、能实际跑、可复用到后续其他 agent 的观测底座。
4. **面试价值**：自研 span 树能实际运行、能讲清设计、能写简历。

---

## 2. 设计目标

1. **轻量可运行**：复用现有 PostgreSQL，新增窄表，不引入额外服务。
2. **逐调用可追溯**：node / llm / mcp 每次调用一条记录，含 task_id / 起止时间 / token / result / 异常。
3. **可扩展**：通用 span 树（uuid span_id + parent_id），支持任意层级，为未来扩展铺路。
4. **双格式输出**：点分路径（结构化定位），大 JSON（前端/人看）。
5. **面试可讲**：从需求 → 设计 → 实现 → 权衡的完整闭环。

---

## 3. 核心设计

### 3.1 任务 id：obs_tasks 前置插入（uuid 主键）

- **task_id = uuid 主键**：与业务 task_id / LangGraph checkpoint thread_id 完全一致。
- 任务一开始就 INSERT obs_tasks（uuid 占位），崩溃/重启不丢失任务入口。
- 无自增 id（uuid 已保证唯一，自增无引用价值）。

### 3.2 数据模型：按「采集时机」分窄表 + 通用层级（uuid span_id + parent_id）

> 核心决策：
> - **不用一张大表**（不同 span_type 字段不同，单表平铺大量 NULL），按"一次采集能同时拿到的数据"拆表。
> - **每张 span 表用 uuid `span_id` 作主键 + `parent_id` 引用父 span_id**，支持任意层级，为可扩展性设计。

**统一模式（三张子表一致）**：
- 无 `seq`：顺序由 `start_ts` 推导。
- 无 `duration_ms`：由 `end_ts - start_ts` 推导。
- `span_id`（uuid）作主键：**全局唯一**（跨表不冲突），代码生成（python uuid4）。
- `parent_id`（uuid）引用父 span_id：`task → node → llm/mcp` 任意层级；node 的 parent 为 NULL（根级）。
- 统一 result 双列：`result_kind` + `result` + `error_what`。`obs_tasks.result` 不是业务 JSON，业务结果在 `result_payload`。
- 统一「开始占位 + 结束补全」：start/end 都走 `INSERT ... ON CONFLICT DO UPDATE`。乱序可接受；已有 `output` / payload.`input` 时，空值或 `running` 不得覆盖。
- span 二级索引只有 `(task_id, start_ts)`。无时间下限时 `since_ts=0` 拉全量，不另建单列 `task_id`。

```sql
-- ① 任务表：task_id=uuid 主键
CREATE TABLE IF NOT EXISTS obs_tasks (
    task_id         TEXT PRIMARY KEY,
    user_query      TEXT,
    user_id         TEXT,
    session_id      TEXT,
    version         TEXT,
    intent          TEXT,
    query_type      TEXT,
    result_kind     TEXT NOT NULL DEFAULT 'running',
    result          TEXT NOT NULL DEFAULT 'running',
    error_what      TEXT,
    client_duration_ms DOUBLE PRECISION,
    result_payload  TEXT,                     -- 业务结果 JSON；result 列仍是粗粒度状态
    start_ts        DOUBLE PRECISION NOT NULL,
    end_ts          DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_obs_tasks_user_ts ON obs_tasks(user_id, start_ts DESC);

-- ② 节点表：span_id=uuid 主键，parent_id 引用父 span_id（根级 NULL）
CREATE TABLE IF NOT EXISTS obs_node_spans (
    span_id         TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES obs_tasks(task_id),
    parent_id       TEXT,
    node            TEXT NOT NULL,
    result_kind     TEXT NOT NULL DEFAULT 'running',
    result          TEXT NOT NULL DEFAULT 'running',
    error_what      TEXT,
    start_ts        DOUBLE PRECISION NOT NULL,
    end_ts          DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_node_spans_task_ts ON obs_node_spans(task_id, start_ts);

-- ③ LLM 表：span_id=uuid 主键，parent_id 引用父 span_id
CREATE TABLE IF NOT EXISTS obs_llm_spans (
    span_id         TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES obs_tasks(task_id),
    parent_id       TEXT,
    node            TEXT NOT NULL,
    agent           TEXT NOT NULL,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cached_tokens   INTEGER DEFAULT 0,
    output          TEXT,
    prompt_id       TEXT,
    prompt_version  TEXT,
    result_kind     TEXT NOT NULL DEFAULT 'running',
    result          TEXT NOT NULL DEFAULT 'running',
    error_what      TEXT,
    start_ts        DOUBLE PRECISION NOT NULL,
    end_ts          DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_llm_spans_task_ts ON obs_llm_spans(task_id, start_ts);

-- ④ MCP 表：span_id=uuid 主键，parent_id 引用父 span_id
CREATE TABLE IF NOT EXISTS obs_mcp_spans (
    span_id         TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES obs_tasks(task_id),
    parent_id       TEXT,
    node            TEXT NOT NULL,
    server          TEXT,
    tool            TEXT,
    retries         INTEGER DEFAULT 0,
    result_kind     TEXT NOT NULL DEFAULT 'running',
    result          TEXT NOT NULL DEFAULT 'running',
    error_what      TEXT,
    start_ts        DOUBLE PRECISION NOT NULL,
    end_ts          DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_mcp_spans_task_ts ON obs_mcp_spans(task_id, start_ts);

-- ⑤ 提示词模板目录（变更才插入，不随调用膨胀）
CREATE TABLE IF NOT EXISTS prompt_versions (
    prompt_id     TEXT NOT NULL,
    version       TEXT NOT NULL,
    content       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (prompt_id, version)
);

-- ⑥ 一次 LLM 调用的渲染后 input（1:1 span_id）
CREATE TABLE IF NOT EXISTS obs_llm_payloads (
    span_id     TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES obs_tasks(task_id),
    input       TEXT,
    truncated   BOOLEAN NOT NULL DEFAULT FALSE,
    start_ts    DOUBLE PRECISION NOT NULL
);
```

**设计要点**：
- **span_id（uuid）全局唯一**：跨表（node/llm/mcp）不冲突，`parent_id` 能唯一确定父 span。这是自增 id 做不到的（三表各自从 1 开始，无法区分父是哪个表）。
- **通用层级（可扩展）**：`task → node → llm/mcp` 是当前 3 层；未来矫正嵌套（llm 下挂 llm）、子规划（node 下挂子 node）只需设置 `parent_id`，无需改表结构。
- **LLM 模型名不存表**：只存 `agent`，合并时按配置反查模型名。
- **LLM 存输出字符串**：存模型返回原文，供复盘。
- 统一幂等 upsert 补全，`duration_ms = end_ts - start_ts`。
- LLM 输入：模板在 `prompt_versions`，渲染后全文在 `obs_llm_payloads`；`obs_llm_spans` 只存 `output` + 版本指针。

### 3.3 span 结果状态：双列（粗粒度 kind + 原因类型）

| 列 | 作用 | 取值 |
|---|---|---|
| `result_kind` | 粗粒度（可索引） | `running` / `ok` / `error` |
| `result` | 细粒度（具体原因，不含环节名） | `ok` / `degraded` / `timeout` / `401` / `auth_failed` / `rate_limited` / `parse_error` / `connection_error` |
| `error_what` | 具体错误消息 | "意图分类失败，降级为 travel: ..." |

**result_kind ↔ result 组合**：
| result_kind | result | 含义 |
|---|---|---|
| `running` | `running` | 进行中（占位） |
| `ok` | `ok` | 完全成功 |
| `ok` | `degraded` | 降级成功（软失败但继续） |
| `error` | `timeout`/`401`/`rate_limited`/... | 各类致命错误 |

- `result` 是具体原因，**不含环节名**（`llm`/`mcp`/`node`）：因为 span 存哪张表就决定了环节，`result` 只存原因。
- 枚举跨表通用，新增原因只需加值。
- `result_kind` 由 `result` 统一推导（`_derive_kind`），不手工分别赋值。

**查询**（走索引、可聚合）：
```sql
WHERE result_kind = 'error'
WHERE result_kind = 'error' AND result = 'timeout'
WHERE result_kind = 'ok' AND result = 'degraded'
SELECT result, COUNT(*) FROM obs_xxx_spans WHERE result_kind='error' GROUP BY result
```

### 3.4 写入链路：span 栈（栈顶作 parent）

**核心机制**：每个任务维护一个运行时 span 栈（contextvars），栈顶即"当前父 span"。任何 span 创建时，自动以栈顶为 parent。

```
栈 = []                    # 任务开始
栈 = [node]                # node start → push
栈 = [node, llm]           # 嵌套调用 start → push，子调用的 parent = llm
栈 = [node]                # 嵌套调用 end → pop
```

- 与业界 OTel / Langfuse 一致：父子 = 生命周期包含（父 start~end 之间创建的 span 即其子）。
- 栈存 contextvars（`_span_stack`），`asyncio.gather` 多城并发时各任务栈独立。
- `span_id` 用 uuid（代码生成），`parent_id` = 栈顶 span_id。

### 3.5 矫正调用建模：移入 `_LLM` 封装（`parse_json`）

**场景**：LLM 返回坏 JSON → flash 小模型矫正。

**结论**：矫正调用应成为主 LLM 的子 span（不是兄弟）。

**实现**：`_LLM(parse_json=True)` 时，`_tracked_ainvoke` 拿到输出后自动 `extract_json_block` + `json.loads`，失败用 flash 矫正。矫正发生在主 LLM span 还在栈上时 → 成为其子 span。

```python
_LLM(agent="planner", parse_json=True).ainvoke([...])
# 内部：主 LLM span start(push) → 解析失败 → flash 矫正（parent=主 LLM）→ 返回 _AIMessageFromDict(dict)
```

- `parse_json=True` 返回解析后的 dict（包装为 `_AIMessageFromDict`，`.content` 是 JSON 字符串，`.get()` 可取字段）。
- 矫正多次失败抛 ValueError，span 记 `result='parse_error'`。

### 3.6 双格式输出

> 核心需求：**一套底层数据，两种输出格式**——点分路径（精确定位一次调用的位置，适合程序/结构化消费），大 JSON（人看监控可视化）。

**格式 A：点分路径**
```
task_id.node.llm/mcp
```
示例：
- `abc123.classify.classify_llm`
- `abc123.transport_check.train_query`
- `abc123.city_planning.city_plan:北京`

- 支持任意层级（`task.node.subnode.tool`），点分路径天然可扩展深度。
- 生成：从 span 树按 parent_id 递归拼接。

**格式 B：大 JSON（给前端/人看）**
```json
{
  "task_id": "abc123",
  "summary": {"node_count": 5, "llm_calls": 8, "tokens": 12000, "duration_ms": 8200,
              "result_kind": "ok"},
  "nodes": [
    {
      "node": "classify",
      "span_id": "node-1",
      "result_kind": "ok",
      "duration_ms": 300,
      "llm": [{"agent": "classify", "tokens": 500, "duration_ms": 300, "result": "ok", "parent_id": "node-1"}],
      "tools": []
    },
    {
      "node": "transport_check",
      "result_kind": "ok",
      "llm": [{"agent": "transport", "tokens": 800, "duration_ms": 600}],
      "tools": [{"tool": "train_query", "duration_ms": 1200, "result": "ok"}]
    }
  ],
  "model_map": {"classify": "deepseek-v4-pro"}
}
```

- 从 span 树按 parent_id 递归生成（或按 node 聚合）。
- 人看监控可视化，扁平清晰。
- `model_map`：模型名按 agent 从配置反查。

### 3.7 对外接口

| 接口 | 说明 |
|---|---|
| `GET /internal/obs/{task_id}` | 返回任务 summary + 大 JSON（格式 B） |
| `GET /internal/obs/{task_id}/trace` | 返回完整 span 树（含点分路径，格式 A + B） |
| gateway 透传 | `GET /obs/{task_id}/trace`（需 JWT） |

### 3.8 span 栈与 checkpoint 的关系（职责分离）

- **checkpoint**（`core/checkpoint.py`，AsyncPostgresSaver）：存 LangGraph 图业务状态（GlobalState），用于断点续跑。thread_id = 业务 task_id。
- **span 栈**：运行时观测上下文（contextvars），不持久化。
- 不需要把 span 栈入 checkpoint：续跑重跑的节点，`node` 装饰器重新执行，自动重建上下文。
- 续跑产生的 span 与首次执行按 `start_ts` 时间顺序区分，不显式标记 attempt。

---

## 4. 实现范围

**已落地**：
1. uuid `span_id` + `parent_id` 通用层级；start/end 幂等 upsert
2. `_SpanWriteBuffer` 批量 flush；`end_task` 前强制 flush
3. `prompt_versions` + `obs_llm_payloads`；LLM 调用自动记渲染后 input
4. span 索引 `(task_id, start_ts)`；`GET /tasks/{id}/events?since_ts=`
5. `obs_tasks.result_payload` + Redis 任务热状态（见 runtime-storage-and-mq.md）
6. `monitor/analyze.py` 按 `task_id IN (...)` 批量读三张 span 表
7. `tests/test_obs_async.py`（含乱序 upsert）

**不做**：Kafka / 观测入库走 MQ；单列 `task_id` 二级索引；`parent_table` 列。

---

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| uuid span_id 可读性差 | 顺序由 `start_ts` 推导，不依赖 id 大小 |
| 逐调用落库 DB 开销 | 写缓冲批量 flush + 幂等 upsert，乱序可接受 |
| 存储体积膨胀 | LLM 存输出原文（可接受）；如需可加开关禁用 |
| 并发归属错误 | 复用 contextvars 隔离 span 栈 |
| parent 跨表歧义 | **uuid span_id 全局唯一**，彻底解决 |
| result_kind 与 result 不一致 | 统一由 `_derive_kind` 推导 |

---

## 6. 面试叙事（直接可背）

> "这个项目我做了自研的可观测系统。核心是**通用 span 树**：一次任务是一个 trace，task_id 作 trace id，每个节点 / LLM / MCP 调用是一个 span，用 uuid span_id 作主键、parent_id 引用父 span，支持任意层级。通过 span 栈（栈顶作 parent）自动形成 task → node → llm/mcp 的层级——这是 OpenTelemetry 的标准生命周期嵌套模型。
>
> 我为可扩展性做了两个关键设计：一是 **uuid span_id + parent_id**，跨表全局唯一，未来矫正嵌套、子规划都不需要改表结构；二是**双格式输出**——点分路径（task.node.tool）精确定位一次调用的位置，大 JSON 给前端监控看，一套数据两种消费方式。
>
> 结果状态我用双列（result_kind 粗粒度 + result 原因类型）表达，能区分完全成功 / 降级成功 / 各类致命错误，且可索引可聚合。
>
> 我调研过 Langfuse / LangSmith，也实际尝试过自托管，但评估后发现它的部署是重架构（web + worker + ClickHouse + Postgres + Redis + S3），当前机器跑不起来；而项目规模用 PostgreSQL 窄表存 span 完全够。所以我选择自研，它轻量、能实际运行、可复用到后续其他 agent 功能。这个过程的收获是理解了『自研 vs 用成熟工具』要基于真实规模、资源和数据自主性权衡。"
