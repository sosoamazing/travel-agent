# 任务态外移（Task State Externalization）设计文档

> 状态：**热路径已落地**（内存 TaskRecord 旁路双写 Redis Hash + `obs_tasks.result_payload`）
> 权威说明：`docs/runtime-storage-and-mq.md`
> 未做：`TaskStateStore` 协议抽象、SSE 跨实例、会话锁分布式化、任务队列化
> 关联：`docs/mq-design.md`（调度扩容草稿，未实施）

---

## 1. 背景与动机

### 1.1 现状：TaskRecord 全部活在进程内存里

`backend/core/service.py` 的 `TaskManager` 用进程内 dict 保存全部任务运行态：

```python
# service.py:194-199
class TaskManager:
    """内存任务注册表（单进程内共享，多 uvicorn worker 各自独立）"""
    def __init__(self):
        self._tasks: Dict[str, TaskRecord] = {}
        self._lock = threading.Lock()
```

`TaskRecord`（service.py:155-191）承载了任务的完整生命周期状态：

| 字段 | 内容 | 现在的存处 |
|---|---|---|
| status | pending / running / succeeded / failed | 内存 dict |
| current_node / progress_message | 当前执行节点与进度文案 | 内存 dict |
| result / error | 最终结果 JSON / 错误信息 | 内存 dict |
| obs_task_id | 观测根 span id（与业务 task_id 解耦） | 内存 dict |
| queue | SSE 事件队列（asyncio.Queue） | 进程内队列 |

### 1.2 不做的代价（现状痛点）

| # | 场景 | 后果 |
|---|---|---|
| 1 | backend 起 2 个 uvicorn worker | 任务提交落在 worker1，轮询 `/internal/tasks/{id}` 打到 worker2 → 查不到，404 |
| 2 | backend 进程重启 | 全部进行中任务状态丢失；前端轮询断流，任务"消失" |
| 3 | 多实例下的同会话并发拦截 | `has_active_for_session()` 遍历本进程 dict，多实例各自为政 → 拦截失效 |
| 4 | SSE 流式回传 | `asyncio.Queue` 绑死所在进程，worker 一换/重启就断 |
| 5 | 水平扩容 | 内存态决定了"收请求的进程"必须同时是"执行任务的进程"，无法拆分 |

读路径已是 **内存 → Redis Hash `task:status:{task_id}` → `obs_tasks`**。
业务结果结束时写入 `obs_tasks.result_payload`（不是 `result` 列；`result` 仍是粗粒度状态）。
Redis 结束后 TTL 24h；未配 `REDIS_URL` 则跳过 Redis。SSE 仍绑执行进程。

### 1.3 与 mq-design.md 的关系

`docs/mq-design.md` 规划的是更完整的「任务队列化」：Redis Streams 队列 + worker 独立
进程 + Pub/Sub 流式回传。它把**执行调度**也搬出进程。本文档只做其中**状态层**这一环：

- **任务态外移**（本文档）：`_tasks` 内存 dict → Redis Hash。**调度仍留在进程内**。
- **任务队列化**（mq-design.md）：`asyncio.create_task` → 消息队列消费。

两者可独立实施；本方案（状态外移）是更低风险的第一步，且是队列化的必要前提。

---

## 2. 技术选型

### 2.1 结论：**Redis Hash**（当前阶段）

| 选型 | 优点 | 缺点 | 适配 |
|---|---|---|---|
| **Redis Hash** | 轻量、天然 KV 语义、TTL 过期、单容器部署 | 额外依赖；值需序列化 | ✅ 规模匹配 |
| PostgreSQL 表 | 复用现有 PG、事务强一致、可 JOIN obs_tasks | 高频读写拉低主库性能、需建表/迁移 | 次选 |
| 内存 dict（现状） | 零依赖 | 重启丢、多实例失效 | 起点 |

> 与 `mq-design.md` 选型原则一致：基于真实规模（每版本几百任务）避免重架构。
> Redis 已在 mq-design 中作为队列载体引入，状态外移顺手复用同一 Redis 实例，
> **不引入第二套基础设施**。

### 2.2 Redis 在本方案中的角色

只做一件事：**任务状态缓存**（`task:status:{task_id}` Hash）。

- 生产者在 `submit_chat` 时写入 `pending` 状态
- 执行者（仍在本进程内）更新 status / current_node / result
- 任何进程的轮询端点读同一份 Hash → 多实例可用
- Hash 带 TTL（如 24h），任务结束后过期清理，避免无限增长

---

## 3. 状态模型设计

### 3.1 Key 与字段

| Key | 结构 | 说明 |
|---|---|---|
| `task:status:{task_id}` | Hash | 任务状态快照（对应现 TaskRecord 字段） |

Hash 字段映射（与 `TaskRecord.snapshot()` 对齐）：

| Field | 对应 TaskRecord | 说明 |
|---|---|---|
| status | status | pending / running / succeeded / failed |
| current_node | current_node | 当前执行节点 |
| progress_message | progress_message | 进度文案 |
| result | result | 最终结果 JSON（序列化字符串） |
| error | error | 错误信息 |
| created_at / started_at / finished_at | 同名 | 起止时间（unix 秒） |
| user_id / session_id / user_query | 同名 | 任务元信息（供归属校验/查询） |
| obs_task_id | obs_task_id | 观测根 span id |

### 3.2 与 obs_tasks 的分工（避免双写混乱）

| 数据 | 存处 | 生命周期 |
|---|---|---|
| **业务结果**（result JSON / city_plans / final_answer） | Redis Hash（TTL 24h）+ `obs_tasks.result_payload` | 热缓存短、PG 长期 |
| **观测数据**（span 树 / token / 耗时） | PostgreSQL obs_* 表 | 永久 |
| **会话历史**（消息列表） | PostgreSQL chat_messages | 永久 |

> Redis 管热状态；PG 管冷审计。`obs_tasks.result` 仍是粗粒度状态，业务 JSON 在 `result_payload`。

---

## 4. 已落地的改造

没有 `TASK_STATE_STORE` 切换。内存 `TaskManager` 仍是本进程热路径（尤其 SSE）；
`core/task_state.py` 旁路双写 Redis。未配 `REDIS_URL` 则跳过。

| 点 | 实现 |
|---|---|
| 提交 / 进度 | 只写 Redis（PG 无进度列） |
| 终态 | 先 PG `result_payload`，成功后再写 Redis + TTL |
| 读取 | 内存 → Redis → PG；PG 命中回填 Redis |
| 会话锁 | 仍遍历本进程 dict（未分布式化） |
| SSE | 仍 `record.queue`，绑执行进程 |

未做：`TaskStateStore` Protocol、会话锁 SET NX、SSE Pub/Sub、任务队列。
需要多实例流式时再看 `docs/mq-design.md` / runtime 文档里的 Redis Streams 条件。

---

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| Redis 额外依赖 | compose 单容器；未配 `REDIS_URL` 则跳过 |
| Redis 不可用 | 写失败打日志，降级内存 + PG，不打断任务；`/health` 含 redis |
| TTL 过期查不到 | 24h 窗口；更久走 `obs_tasks.result_payload` |
| 与 obs 双写 | Redis 热状态，PG 冷审计，`result` 列不是业务 JSON |

---

## 附：文档变更记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1 | 2026-08-20 | 初稿：任务态外移设计（状态层，队列化前置） |
| v2 | 2026-09-18 | 对齐实现：Redis 旁路双写 + result_payload；队列化仍未做 |
