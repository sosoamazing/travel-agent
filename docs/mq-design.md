# 消息队列（MQ）改造实现文档

> 状态：**设计方案（待评审）**
> 目标读者：项目作者 / 协作者 / 面试评审
> 关联：`docs/observability-design.md`、`docs/traceability-design.md`

---

## 1. 背景与动机

### 1.1 现状：进程内异步任务（in-process asyncio）

当前 `backend/core/service.py` 用「进程内 asyncio」实现任务的异步执行与结果回传，核心链路：

```
submit_chat
  → TaskManager.has_active_for_session(session_id)   同会话并发锁（拒绝重复提交）
  → TaskManager.create(...)                          创建内存 TaskRecord
  → asyncio.create_task(_run_task(record))           进程内调度执行
  → _concurrency_sem（asyncio.Semaphore）             进程内并发限流
  → record.queue（asyncio.Queue）→ SSE                流式回传 token / progress / final
```

关键进程内耦合点（也是 MQ 化的改造对象）：

| # | 组件 | 位置 | 进程内特征 |
|---|---|---|---|
| 1 | 任务调度 | `service.py:365,406` `asyncio.create_task` | 单进程内调度，进程崩溃即丢 |
| 2 | 并发限流 | `service.py:253,435` `asyncio.Semaphore` | 单进程内信号量，多实例各自独立 |
| 3 | 流式回传 | `service.py:466,478,521` `record.queue` | 进程内 asyncio.Queue + SSE |
| 4 | 任务状态 | `service.py:155-230` `self._tasks` 内存 dict | 进程重启即清空 |

### 1.2 为什么要引入 MQ

1. **解耦**：把「接收请求（生产者）」与「执行任务（消费者）」分离，各自独立伸缩。
2. **可靠投递**：任务入队即持久化，进程崩溃/重启不丢任务；可重试、可延迟。
3. **横向扩展**：多 worker 实例并行消费；当前单进程 + 内存态无法多实例。
4. **削峰填谷**：压测/高峰期队列积压缓冲，消费者按能力消化。
5. **当前架构很适合**：`_run_task(record, resume)` 已是「可序列化工作单元」，天然是 MQ 消费者形态，业务逻辑无需重写。

### 1.3 不做 MQ 的代价（现状痛点）

- `self.tasks` 内存态：**backend 重启丢全部任务状态**（正是观测 trace 查不到历史任务的根因）。
- 单进程：无法多 worker，CPU/内存上限受限。
- 进程崩溃丢进行中任务，无重试。

---

## 2. 技术选型

### 2.1 结论：**Redis Streams**（当前阶段）

| 选型 | 优点 | 缺点 | 本项目适配 |
|---|---|---|---|
| **Redis Streams** | 轻量、支持消费组/ACK/死信、原生流式事件、部署简单（单容器） | 无持久化到磁盘强保证（可配 AOF）；单写者性能上限 | ✅ 规模匹配，与 ClickHouse 同理避免过度设计 |
| RabbitMQ | 成熟、路由灵活、可靠投递 | 重部署（Erlang），流式 token 需额外机制 | 功能偏重 |
| Kafka | 高吞吐、日志语义 | 重依赖（ZK/KRaft），运维复杂 | 过度设计（当前每版本几百任务） |
| 进程内 asyncio（现状） | 零依赖 | 单进程、重启丢状态、无法扩展 | 起点 |

> 选型原则与观测系统一致：**基于真实规模、资源约束、数据自主性权衡**，避免重架构。

### 2.2 Redis 在项目中的角色

Redis 承担三件事（可拆可合）：
1. **Streams**：任务队列（生产者入队，消费者消费组）。
2. **Pub/Sub**：流式 token / progress 事件回传（SSE 拉取改订阅推送）。
3. **任务状态缓存**：`TaskRecord` 从内存 dict 迁到 Redis Hash（缓解重启丢状态）。

> 若不想引入 Redis，纯 MQ 用 RabbitMQ + 前端轮询结果也可，但流式体验退化。

---

## 3. 总体架构

```
┌──────────────┐    JWT    ┌──────────────┐   internal    ┌──────────────────────────────┐
│  前端/管理后台 │ ────────► │   Gateway    │ ─────────────► │   Backend（生产者 + API）     │
└──────────────┘           └──────────────┘                │  submit_chat → 校验 → 入队    │
                                                            └──────────────┬───────────────┘
                                                                           │ produce
                                                                           ▼
                                                                   ┌──────────────┐
                                                                   │  Redis Stream │
                                                                   │   obs_tasks   │
                                                                   │   task:queue   │
                                                                   └──────┬───────┘
                                                                           │ consume (消费组)
                                                            ┌──────────────▼───────────────┐
                                                            │   Worker（消费者进程 ×N）      │
                                                            │  _run_task(record)            │
                                                            └──────┬────────────────────────┘
                                                                   │ 事件回传（Pub/Sub / 状态写回）
                                                                   ▼
                                                            ┌──────────────┐
                                                            │ Redis Pub/Sub │  ← token / progress / final
                                                            └──────────────┘
```

**职责分离**：
- **Backend**：接收请求、校验权限/会话锁、入队、查询任务状态、SSE 连接订阅。
- **Worker**：消费消息、执行 `_run_task`、写 obs、发布事件、更新任务状态。
- **Redis**：队列 + 状态 + 事件总线。

---

## 4. 消息模型设计

### 4.1 队列与 Stream Key

| Key | 用途 | 结构 |
|---|---|---|
| `task:stream` | 任务执行队列 | Redis Stream（消费组 `task-workers`） |
| `task:status:{task_id}` | 任务状态快照 | Hash（字段见下） |
| `task:events:{task_id}` | 任务事件（token/progress/final） | Redis Stream / Pub-Sub channel |

### 4.2 任务入队消息体（生产者 → 队列）

```json
{
  "task_id": "uuid",
  "user_query": "北京一日游",
  "session_id": "xxx",
  "user_id": "test_user_01",
  "intent": "travel",
  "resume": false,
  "obs_task_id": null,
  "created_at": 1787055875.0,
  "retry_count": 0
}
```

### 4.3 任务状态 Hash（worker 写回，backend/前端读）

对应现 `TaskRecord` 字段：

| Field | 说明 |
|---|---|
| status | pending / running / succeeded / failed |
| current_node | 当前执行节点 |
| progress_message | 进度文案 |
| result | 最终结果 JSON（序列化） |
| error | 错误信息 |
| started_at / finished_at | 起止时间 |
| user_id / session_id / user_query | 任务元信息 |

### 4.4 事件消息（worker → 订阅者）

现 `record.queue.put({...})` 的三种事件类型原样迁移：
```json
{"type": "progress", "node": "classify"}
{"type": "token", "text": "北京"}
{"type": "final", "result": {...}}
{"type": "error", "text": "..."}
```
结束哨兵 `None` 由「发布 final/error + 状态置终态」替代。

---

## 5. 改造点（基于现有代码）

### 5.1 抽取执行内核：`_run_task` 解耦

**现状**：`_run_task` 直接写 `record.queue`（进程内 Queue）、`record.result/status`（内存）、`self._concurrency_sem`。

**改造**：把「执行逻辑」与「回传/状态」解耦为可插拔接口。

```python
# 新增：任务执行器（worker 侧核心），_run_task 迁移为 execute()
class TaskExecutor:
    def __init__(self, emit: Emitter, state: StateStore, sem: Optional[asyncio.Semaphore]):
        ...

    async def execute(self, msg: TaskMessage) -> None:
        # 内部：astream_events 迭代，把事件交给 self.emit()，状态交给 self.state.update()
        # 观测层 start_task/end_task 不变（contextvars 隔离仍有效）
        ...
```

- `emit(event)` 抽象了「回传」：进程内实现写 `record.queue`；MQ 实现发布到 Redis。
- `state.update()` 抽象了「状态」：进程内写 `TaskRecord`；MQ 实现写 Redis Hash。

### 5.2 生产者（Backend）

```python
# submit_chat 改造：不再 create_task，改为入队
async def submit_chat(self, ...):
    if self.tasks.has_active_for_session(session_id):   # 会话锁仍在前置（也可挪到队列判重）
        raise RuntimeError("该会话有进行中的任务")
    task_id = uuid.uuid4().hex
    await self.redis.xadd("task:stream", msg)            # 入队
    return task_id
```

### 5.3 消费者（Worker 独立进程）

```python
async def worker_loop():
    while True:
        entries = await redis.xreadgroup("task-workers", "consumer-1",
                                         "task:stream", block=5000)
        for entry in entries:
            try:
                await executor.execute(msg)
                await redis.xack("task:stream", "task-workers", entry_id)
            except Exception:
                await handle_retry(msg)                  # 重试/死信
```

### 5.4 流式回传（最难部分）

**现状**：`record.queue` + 前端 SSE `GET /tasks/{task_id}/stream` 拉取。

**MQ 方案**：SSE 端点改为「订阅 Redis Pub/Sub channel」，worker 发布事件：

```python
# backend SSE 端点
@app.get("/internal/tasks/{task_id}/stream")
async def stream_task(task_id, user_id):
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"task:events:{task_id}")
    async def gen():
        async for msg in pubsub.listen():
            yield f"data: {msg['data']}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")
```

- 前端**无需改**：仍是 SSE，协议不变。
- 事件发布改为 worker `redis.publish(f"task:events:{task_id}", json.dumps(event))`。

### 5.5 并发限流迁移

- 现状 `_concurrency_sem`（单进程）→ 删除或仅作「单 worker 内」软限流。
- 真正的并发控制 = **worker 进程数 + 每 worker 消费速率**。
- 会话锁（`has_active_for_session`）：可用 Redis `SET NX` 分布式锁 / 队列内去重替代，避免多 worker 竞争。

---

## 6. 分阶段实施

### 阶段 0（地基，不改架构）
- 定义 `Emitter` / `StateStore` 抽象，把 `record.queue.put` 和 `record.status` 写入封装。
- 保持进程内实现（回退兼容），保证现功能零回归。

### 阶段 1（引入 Redis，队列化）
- 加依赖 `redis`，启动 Redis 容器。
- `submit_chat` 入队；单 worker 进程消费 `_run_task`。
- SSE 端点改订阅 Redis Pub/Sub。
- 任务状态写 Redis Hash（替代内存 dict 为主存）。

### 阶段 2（多 worker + 可靠性）
- 多 worker 进程（`worker.py`），消费组 + ACK。
- 失败重试（retry_count）、死信队列、延迟重试（XADD MINID + 延时消费）。
- 会话锁改分布式（Redis SETNX / 队列内唯一约束）。

### 阶段 3（可选，观测集成）
- obs 任务入口与队列消息联动；任务状态与 obs_tasks 归一对齐。

---

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| Redis 引入额外依赖 | 单容器、轻量；规模匹配（避免 ClickHouse 式过度设计） |
| 流式 token 经 Pub/Sub 可能乱序/丢失 | 事件带 seq；final 事件作为强一致终态，前端以 final 为准；必要时落 Redis Stream |
| 会话锁并发竞态（多 worker） | 队列消费前置 Redis 分布式锁；入队时 SETNX 会话锁 |
| 任务状态读一致性 | 状态以 Redis Hash 为准，SSE final 事件作为提交点；后端重启从 Redis 恢复 |
| 迁移期回归 | 阶段 0 保留进程内实现，抽象层可切换 |

---

## 8. 面试叙事（可选背诵）

> "我评估过当前异步任务架构的瓶颈：进程内 asyncio + 内存任务表，单进程、重启丢状态、无法横向扩展。我做了一个演进设计：用消息队列把『接收请求』和『执行任务』解耦。核心是**把 `_run_task` 从写进程内 Queue / 内存 TaskRecord 解耦成 Emitter + StateStore 两个接口**，进程内实现用于迁移期零回归，MQ 实现用 Redis Streams 做任务队列、Redis Pub/Sub 做流式事件回传、Redis Hash 做任务状态。这样 `_run_task` 的业务逻辑完全复用，只是换掉调度和回传载体。SSE 前端协议不变，只改后端事件来源。选 Redis 而非 Kafka/RabbitMQ 是因为规模匹配——每版本几百任务，Redis Streams 的消费组 + ACK + 死信已够用，避免重架构。"

---

## 附：文档变更记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1 | 2026-08-18 | 初稿：MQ 选型 + 总体架构 + 消息模型 + 分阶段改造 |
