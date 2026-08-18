# 可追溯性设计：自研 Span 树（最终方案）

> 状态：**已定方案**（设计已评审，待按此实现）
> 关联文档：`docs/observability-architecture.md`（原观测 ADR）
> 目标读者：项目作者 / 面试评审 / 协作者

---

## 1. 背景与动机

### 1.1 需求：每个问题都可追溯

做旅行规划这种多 Agent、多 LLM 调用、多 MCP 外部依赖的系统，出现"结果不对"时，需要回答：

- 用户这个问题走到了哪条分支（`classify` → `extract_params` → `transport_check` → …）
- 每个节点调用了**哪几次** LLM，每次的 agent / 模型 / token / 耗时 / 是否报错
- 每个节点调用了**哪几次** MCP，哪个 server 的哪个 tool，耗时 / 重试 / 错误
- 整个任务能否**合并成一个可读的大 JSON**（含 `task_id`、parent 层级、调用顺序），用于复盘 / 排障 / 面试展示

### 1.2 现状：聚合平均模型

当前观测系统（`backend/agent_nodes/_observability.py` + `_obs_storage.py`）采用**聚合平均模型**：

- 运行期用 `contextvars` + 内存累加器按维度聚合
- `end_task` 时批量落库到 4 张表：`obs_tasks` / `obs_node_metrics` / `obs_llm_metrics` / `obs_mcp_metrics`
- 同 `node × agent × model` 的多次 LLM 调用被**折叠成一行**，只存平均值

**代价**：丢失调用顺序、单次耗时、单次 token、单次错误——无法回答"这个 `route_plan` 节点第 3 次 LLM 调用发生了什么"。

### 1.3 演进历史（诚实记录）

| 阶段 | 方案 | 结果 |
|---|---|---|
| 早期 | 自研 span 树（`obs_spans` / `obs_llm_outputs` / `obs_mcp_results`） | 曾实现，后废弃删除 |
| 中期 | 评估 Langfuse / LangSmith（本地自托管） | 确认成熟方案；但评估部署成本后发现不适用当前环境 |
| 现在 | **重新自研 span 树（轻量，独立实现）** | 本方案 |

**为什么最终不选 Langfuse 容器部署（本次关键决策）**：

1. **资源约束（硬性）**：Langfuse 全家桶依赖 `web + worker + ClickHouse + Postgres + Redis + MinIO(S3)`，ClickHouse 是 OLAP 数据库，内存/磁盘开销大。当前机器 Docker 配额仅 7.6G，只跑一个 Postgres 内存就告急，无法稳定承载 Langfuse。
2. **镜像获取困难**：Docker Hub 访问受限，国内加速器不稳定，镜像拉取多次失败。
3. **规模不匹配**：项目每版本几十到几百条任务，自建窄表（node/llm/mcp 分表）足够，引入 ClickHouse 是过度设计。
4. **数据自主可控 + 通用可扩展**：后续不只做这一个 agent，会扩展其他功能，需要一个**轻量、不依赖额外重服务、能实际跑**的统一观测底座。自研 span 树复用现有 PostgreSQL，是最匹配的。
5. **面试价值**：自研 span 树是"能实际运行 + 能讲清设计 + 能写简历"的可观测实现；Langfuse 的概念（trace / observation / span）在面试中口头讲清即可。

---

## 2. 设计目标

1. **轻量可运行**：复用现有 PostgreSQL，新增少量窄表，不引入任何额外服务
2. **逐调用可追溯**：node / llm / mcp 每次调用都记录，含 `task_id` / 起止时间 / token / 状态
3. **合并大 JSON**：`build_task_trace_json(task_id)` 把一次任务完整调用链合并成可读 JSON
4. **通用可扩展**：设计对"旅行 agent"无业务耦合，后续其他 agent / 功能可复用同一套 span 底座
5. **面试可讲**：从需求 → 设计 → 实现 → 权衡的完整闭环

---

## 3. 核心设计

### 3.1 任务 id：obs_tasks 前置插入（uuid 主键，无自增 id）

> 关键决策：**用户发请求时，先 INSERT `obs_tasks`（主键即业务 uuid `task_id`）**，任务一进来就有落库记录，作为所有 span 的聚合键。

- **主键 = uuid `task_id`**：与业务 task_id / LangGraph checkpoint thread_id 完全一致，子表用同一 uuid 关联，无冗余自增 id。
- **为什么不用自增 id**：唯一性已由 uuid 保证，自增 id 既无被引用的价值（子表用 uuid 关联），也不需要用于排序（`start_ts` 可排序）或断点续跑（续跑靠 uuid thread_id）。保留自增是冗余。
- **"前置生成"的满足方式**：不是靠 DB 自增，而是"任务一开始就 INSERT 一行 obs_tasks（uuid 占位）"——任务一进来就有落库记录，崩溃/重启不丢失任务入口，可追踪。
- **checkpoint / gateway / 前端零改动**：task_id 仍是字符串 uuid，全链路不变。

### 3.2 数据模型：按「采集时机」分窄表（无 NULL 冗余）

> 关键决策：**不用一张大表**（不同 span_type 字段不同，单表平铺会大量 NULL）。改为**按"一次采集动作能同时拿到的数据"拆表**——每张表对应一个采集时机，一次插入一行，各取所需。

**统一模式（三张子表一致）**：
- **无 `seq`**：调用顺序可由 `start_ts` 推导，不冗余存储。
- **无 `duration_ms`**：`duration_ms = end_ts - start_ts` 可推导，不冗余存储，避免与起止时间重叠。
- **子表 `id SERIAL` 仅作每行主键**：用于"占位后 UPDATE 定位该行"和 trace 内排序，不是任务自增 id；任务关联统一用 uuid `task_id`。
- **统一 `status`**：`running | ok | error`，调用开始即占位为 `running`。
- **统一「开始占位 + 结束更新」**：调用开始 INSERT（`start_ts` + `status=running`），结束 UPDATE 补全 `end_ts` / 状态。

```sql
-- ① 任务表：任务开始即插入；task_id=uuid 主键（同业务/checkpoint thread_id，无自增id）
CREATE TABLE IF NOT EXISTS obs_tasks (
    task_id         TEXT PRIMARY KEY,           -- uuid，同 TaskRecord.task_id / LangGraph thread_id
    user_query      TEXT,
    user_id         TEXT,
    session_id      TEXT,
    version         TEXT,
    status          TEXT NOT NULL DEFAULT 'running',  -- running | ok | error
    error           TEXT,
    start_ts        DOUBLE PRECISION NOT NULL,
    end_ts          DOUBLE PRECISION
);

-- ② 节点表：开始占位(running)，结束 UPDATE 补全；按业务 task_id 关联
CREATE TABLE IF NOT EXISTS obs_node_spans (
    id              SERIAL PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES obs_tasks(task_id),
    node            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',  -- running | ok | error
    error           TEXT,
    start_ts        DOUBLE PRECISION NOT NULL,
    end_ts          DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_node_spans_task ON obs_node_spans(task_id);

-- ③ LLM 表：开始占位(running)，结束 UPDATE 补全（含流式输出）
CREATE TABLE IF NOT EXISTS obs_llm_spans (
    id              SERIAL PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES obs_tasks(task_id),
    node            TEXT NOT NULL,              -- 所在节点（父级）
    agent           TEXT NOT NULL,              -- classify / route_plan / hotel_price ...
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cached_tokens   INTEGER DEFAULT 0,
    output          TEXT,                       -- 模型返回的输出字符串（存原文，供复盘）
    status          TEXT NOT NULL DEFAULT 'running',  -- running | ok | error
    error           TEXT,
    start_ts        DOUBLE PRECISION NOT NULL,  -- 开始时间占位
    end_ts          DOUBLE PRECISION            -- 结束时更新
);
CREATE INDEX IF NOT EXISTS idx_llm_spans_task ON obs_llm_spans(task_id);

-- ④ MCP 表：开始占位(running)，返回 UPDATE 补全
CREATE TABLE IF NOT EXISTS obs_mcp_spans (
    id              SERIAL PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES obs_tasks(task_id),
    node            TEXT NOT NULL,              -- 所在节点（父级）
    server          TEXT,
    tool            TEXT,
    retries         INTEGER DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'running',  -- running | ok | error
    error           TEXT,
    start_ts        DOUBLE PRECISION NOT NULL,  -- 开始时间占位
    end_ts          DOUBLE PRECISION            -- 返回时更新
);
CREATE INDEX IF NOT EXISTS idx_mcp_spans_task ON obs_mcp_spans(task_id);
```

**设计要点**：

- **LLM 模型名不存表**：只存 `agent`，trace 合并时按 agent 从配置（`settings.LLM_MODEL_<AGENT>` / `LLM_MAIN_MODEL` / `LLM_FLASH_MODEL`）反查模型名显示。避免冗余。
- **LLM 存输出字符串**：存模型返回原文，trace 能复盘每次返回内容；存储成本可接受（每版本几百条）。
- **node/llm/mcp 统一「开始占位 + 返回补全」**：调用开始先 INSERT（`start_ts`、`status=running`），返回后 UPDATE 补全 `end_ts` / 状态。`duration_ms = end_ts - start_ts`，不冗余存储。

**span 树层级**：

```
task (obs_tasks, task_id)
 └─ node span (obs_node_spans)
     ├─ llm span (obs_llm_spans, node=父级)
     └─ mcp span (obs_mcp_spans, node=父级)
```

对应你的原话：**"task_id, parent, mcp/llm/database"** —— 节点是 parent，LLM/MCP 调用的 parent 就是所在节点。

### 3.3 写入链路：改造 `_observability.py`

现有 `_observability.py` 从"内存累加"改为"逐调用写 span"：

- 任务开始：INSERT `obs_tasks` 拿 task_id（DB 前置生成）
- `node` / `node_scope` 装饰器 → 开始 INSERT 占位（`status=running`），结束 UPDATE 补全 `end_ts` / `status`
- `record_llm()` → 开始 INSERT 占位，结束 UPDATE 补全 token / output / `end_ts` / `status`
- `record_mcp()` → 开始 INSERT 占位，返回 UPDATE 补全 `end_ts` / retries / `status`
- 维护 `contextvars` 的 `_current_node`，让 llm/mcp span 的 `node` 正确指向当前节点

**流式输出（特殊场景）**：
- 流式调用（如 summarizer / conversation_reply，`_LLM(streaming=True)` 逐步产出 token）：**开始 INSERT 占位（`status=running`）**，流式过程中**不逐 chunk 落库**（避免高频写库），在内存累积 output 文本与 token；**结束时 UPDATE 一次**，补全 `end_ts`、完整 `output`、最终 token 汇总、`status=ok/error`。
- 好处：既记录完整流式结果，又不因每 chunk 写库产生 DB 压力；span 只有 2 次 DB 操作（1 INSERT + 1 UPDATE）。

**并发隔离**：复用现有 `contextvars` 机制——`asyncio.gather` 多城并发时，每个任务协程有自己的 `_current_node`，保证 llm/mcp span 归属正确的 node。

### 3.4 合并大 JSON：`build_task_trace_json(task_id)`

```python
async def build_task_trace_json(task_id: int) -> Dict:
    """把一次任务完整调用链合并成可读 JSON。"""
    task = await storage.get_task(task_id)         # obs_tasks
    nodes = await storage.get_node_spans(task_id)  # obs_node_spans
    llms  = await storage.get_llm_spans(task_id)   # obs_llm_spans
    mcps  = await storage.get_mcp_spans(task_id)   # obs_mcp_spans
    # 按 node 分组挂载 llm/mcp；模型名按 agent 从配置反查
    return {"task_id": task_id, "summary": ..., "trace": [...], "model_map": {...}}
```

**可复用现成逻辑**：`monitor/analyze.py` 的 `_summary(spans)`（L108）与 `load_from_json._walk_node`（L466）已能按 span 结构聚合 / 递归遍历，移到共享位置（`backend/agent_nodes/_obs_trace.py`）供复用。

### 3.5 对外接口

| 接口 | 说明 |
|---|---|
| `GET /internal/obs/{task_id}` | 现有，返回任务 summary + 节点聚合（兼容） |
| `GET /internal/obs/{task_id}/trace` | **新增**：返回完整调用链合并大 JSON |
| gateway 透传 | `GET /obs/{task_id}/trace`（需 JWT） |

---

## 4. 与 Langfuse 的取舍（面试话术）

> "我调研过 Langfuse / LangSmith 这类业界 LLM 可观测平台，也实际尝试过本地自托管。Langfuse 很强大（trace / observation / generation / span 模型，Dashboard 可视化），但它的部署是重架构——web + worker + ClickHouse + Postgres + Redis + S3，ClickHouse 是 OLAP 库，对内存磁盘要求高。我评估后基于三点决定不依赖它：
> 1. **资源约束**：当前机器 Docker 配额不足，无法稳定跑起 Langfuse 全家桶；
> 2. **规模不匹配**：项目每版本几百条任务，自建窄表（node/llm/mcp 分表）足够，引入 ClickHouse 是过度设计；
> 3. **数据自主 + 可扩展**：我要的是一个能实际运行、不依赖额外重服务、可复用到后续其他 agent 的观测底座，自研 span 树复用现有 PostgreSQL 最合适。
> 所以最终选了自研 span 树——它满足'轻量、能跑、可扩展、可追溯'，Langfuse 的概念（span/trace 层级）我在设计里也充分吸收。这是基于真实约束和规模的技术选型，而不是盲目自研。"

---

## 5. 实现范围（后续实施）

1. **`_obs_storage.py`**：新增 `obs_tasks`(uuid主键+前置) / `obs_node_spans` / `obs_llm_spans` / `obs_mcp_spans` 建表 + 各表读写方法
2. **`_observability.py`**：任务开始 INSERT `obs_tasks` 拿 task_id；`node`/`node_scope`/`record_llm`/`record_mcp` 统一「占位+更新」（含流式结束才 UPDATE）
3. **新增 `_obs_trace.py`**：`build_task_trace_json`（按 node 挂载 llm/mcp + 模型名按 agent 从配置反查，复用 monitor 逻辑）
4. **`monitor/analyze.py`**：`load_from_db` 从新表聚合（或新增 `load_trace`）
5. **`server.py`**：暴露 `/internal/obs/{task_id}/trace`；**`gateway/main.py`** 透传
6. **`core/service.py`**：`get_obs` 支持返回 trace

---

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 逐调用落库 DB 开销 | LLM 一次 INSERT；node/mcp 占位+UPDATE，避免运行期高额写放大 |
| 存储体积膨胀 | LLM 存输出原文（可接受）；如需可加开关禁用输出存储 |
| 并发归属错误 | 复用 `contextvars` 隔离 `_current_node` |
| monitor 读取兼容 | 从新表聚合时复用 `_summary` / `_merge_per_node` 公式 |
| 现有 400 条历史数据 | 新表为新 schema，历史聚合数据不受影响；monitor 兼容旧 4 表读取 |

---

## 7. 面试叙事（直接可背）

> "这个项目我做了自研的可观测系统。核心是 span 树：一次任务是一个 trace，每个节点（classify / transport / city_planning 等）是一个 node span，节点里的每次 LLM / MCP 调用作为子 span，通过 parent_id 关联，形成 task → node → llm/mcp 的层级。这样每个用户问题都能追溯完整调用链，并按 task_id 合并成一个大 JSON。
>
> 我调研过 Langfuse / LangSmith，也实际尝试过自托管。但评估后发现 Langfuse 的部署是重架构（web + worker + ClickHouse + Postgres + Redis + S3），ClickHouse 对资源要求高，当前环境跑不起来；而项目规模每版本几百条任务，用 PostgreSQL 按采集时机分窄表存 span 完全够。所以我选择自研——它轻量、能实际运行、可复用到后续其他 agent 功能。
>
> 技术上我用了 contextvars 解决 asyncio.gather 多城并发时的 span 归属问题，用批量 executemany 控制落库开销，并设计了 trace 合并接口。这个过程的收获是理解了『自研 vs 用成熟工具』要基于真实规模、资源和数据自主性权衡。"
